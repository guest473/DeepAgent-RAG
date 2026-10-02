from __future__ import annotations

import os.path
import time
import threading
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from utils.config_loader import vector_db_config
from langchain_milvus import Milvus
from utils.file_tool import list_files_with_allowed_type, get_file_md5_hex, check_file_md5_hex_in_file, choose_loader, \
    add_file_md5_hex_to_file, remove_file_md5_hex_from_file
from utils.path_tool import get_abs_path
from dotenv import load_dotenv
load_dotenv()

_vector_store_instance: VectorStore | None = None
_lock: threading.Lock = threading.Lock()

# 单文件入库闭环参数：失败后删向量重建的最大次数与重试间隔（秒）
_INDEX_MAX_ATTEMPTS = 3
_INDEX_RETRY_SLEEP = 1.0

# 入库失败的对外文案：意外异常的原文可能含 Milvus 连接串、主机名等内部信息，只写服务端日志，
# 接口回传统一用此文案；已知原因（类型不支持 / 加载失败 / 校验条数不符）仍如实返回，便于管理员定位
_INDEX_FAIL_GENERIC = "入库失败，详见服务端日志"


def get_vector_store() -> "VectorStore":
    """返回 VectorStore 的全局单例（双重检查锁），线程安全。"""
    global _vector_store_instance
    if _vector_store_instance is None:
        with _lock:
            if _vector_store_instance is None:
                _vector_store_instance = VectorStore()
    return _vector_store_instance


class VectorStore:
    def __init__(self):
        # Milvus 实现（通过 Docker 部署 Milvus 服务）
        milvus_uri = vector_db_config["milvus_uri"]
        self.vector_store = Milvus(
            collection_name=vector_db_config["collection_name"],
            embedding_function=DashScopeEmbeddings(model=vector_db_config["embedding_model"]),
            connection_args={
                "uri": milvus_uri,
                "pool_size": vector_db_config.get("pool_size", 50),
            },
            drop_old=False,
        )
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=vector_db_config["chunk_size"],
            chunk_overlap=vector_db_config["chunk_overlap"],
            separators=vector_db_config["separators"],
            length_function=len,
        )
        self._retriever = self.vector_store.as_retriever(search_kwargs={"k": vector_db_config["k"]})
        # 入库/删除互斥锁（可重入，index_file 内部也会获取）：
        # 防止并发构建或"构建-删除"交错导致同一文件重复入库
        self._store_lock = threading.RLock()

    def get_retriever(self):
        return self._retriever

    def do_store(self, file_paths=None) -> list[dict]:
        """加载未入库文件并完成入库闭环（写入 → 校验 → 失败删向量重建复验）。

        返回失败文件清单 [{'file': ..., 'error': ...}]，全部成功时为空列表。
        """
        failures: list[dict] = []
        if file_paths is None:
            allowed_type_files_list = list_files_with_allowed_type(
                vector_db_config["knowledge_dir"],
                tuple(vector_db_config["allowed_types"])
            )
        else:
            allowed_type_files_list = file_paths
        with self._store_lock:
            for f in allowed_type_files_list:
                try:
                    md5_hex = get_file_md5_hex(f)
                    if md5_hex is None:
                        failures.append({"file": f, "error": "MD5 计算失败"})
                        continue
                    if check_file_md5_hex_in_file(md5_hex):
                        continue  # 已有入库记录，跳过
                    result = self.index_file(f)
                    if result["ok"]:
                        print(f"已添加并存入{f}，共{result['chunks']}个文档块")
                    else:
                        print(f"添加文件{f}时出错: {result['error']}")
                        failures.append({"file": f, "error": result["error"]})
                except Exception as e:
                    print(f"添加文件{f}时出错: {e}")
                    failures.append({"file": f, "error": _INDEX_FAIL_GENERIC})
        return failures

    # ---------- 单文件入库闭环 ----------

    def index_file(self, file_path: str) -> dict:
        """单文件入库闭环：切分 → 写入向量 → 写 md5 记录 → 立即校验。

        校验标准：Milvus 中该 source 的条数 == 切块数，且 md5.txt 含该记录；
        两项任一不过即删除该文件全部向量与记录后重建复验，最多重试
        `_INDEX_MAX_ATTEMPTS` 次。成功返回 {'ok': True, 'chunks': n}；
        失败返回 {'ok': False, 'error': ...}，不抛异常。每个文件入库
        自己构成一个小闭环，不依赖定时自检兜底。
        """
        file_path = get_abs_path(file_path)
        with self._store_lock:
            md5_hex = get_file_md5_hex(file_path)
            if md5_hex is None:
                return {"file": file_path, "chunks": None, "ok": False, "error": "MD5 计算失败"}
            # 已被其他流程完成入库则幂等跳过（并发构建/上传时防重复写入）
            if check_file_md5_hex_in_file(md5_hex):
                return {"file": file_path, "chunks": None, "ok": True, "error": None}
            loader = choose_loader(file_path)
            if loader is None:
                return {"file": file_path, "chunks": None, "ok": False, "error": "不支持的文件类型"}
            doc = loader(file_path)
            if doc is None:
                return {"file": file_path, "chunks": None, "ok": False, "error": "文件加载失败"}
            split_doc = self.splitter.split_documents(doc)
            if not split_doc:
                return {"file": file_path, "chunks": None, "ok": False, "error": "文件分割后为空"}

            last_err: str = _INDEX_FAIL_GENERIC
            for attempt in range(1, _INDEX_MAX_ATTEMPTS + 1):
                try:
                    self.vector_store.add_documents(split_doc)
                    add_file_md5_hex_to_file(md5_hex)
                    if self._verify_indexed(file_path, md5_hex, len(split_doc)):
                        return {"file": file_path, "chunks": len(split_doc), "ok": True, "error": None}
                    last_err = f"校验未通过（Milvus 条数 ≠ 切块数 {len(split_doc)}）"
                except Exception as e:
                    last_err = _INDEX_FAIL_GENERIC
                    print(f"文件{file_path}入库第{attempt}次失败: {e}")
                # 补偿清理：删除该文件全部向量与 md5 记录后重试，保证下次写入不重复
                self.delete_by_source(file_path)
                if check_file_md5_hex_in_file(md5_hex):
                    remove_file_md5_hex_from_file(md5_hex)
                if attempt < _INDEX_MAX_ATTEMPTS:
                    time.sleep(_INDEX_RETRY_SLEEP)
            return {"file": file_path, "chunks": None, "ok": False, "error": last_err}

    def _verify_indexed(self, file_path: str, md5_hex: str, expected_chunks: int) -> bool:
        """校验单文件入库结果：md5 记录存在 且 Milvus 条数 == 切块数。

        Milvus Bounded 一致性下刚插入的数据可能短暂不可见，
        首次计数不符时 flush 后复验一次，避免误判重建。
        """
        if not check_file_md5_hex_in_file(md5_hex):
            return False
        if self._count_vectors(file_path) == expected_chunks:
            return True
        self._flush_milvus()
        return self._count_vectors(file_path) == expected_chunks

    def _count_vectors(self, file_path: str) -> int:
        """返回 Milvus 中该 source 的向量条数。"""
        return len(self.pks_by_source(file_path))

    def _flush_milvus(self) -> None:
        """强制 Milvus 落盘，使刚插入的数据对后续 query 可见。"""
        client = getattr(self.vector_store, "client", None)
        if client is not None:
            client.flush(self.vector_store.collection_name)

    # Milvus 的 query() 有 offset + limit ≤ 16384 的硬上限，固定页大小 + 递增 offset 翻页
    # 在数据量涨上去后必然报错（invalid max query result window）。游标迭代器没有该上限。
    _ITER_BATCH_SIZE = 1000

    def pks_by_source(self, file_path: str) -> list:
        """拉取某 source 的全部主键（校验、删除、自检共用）。"""
        vs = self.vector_store
        pk_field = getattr(vs, "_primary_field", "pk")
        escaped_path = file_path.replace('\\', '\\\\').replace('"', '\\"')
        iterator = vs.client.query_iterator(
            collection_name=vs.collection_name,
            batch_size=self._ITER_BATCH_SIZE,
            filter=f'source == "{escaped_path}"',
            output_fields=[pk_field],
        )
        try:
            pks: list = []
            while True:
                rows = iterator.next()
                if not rows:
                    break
                pks.extend(row.get(pk_field) for row in rows)
            return pks
        finally:
            iterator.close()

    def delete_by_source(self, file_path: str) -> int:
        """按 source 删除该文件的全部向量（分批，避免单次请求过大），返回删除条数。"""
        pks = self.pks_by_source(file_path)
        for i in range(0, len(pks), self._ITER_BATCH_SIZE):
            self.vector_store.delete(ids=pks[i:i + self._ITER_BATCH_SIZE])
        return len(pks)

    def _another_file_with_same_content(self, file_path: str, md5_hex: str) -> str | None:
        """返回知识库中另一份内容相同的文件路径；不存在则返回 None。

        内容级去重下，同内容文件共享一条入库记录与一组向量（向量挂在首个入库
        文件的 source 上）。md5 计算走缓存，重复调用开销可忽略。
        """
        norm_self = os.path.normcase(os.path.abspath(file_path))
        for f in list_files_with_allowed_type(
            vector_db_config["knowledge_dir"], tuple(vector_db_config["allowed_types"])
        ):
            if os.path.normcase(os.path.abspath(f)) == norm_self:
                continue
            if get_file_md5_hex(f) == md5_hex:
                return f
        return None

    def delete_file(self, file_path, md5_hex: str | None = None):
        """删除文件对应的向量块和 MD5 记录。失败时抛出异常，不静默吞掉。

        `md5_hex` 可由调用方传入：若调用方先删掉了磁盘文件（Windows 下文件被占用时
        删除会失败，先删文件能把失败挡在改动索引之前），文件已不在，本方法算不出 md5。

        先删入库记录、再删向量；向量删除失败时回滚恢复入库记录，
        保证失败后状态要么完整未删、要么"记录缺失+向量仍在"，均由
        index_health 按"重建记录"修复，而不是误把待删文件重新索引。

        若被删的正是"持有向量的那一份"（向量挂在其 source 上）而磁盘上还有同内容
        副本，则删完向量后立即以副本重新入库——否则会留下 missing_vectors：
        界面显示"已入库"但实际检索不到，要等定时自检或手动修复才恢复。
        """
        file_path = get_abs_path(file_path)
        if md5_hex is None:
            md5_hex = get_file_md5_hex(file_path)

        # Milvus 实现：通过 expr 过滤 source 元数据获取主键后删除
        with self._store_lock:
            survivor = self._another_file_with_same_content(file_path, md5_hex) if md5_hex else None
            # 内容级去重：仅当磁盘上已无同内容文件时才移除入库记录，
            # 否则删掉副本会连带把原文件误标成「未入库」（向量其实还在）
            drop_record = bool(md5_hex) and survivor is None
            if drop_record:
                try:
                    remove_file_md5_hex_from_file(md5_hex)
                except Exception:
                    # 瞬时故障重试一次；仍失败则抛出，此刻记录、向量、文件均未动，无部分状态
                    remove_file_md5_hex_from_file(md5_hex)

            try:
                deleted = self.delete_by_source(file_path)
            except Exception:
                # 回滚：恢复本次移除的入库记录，使文件保持"可检索"的一致状态
                if drop_record:
                    try:
                        add_file_md5_hex_to_file(md5_hex)
                        print(f"向量删除失败，已恢复{os.path.basename(file_path)}的入库记录")
                    except Exception as rollback_err:
                        print(f"向量删除失败且恢复入库记录失败: {rollback_err}（可依赖 index_health repair 重建记录）")
                raise

            if deleted:
                print(f"已从向量库删除文件{file_path}的{deleted}个文档块")
            else:
                print(f"向量库中未找到文件{file_path}的文档")

            # 删掉的正是持有向量的那一份，且磁盘上还有同内容副本：
            # 向量已随之消失，若只保留记录就会变成 missing_vectors（界面显示「已入库」
            # 但实际检索不到）。这里立即以副本重新入库，维持「磁盘上还有同内容 ⇒ 仍可检索」。
            if deleted and survivor is not None:
                # 先摘掉记录：index_file 见到记录会幂等跳过，那样等于没重建
                remove_file_md5_hex_from_file(md5_hex)
                result = self.index_file(survivor)
                if result["ok"]:
                    print(f"已改由副本{os.path.basename(survivor)}承载该内容的向量")
                else:
                    # 重建失败：把记录放回，让自检按 missing_vectors 自动重试
                    add_file_md5_hex_to_file(md5_hex)
                    print(f"以副本{os.path.basename(survivor)}重建向量失败: {result['error']}")
