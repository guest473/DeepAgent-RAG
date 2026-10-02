"""索引一致性健康检查与修复。

协调三份数据的一致性：
- md5.txt          入库记录（文件 MD5 一行一个）
- knowledge 目录   源文件
- Milvus           向量数据（chunk 的 source 元数据 = 文件绝对路径）

覆盖三类故障：
1. md5.txt 损坏（knowledge、Milvus 正常）→ 以 Milvus 为准重建入库记录
2. knowledge 文件丢失（md5.txt、Milvus 正常）→ 清理残留向量 + 剔除孤立记录
3. Milvus 向量丢失（md5.txt、knowledge 正常）→ 剔除记录并重新入库

判定基准是**内容**（md5）而非路径：同内容文件按内容去重、只入库一次，
因此只要该内容的任意一个文件带向量即视为已入库，与前端 /api/files 的口径一致。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime

from rag.vector_store import get_vector_store
from utils.file_tool import list_files_with_allowed_type, get_file_md5_hex, md5_lock
from utils.config_loader import vector_db_config
from utils.path_tool import get_abs_path

# Milvus 单次 query 默认上限附近，分页拉取全部 chunk 的 source
# 游标迭代器的每批条数：Milvus 的 query() 有 offset + limit ≤ 16384 的硬上限，
# 迭代器（query_iterator）不受该限制，故不按页大小 + offset 翻页
_ITER_BATCH_SIZE = 1000


def _md5_txt_path() -> str:
    return get_abs_path("md5.txt")


def _read_md5_set() -> set:
    path = _md5_txt_path()
    with md5_lock:
        if not os.path.exists(path):
            return set()
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}


def _write_md5_set(hashes: set) -> None:
    with md5_lock:
        path = _md5_txt_path()
        # 原子替换：先写临时文件再 rename，避免中途失败截断损坏 md5.txt
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for h in sorted(hashes):
                f.write(f"{h}\n")
        os.replace(tmp_path, path)


def _milvus_source_counts(vector_store) -> dict:
    """返回 Milvus 中 {source绝对路径: chunk数}，连接失败或查询失败时抛出异常。

    用 MilvusClient.query_iterator 游标翻页；不用 query() + 递增 offset，
    因为 query() 有 offset + limit ≤ 16384 的硬上限，数据量涨上去会直接报错
    （invalid max query result window），导致整个自检/自愈抛异常。
    """
    vs = vector_store.vector_store
    client = vs.client  # pymilvus.MilvusClient（query_iterator/delete 均基于它，版本稳定）
    counts: dict = {}
    iterator = client.query_iterator(
        collection_name=vs.collection_name,
        batch_size=_ITER_BATCH_SIZE,
        filter='source != ""',
        output_fields=["source"],
    )
    try:
        while True:
            rows = iterator.next()
            if not rows:
                break
            for row in rows:
                counts[row["source"]] = counts.get(row["source"], 0) + 1
    finally:
        iterator.close()
    return counts


class IndexHealth:
    """索引健康检查与修复（线程安全，check/repair 串行执行）。"""

    def __init__(self):
        self.vector_store = get_vector_store()
        self.knowledge_dir = get_abs_path(vector_db_config["knowledge_dir"])
        self.allowed_types = tuple(vector_db_config["allowed_types"])
        self._lock = threading.RLock()

    def _disk_files(self) -> list:
        os.makedirs(self.knowledge_dir, exist_ok=True)
        return list(list_files_with_allowed_type(self.knowledge_dir, self.allowed_types))

    def _base_report(self) -> dict:
        return {
            "checked_at": None,
            "healthy": True,
            "milvus_error": None,
            "stats": {"files": 0, "indexed_files": 0, "chunks": 0},
            "issues": [],
        }

    def check(self) -> dict:
        """只读自检，返回健康报告。不会修改任何数据。"""
        with self._lock:
            report = self._base_report()
            report["checked_at"] = datetime.now().isoformat(timespec="seconds")

            disk_files = [get_abs_path(f) for f in self._disk_files()]
            md5_set = _read_md5_set()
            report["stats"]["files"] = len(disk_files)

            try:
                source_counts = _milvus_source_counts(self.vector_store)
            except Exception as e:
                report["healthy"] = False
                # 原始异常可能是连接串 / 主机名 / 上游原文，只写服务端日志；
                # 报告体由接口原样返回，不能带上这些内部信息
                print(f"[索引健康] 连接 Milvus 失败: {e}")
                report["milvus_error"] = "无法连接 Milvus，详情见服务端日志"
                report["issues"].append({
                    "type": "milvus_unreachable", "severity": "error",
                    "title": "无法连接 Milvus", "detail": "无法连接 Milvus，详情见服务端日志",
                    "count": 0, "items": [],
                })
                return report

            milvus_by_norm = {os.path.normcase(s): s for s in source_counts}
            report["stats"]["chunks"] = sum(source_counts.values())

            # 磁盘文件：normcase路径 -> (原路径, md5)
            disk_by_norm: dict = {}
            for f in disk_files:
                disk_by_norm[os.path.normcase(f)] = (f, get_file_md5_hex(f))

            # 内容级视角：同内容文件按内容去重（只入库一次），因此判定基准是"内容"而非"路径"
            files_by_md5: dict = {}
            for _, (path, md5) in disk_by_norm.items():
                if md5:
                    files_by_md5.setdefault(md5, []).append(path)
            disk_hashes = set(files_by_md5)
            # 某内容只要有任意一个同内容文件带向量，该内容即视为已入库
            backed_hashes = {
                md5 for norm, (_, md5) in disk_by_norm.items()
                if md5 and norm in milvus_by_norm
            }

            # 场景2（Milvus侧）：有向量但磁盘文件已丢失 → 残留向量
            ghost = [s for s in source_counts if os.path.normcase(s) not in disk_by_norm]
            if ghost:
                report["healthy"] = False
                report["issues"].append({
                    "type": "ghost_vectors", "severity": "warn",
                    "title": "向量库存在残留向量",
                    "detail": "对应磁盘文件已丢失；若该内容在磁盘上仍有同内容副本，修复会改用副本重新入库",
                    "count": len(ghost), "items": ghost,
                })

            # 场景2（md5侧）：md5.txt 中的记录在磁盘上已无对应文件
            orphan_hashes = [h for h in md5_set if h not in disk_hashes]
            if orphan_hashes:
                report["healthy"] = False
                report["issues"].append({
                    "type": "orphan_md5", "severity": "warn",
                    "title": "入库记录存在孤立条目", "detail": "对应文件已丢失",
                    "count": len(orphan_hashes), "items": [h[:12] + "..." for h in orphan_hashes],
                })

            # 场景1（内容级）：向量库已有该内容，但入库记录缺失 → 界面误标「未入库」，重建会重复入库
            missing_record = [
                p for h in (backed_hashes - md5_set) for p in files_by_md5[h]
            ]

            # 场景3（内容级）：有入库记录但向量库里没有该内容的向量 → 检索静默漏查
            missing_vectors = [
                p for h in (md5_set - backed_hashes) if h in files_by_md5 for p in files_by_md5[h]
            ]

            # 已入库文件数：内容已被记录即视为已入库，与前端 /api/files 的口径保持一致
            report["stats"]["indexed_files"] = sum(
                1 for _, (_, md5) in disk_by_norm.items() if md5 and md5 in md5_set
            )

            if missing_record:
                report["healthy"] = False
                report["issues"].append({
                    "type": "missing_md5_record", "severity": "warn",
                    "title": "向量已存在但入库记录丢失", "detail": "界面会误标「未入库」，重建将产生重复向量",
                    "count": len(missing_record), "items": missing_record,
                })
            if missing_vectors:
                report["healthy"] = False
                report["issues"].append({
                    "type": "missing_vectors", "severity": "error",
                    "title": "入库记录存在但向量丢失", "detail": "该内容在向量库中没有向量，检索查不到对应文件",
                    "count": len(missing_vectors), "items": missing_vectors,
                })
            return report

    def repair(self) -> dict:
        """按安全顺序修复三类不一致，返回修复摘要 + 修复后的健康报告。

        修复动作：清理残留向量 → 以 Milvus∩磁盘 重建 md5.txt（记录按内容去重）
        → 为“记录存在但向量丢失”的内容重新入库一个代表文件。
        新上传但从未构建的文件不属于异常，不做处理。
        """
        with self._lock:
            disk_files = [get_abs_path(f) for f in self._disk_files()]
            disk_by_norm = {os.path.normcase(f): (f, get_file_md5_hex(f)) for f in disk_files}
            md5_set_before = _read_md5_set()

            try:
                source_counts = _milvus_source_counts(self.vector_store)
            except Exception as e:
                raise RuntimeError(f"无法连接 Milvus，修复终止: {e}")

            summary = {"ghost_deleted": [], "ghost_errors": [], "md5_rebuilt": False, "re_indexed": [], "re_index_failures": []}

            # 1. 清理幽灵向量（场景2）
            for src in source_counts:
                if os.path.normcase(src) in disk_by_norm:
                    continue
                try:
                    # 走 VectorStore 的分页删除：单次 get_pks/delete 只处理一页，
                    # 大文件的残留向量会删不干净
                    self.vector_store.delete_by_source(src)
                    summary["ghost_deleted"].append(src)
                except Exception as e:
                    # 该摘要随接口原样返回，异常原文（含 Milvus 内部信息）只写日志
                    print(f"[索引健康] 清理残留向量失败 {src}: {e}")
                    summary["ghost_errors"].append(f"{src}: 删除失败，详情见服务端日志")

            # 2. 以 Milvus 为准重建 md5.txt（场景1 + 剔除场景2孤立记录）
            try:
                source_counts = _milvus_source_counts(self.vector_store)
            except Exception as e:
                raise RuntimeError(f"清理后复查 Milvus 失败，修复终止: {e}")
            milvus_by_norm = {os.path.normcase(s) for s in source_counts}
            keep_hashes = {
                md5 for norm, (_, md5) in disk_by_norm.items()
                if md5 and norm in milvus_by_norm
            }
            _write_md5_set(keep_hashes)
            summary["md5_rebuilt"] = True

            # 3. 重索引“记录存在但向量丢失”的内容（场景3）
            #    内容级去重：每个缺失内容只需入库一个代表文件，其余同内容副本共享该结果
            backed_after = {
                md5 for norm, (_, md5) in disk_by_norm.items()
                if md5 and norm in milvus_by_norm
            }
            to_reindex: list = []
            seen_hashes: set = set()
            for norm, (path, md5) in disk_by_norm.items():
                if md5 and md5 in md5_set_before and md5 not in backed_after and md5 not in seen_hashes:
                    to_reindex.append(path)
                    seen_hashes.add(md5)
            if to_reindex:
                failures = self.vector_store.do_store(to_reindex)
                failed_files = {item["file"] for item in failures}
                summary["re_indexed"] = [p for p in to_reindex if p not in failed_files]
                summary["re_index_failures"] = failures
                # 这些内容的记录已在步骤 2 被清掉；重索引失败就把记录补回去，
                # 让 check() 判为 missing_vectors 并在下一轮自检继续重试。
                # 否则会静默停在「未入库」——既非 missing_vectors 也非 missing_md5_record，
                # 自检认为一切健康、永远不再重试，只能靠管理员手动补建。
                failed_hashes = {
                    disk_by_norm[os.path.normcase(p)][1] for p in failed_files
                    if os.path.normcase(p) in disk_by_norm
                }
                failed_hashes.discard(None)
                if failed_hashes:
                    _write_md5_set(_read_md5_set() | failed_hashes)

            report = self.check()
            report["repair"] = summary
            return report