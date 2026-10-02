"""FastAPI 后端，为 Vue 前端提供对话与知识库 API，含账户系统。"""
import os
import json
import time
import uuid
import asyncio
import itertools
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header, Request

import anyio
import psycopg
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from main_deep_agent import MainDeepAgent, PG_CHECKPOINT_POOL_MAX
from rag.vector_store import get_vector_store
from rag.index_health import IndexHealth
from utils.file_tool import list_files_with_allowed_type, get_file_md5_hex, read_md5_set
from utils.config_loader import vector_db_config
from utils.path_tool import get_abs_path
from utils.pg_lock import SingleInstanceGuard, exclusive_section, shared_section
from auth import (
    PG_CONN,
    PG_POOL_MAX,
    init_db,
    register,
    login,
    check_rate,
    verify_token,
    get_user_cached,
    list_conversations,
    create_conversation,
    update_conversation_title,
    append_message,
    delete_conversation,
)

# ---------- 并发与部署参数 ----------
# 线程池上限：同步端点与 SSE 流式生成器都占用它，每个活跃对话流约占用 1 个线程，
# 因此该值应不小于目标并发流数（anyio 默认仅 40，是单进程并发的真实天花板）
THREADPOOL_SIZE = int(os.getenv("THREADPOOL_SIZE", "200"))
# 工作进程数：>1 时需核算 PG 连接总量（见启动检查），并依赖咨询锁保证定时任务单实例
UVICORN_WORKERS = int(os.getenv("UVICORN_WORKERS", "1"))
# 反向代理地址白名单：只有来自这些地址的 X-Forwarded-For 才会被信任
FORWARDED_ALLOW_IPS = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
# 交互式文档 /docs、/redoc 与 /openapi.json 默认关闭：公网可达会暴露完整接口清单与数据模型
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in ("1", "true", "yes")

# 输入边界：超限由 Pydantic 直接拒绝（422），不会打到模型上下文、也不会撞数据库列宽变成 500
CHAT_MESSAGE_MAX_CHARS = int(os.getenv("CHAT_MESSAGE_MAX_CHARS", "8000"))
CHAT_THREAD_ID_MAX_CHARS = 128
CONV_TITLE_MAX_CHARS = 255   # 与 conversations.title VARCHAR(255) 对齐
MESSAGE_STATUS_VALUES = ("streaming", "completed", "error")

# 对话接口按用户限流：单次调用会触发多轮 LLM 与外部 API，不限流时可被单账号刷额度与线程池
CHAT_RATE_LIMIT = int(os.getenv("CHAT_RATE_LIMIT", "30"))
CHAT_RATE_WINDOW_SECONDS = int(os.getenv("CHAT_RATE_WINDOW_SECONDS", "60"))
# 单用户并发对话上限：每个活跃流占 1 个线程池线程，只限频率挡不住同时开多个流。
# 计数在进程内（UVICORN_WORKERS=1 时精确；多 worker 时各进程独立计数，属近似值）
MAX_CONCURRENT_CHAT_PER_USER = int(os.getenv("MAX_CONCURRENT_CHAT_PER_USER", "3"))
# 单个流的最长存活时间（秒）：兜底清理客户端断开时未及释放的槽位
CHAT_SLOT_MAX_AGE_SECONDS = int(os.getenv("CHAT_SLOT_MAX_AGE_SECONDS", "1800"))

# 注：JWT_SECRET 缺失由 auth.py 在导入时直接拒绝启动（含单进程），此处无需重复校验。


def _warn_connection_budget() -> None:
    """启动时核算 PG 连接需求，超出上限时明确告警（多 worker 时最容易踩）。"""
    per_worker = PG_CHECKPOINT_POOL_MAX + PG_POOL_MAX
    need = UVICORN_WORKERS * per_worker
    try:
        with psycopg.connect(PG_CONN) as conn, conn.cursor() as cur:
            cur.execute("SHOW max_connections")
            max_conn = int(cur.fetchone()[0])
    except Exception as e:
        print(f"[启动检查] 无法读取 PG max_connections，跳过连接预算核算: {e}")
        return
    detail = f"workers={UVICORN_WORKERS} × (检查点池 {PG_CHECKPOINT_POOL_MAX} + 业务池 {PG_POOL_MAX}) = {need}"
    if need > max_conn - 5:
        print(f"[启动检查] 警告：PG 连接需求约 {need} 已逼近 max_connections={max_conn}（{detail}），"
              f"请调大 PG max_connections 或调小池上限，否则高并发下会出现连接等待")
    else:
        print(f"[启动检查] PG 连接需求约 {need}，max_connections={max_conn}，余量充足（{detail}）")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 必须在事件循环内设置，anyio 的默认线程池限流器随循环创建
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    print(f"[启动检查] 线程池上限 {THREADPOOL_SIZE}，工作进程数 {UVICORN_WORKERS}")
    _warn_connection_budget()
    task = asyncio.create_task(_scheduled_health_loop())
    yield
    task.cancel()


app = FastAPI(
    title="浅问深答 API",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

agent = MainDeepAgent()
vector_store = get_vector_store()
index_health = IndexHealth()

knowledge_dir = get_abs_path(vector_db_config["knowledge_dir"])
allowed_types = vector_db_config["allowed_types"]

# 单文件上传大小上限（MB）：上传按块写入并即时校验，避免超大文件整块读入内存
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

# 启动时初始化数据库
init_db()


# ---------- 索引健康自检 + 自愈（后台定时执行，发现问题即自动修复） ----------

_health_report: dict | None = None
_health_report_at: float = 0.0

# 健康报告缓存的时效（秒）：多 worker 下各进程各存一份，若不设时效，会出现
# 某个 worker 一直返回几小时前的旧报告、另一个 worker 现算一份新的。
# 超过该时长即视为过期并重新自检，把各 worker 之间的口径差异限制在该时长内。
HEALTH_REPORT_TTL_SECONDS = int(os.getenv("HEALTH_REPORT_TTL_SECONDS", "120"))


def _set_health_report(report: dict) -> None:
    """更新进程内报告缓存并刷新时间戳。"""
    global _health_report, _health_report_at
    _health_report = report
    _health_report_at = time.monotonic()


# 自检/自愈的单实例咨询锁键，以及非持有者的锁竞争轮询间隔（秒）
_PG_LOCK_KEY_HEALTH = 0x64656570
_HEALTH_LEADER_POLL_SECONDS = 60

# 索引写操作的跨进程互斥锁键：上传入库、删除文件、构建、修复都会写 Milvus 与 md5.txt，
# 而 VectorStore._store_lock / md5_lock / IndexHealth._lock 全是进程内锁，多 worker 下
# 不同进程会并发写、互相覆盖，必须再套一层 PG 咨询锁。
# 注：持锁期间额外占用一个 PG 连接（与业务连接池相互独立）。
_PG_LOCK_KEY_INDEX_WRITE = 0x64656572

# 等待索引写锁的上限（秒）：持锁段内含外部 embedding 调用，若某个持锁者卡死，
# 无限等待会让其余写请求各自占住一个线程池线程与一条 PG 连接，最终拖垮整站。
# 超时后请求快速失败（503），把压力交还给客户端重试。
INDEX_WRITE_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_WRITE_LOCK_TIMEOUT_SECONDS", "300"))


def _index_write_section():
    """索引写操作的跨进程互斥段（写入方使用，带等待上限）。"""
    return exclusive_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE, INDEX_WRITE_LOCK_TIMEOUT_SECONDS)


def _replace_and_index(tmp_path: str, dest: str, overwritten: bool) -> dict:
    """原子替换 + 入库，整段持有索引写锁。"""
    with _index_write_section():
        if overwritten:
            vector_store.delete_file(dest)
        os.replace(tmp_path, dest)
        return vector_store.index_file(dest)


def _auto_heal_report() -> dict:
    """索引自愈闭环：自检发现问题 → 自动修复 → 复查，直至自检通过或轮次耗尽。

    停止条件：report['healthy'] 为 True。Milvus 不可达或修复异常时立即终止，
    等待下一轮自检再试。
    """
    # 整段（自检 + 修复 + 复查）持有索引写锁，与手动的修复/上传/构建互斥
    with _index_write_section():
        max_rounds = int(vector_db_config.get("health_auto_repair_max_rounds", 3))
        report = index_health.check()
        for _ in range(max_rounds):
            if report.get("healthy"):
                break
            if report.get("milvus_error"):
                print("[索引自愈] Milvus 不可达，跳过修复")
                break
            try:
                # repair() 内部自带一次全新 check，返回修复摘要 + 修复后报告
                report = index_health.repair()
                repair = report.get("repair") or {}
                print(
                    f"[索引自愈] 修复执行完成: 清理幽灵向量 {len(repair.get('ghost_deleted', []))}，"
                    f"重建记录 {repair.get('md5_rebuilt')}，重索引 {len(repair.get('re_indexed', []))}，"
                    f"重索引失败 {len(repair.get('re_index_failures', []))}"
                )
            except Exception as e:
                print(f"[索引自愈] 修复执行失败: {e}")
                break
        return report


async def _scheduled_health_loop():
    """定时自检 + 自愈。

    多 worker/多实例下必须只由一个进程执行，否则会重复修复、重复触发嵌入调用；
    用 PG 会话级咨询锁选主：只有抢到锁的进程执行，持有者退出后其他进程自动接管。
    """
    interval = max(int(vector_db_config.get("health_check_interval_hours", 6) * 3600), 600)
    guard = SingleInstanceGuard(PG_CONN, _PG_LOCK_KEY_HEALTH)
    next_run = 0.0
    was_leader = False
    while True:
        # 每轮都重新核验持锁状态，不能只在"还不是 leader"时才抢锁：
        # leader 的专用连接若被 PG 重启/网络抖动静默掐断，服务端已释放咨询锁、
        # 别的 worker 可能已接管，本进程必须立即让位，否则会出现两个 leader 同时自愈。
        # try_acquire 在已持有时会做一次 SELECT 1 往返来暴露这种静默断开。
        is_leader = await asyncio.to_thread(guard.try_acquire)
        if is_leader != was_leader:
            print(
                "[索引自检] 本进程已成为自检/自愈执行者" if is_leader
                else "[索引自检] 已失去执行者身份（连接中断或锁被接管），暂停自检"
            )
            was_leader = is_leader
        if not is_leader or time.monotonic() < next_run:
            await asyncio.sleep(_HEALTH_LEADER_POLL_SECONDS)
            continue
        try:
            report = await asyncio.to_thread(_auto_heal_report)
            _set_health_report(report)
            issues = len(report["issues"])
            print(f"[索引自检] {'健康' if report['healthy'] else f'发现 {issues} 类问题'}（文件 {report['stats']['files']}，向量块 {report['stats']['chunks']}）")
        except Exception as e:
            print(f"[索引自检] 执行失败: {e}")
        next_run = time.monotonic() + interval


# ---------- 请求模型 ----------

class ChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MESSAGE_MAX_CHARS)
    thread_id: str = Field(max_length=CHAT_THREAD_ID_MAX_CHARS)
    use_online_search: bool = False


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新对话", max_length=CONV_TITLE_MAX_CHARS)


class UpdateTitleRequest(BaseModel):
    title: str = Field(max_length=CONV_TITLE_MAX_CHARS)


class AppendMessageRequest(BaseModel):
    role: str
    content: str
    status: str | None = None


# ---------- 认证依赖 ----------

def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization header 中解析 JWT，验签后复核账号存续与角色。

    复核结果带短 TTL 缓存（USER_CACHE_TTL_SECONDS），避免每个请求都查库；
    代价是删号/降权的最长生效延迟等于该 TTL。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:]
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user = get_user_cached(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="账号不存在或已被注销")
    return {"user_id": user["id"], "username": user["username"], "role": user["role"]}


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """仅允许管理员访问。"""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    return user


# ---------- 健康检查 ----------

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------- 账户 API ----------

@app.post("/api/auth/register")
def api_register(req: RegisterRequest, request: Request):
    client_ip = request.client.host if request.client else None
    result = register(req.username, req.password, client_ip)
    if not result["ok"]:
        # 限流用 429，与"参数不合法"的 400 区分开
        raise HTTPException(
            status_code=429 if result.get("rate_limited") else 400, detail=result["error"]
        )
    return {"message": "注册成功"}


@app.post("/api/auth/login")
def api_login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else None
    result = login(req.username, req.password, client_ip)
    if not result["ok"]:
        # 限流用 429，与"用户名或密码错误"的 401 区分开
        raise HTTPException(
            status_code=429 if result.get("rate_limited") else 401, detail=result["error"]
        )
    return {"token": result["token"], "user": result["user"]}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(get_current_user)):
    return {"user": {"id": user["user_id"], "username": user["username"], "role": user["role"]}}


# ---------- 会话管理 API（需登录） ----------

@app.get("/api/conversations")
def api_list_conversations(user: dict = Depends(get_current_user)):
    return {"conversations": list_conversations(user["user_id"])}


@app.post("/api/conversations")
def api_create_conversation(req: CreateConversationRequest, user: dict = Depends(get_current_user)):
    # 会话 id 一律由服务端生成：客户端不再能指定，既避免非法/他人 id 撞列宽或唯一约束，
    # 也消除"用创建接口探测某个会话 id 是否存在"的信息泄露面
    conv_id = str(uuid.uuid4())
    if not create_conversation(user["user_id"], conv_id, req.title):
        raise HTTPException(status_code=500, detail="创建会话失败，请稍后重试")
    return {"id": conv_id, "title": req.title, "messages": []}


@app.patch("/api/conversations/{conv_id}")
def api_update_title(conv_id: str, req: UpdateTitleRequest, user: dict = Depends(get_current_user)):
    if not update_conversation_title(user["user_id"], conv_id, req.title):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}


@app.post("/api/conversations/{conv_id}/messages")
def api_append_message(conv_id: str, req: AppendMessageRequest, user: dict = Depends(get_current_user)):
    if req.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="非法的消息角色")
    # status 直接落库（messages.status VARCHAR(20)），不校验会让任意值/超长值进入或触发 500
    if req.status is not None and req.status not in MESSAGE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="非法的消息状态")
    if not append_message(user["user_id"], conv_id, req.role, req.content, req.status):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}


@app.delete("/api/conversations/{conv_id}")
def api_delete_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    thread_id = f"{user['user_id']}:{conv_id}"
    # 先清理 LangGraph 检查点（瞬时故障自动重试 3 次）；
    # 仍失败则中止删除并返回 500，会话与检查点均保留，避免"会话已删但检查点残留"
    last_err = None
    for attempt in range(3):
        try:
            agent.checkpointer.delete_thread(thread_id)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"清理检查点失败 thread={thread_id}（第{attempt + 1}次）: {e}")
            time.sleep(0.5)
    if last_err is not None:
        raise HTTPException(status_code=500, detail="清理会话上下文失败，请稍后重试")
    if not delete_conversation(user["user_id"], conv_id):
        # 会话不存在：删除不存在的检查点线程为幂等操作，直接返回 404
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}


# ---------- 对话 API（需登录） ----------

# 单用户并发对话槽位：user_id -> {token: 起始时刻}
_chat_slots: dict[int, dict[int, float]] = {}
_chat_slot_seq = itertools.count(1)
_chat_slots_lock = threading.Lock()


def _acquire_chat_slot(user_id: int) -> int | None:
    """占用一个对话并发槽，返回槽位 token；已达上限返回 None。

    顺带清理超过 CHAT_SLOT_MAX_AGE_SECONDS 的槽位：客户端断开时生成器未必能走到
    finally，靠这一步兜底，避免槽位泄漏后该用户被永久限流。
    """
    now = time.monotonic()
    with _chat_slots_lock:
        slots = {
            token: started for token, started in _chat_slots.get(user_id, {}).items()
            if now - started < CHAT_SLOT_MAX_AGE_SECONDS
        }
        if len(slots) >= MAX_CONCURRENT_CHAT_PER_USER:
            _chat_slots[user_id] = slots
            return None
        token = next(_chat_slot_seq)
        slots[token] = now
        _chat_slots[user_id] = slots
        return token


def _release_chat_slot(user_id: int, token: int) -> None:
    with _chat_slots_lock:
        slots = _chat_slots.get(user_id)
        if not slots:
            return
        slots.pop(token, None)
        if not slots:
            _chat_slots.pop(user_id, None)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    # 按用户限流：单次调用会触发多轮 LLM 与外部 API，无限制时可被单账号刷额度与线程池
    if not await asyncio.to_thread(
        check_rate, "chat", str(user["user_id"]), CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    slot = _acquire_chat_slot(user["user_id"])
    if slot is None:
        raise HTTPException(status_code=429, detail="并发回答数过多，请等待当前回答完成")

    def generate():
        try:
            for chunk in agent.ask_deep_agent(req.message, f"{user['user_id']}:{req.thread_id}", use_online_search=req.use_online_search):
                yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 异常细节只落服务端日志：直接回传会泄露内部主机名、连接信息与上游错误原文
            print(f"[对话流] thread={user['user_id']}:{req.thread_id} 处理失败: {e}")
            yield f"data: {json.dumps({'error': '服务处理异常，请稍后重试'}, ensure_ascii=False)}\n\n"
        finally:
            _release_chat_slot(user["user_id"], slot)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------- 知识库 API（仅管理员） ----------

@app.get("/api/files")
def list_files(user: dict = Depends(require_admin)):
    os.makedirs(knowledge_dir, exist_ok=True)
    files = list_files_with_allowed_type(knowledge_dir, tuple(allowed_types))
    # md5.txt 只读一次按集合比对，避免逐文件重新扫描整个记录文件
    indexed_set = read_md5_set()
    items = []
    for f in files:
        # MD5 按 (大小, mtime) 命中缓存，未改动的文件不会重复整读
        md5_hex = get_file_md5_hex(f)
        indexed = bool(md5_hex) and md5_hex in indexed_set
        items.append({"name": os.path.basename(f), "path": f, "indexed": indexed})
    return {"files": items}


async def _write_upload_to(f: UploadFile, tmp_path: str) -> int:
    """分块写入上传内容并累计大小，超过上限立即中断，避免整文件读入内存。"""
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(tmp_path, "wb") as fp:
        while chunk := await f.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"文件超过大小上限 {MAX_UPLOAD_MB} MB")
            fp.write(chunk)
    return size


@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...), user: dict = Depends(require_admin)):
    os.makedirs(knowledge_dir, exist_ok=True)
    uploaded = []
    errors = []
    for f in files:
        name = os.path.basename(f.filename or "")
        if not name or name.startswith("."):
            errors.append({"name": name or "(无名文件)", "error": "无效的文件名"})
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in allowed_types:
            errors.append({"name": name, "error": f"不支持的文件类型: {ext}"})
            continue
        try:
            dest = os.path.join(knowledge_dir, name)
            overwritten = os.path.exists(dest)
            # 先分块写临时文件（无扩展名，不会被扫描入库，且边写边校验大小上限），
            # 成功后再删旧向量并原子替换。
            # 写 tmp 或删向量失败：旧文件/旧向量/旧记录均不受影响；
            # 原子替换失败：磁盘旧文件保留，向量与记录已清理，处于干净的
            # 「未入库」状态（非场景1/3 异态），重新构建即可恢复
            tmp_path = os.path.join(knowledge_dir, f".tmp_{uuid.uuid4().hex}")
            try:
                await _write_upload_to(f, tmp_path)
                # 删旧向量 → 原子替换 → 入库闭环（写入后当场校验，失败会删向量重建复验），
                # 整段持索引写锁：多 worker 下别的进程可能正在修复/构建，不加锁会并发写向量与 md5.txt。
                # 该段含外部嵌入调用与向量写入，阻塞时间长，必须放线程执行，
                # 否则上传期间会冻结所有用户的流式回答
                index_result = await asyncio.to_thread(_replace_and_index, tmp_path, dest, overwritten)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            uploaded.append({
                "name": name,
                "overwritten": overwritten,
                "indexed": index_result["ok"],
                "error": None if index_result["ok"] else index_result["error"],
            })
        except TimeoutError as e:
            # 索引写锁等待超时：别的进程正持锁做长操作，别把这个请求挂死
            print(f"[知识库] 上传文件 {name} 等待索引写锁超时: {e}")
            errors.append({"name": name, "error": "索引正被其他操作占用，请稍后重试"})
        except Exception as e:
            # 预期外异常的原文只写日志（可能含绝对路径、Milvus 连接信息）；
            # 文件级入库失败原因已在 index_result["error"] 中分类返回
            print(f"[知识库] 上传文件 {name} 失败: {e}")
            errors.append({"name": name, "error": "上传失败，详见服务端日志"})
    return {"uploaded": uploaded, "errors": errors}


@app.delete("/api/files/{filename}")
def delete_file(filename: str, user: dict = Depends(require_admin)):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(knowledge_dir, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        with _index_write_section():
            # 先取 md5 再删磁盘文件：删完就取不到了，而删向量与记录时都需要它。
            # 顺序也很关键——先删文件，失败时向量与记录都还没动，状态完全未变；
            # 若反过来先删向量与记录，os.remove 失败（Windows 下文件被占用）就会留下
            # 「文件还在、却已未入库」——这既不是 missing_vectors 也不是明明缺记录，
            # 自检不会认、也不会修，提示却写着"删除失败"，对管理员是误导。
            md5_hex = get_file_md5_hex(file_path)
            os.remove(file_path)
            # 文件已删；此处若失败只会留下 ghost 向量（磁盘无对应文件），
            # 属于自检能识别并按 ghost_vectors 清理的状态
            vector_store.delete_file(file_path, md5_hex)
        return {"message": f"已删除: {safe_name}"}
    except TimeoutError as e:
        print(f"[知识库] 删除文件 {safe_name} 等待索引写锁超时: {e}")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        # 异常细节只落服务端日志：直接回传会泄露 Milvus 连接串、主机名与上游错误原文
        if os.path.exists(file_path):
            # 磁盘文件还在，说明失败在"删文件"这一步，索引侧尚未改动
            print(f"[知识库] 删除磁盘文件 {safe_name} 失败（向量与入库记录未改动）: {e}")
            raise HTTPException(status_code=500, detail="删除文件失败，请确认文件未被其他程序占用后重试")
        print(f"[知识库] 删除文件 {safe_name} 后清理向量/记录失败: {e}")
        raise HTTPException(status_code=500, detail="文件已删除，但向量/入库记录清理失败，请稍后执行一次「修复」")


@app.post("/api/vector/build")
def build_vector(user: dict = Depends(require_admin)):
    try:
        with _index_write_section():
            failures = vector_store.do_store()
    except TimeoutError as e:
        print(f"[知识库] 构建向量库等待索引写锁超时: {e}")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        print(f"[知识库] 构建向量库失败: {e}")
        raise HTTPException(status_code=500, detail="构建失败，请稍后重试")
    if failures:
        return {
            "message": f"构建完成，但有 {len(failures)} 个文件失败",
            "failures": failures,
        }
    return {"message": "构建完成"}


@app.get("/api/index/health")
def get_index_health(refresh: bool = False, user: dict = Depends(require_admin)):
    """返回最近一次自检报告；refresh=1 时立即执行一次全新检查。"""
    if refresh or _health_report is None or time.monotonic() - _health_report_at > HEALTH_REPORT_TTL_SECONDS:
        try:
            # 只读自检用共享锁：与正在进行的写（上传/删除/构建/修复）互斥，
            # 避免读到写了一半的状态；多个并发自检彼此不阻塞
            with shared_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE):
                _set_health_report(index_health.check())
        except Exception as e:
            print(f"[索引健康] 自检失败: {e}")
            raise HTTPException(status_code=500, detail="自检失败，请稍后重试")
    return _health_report


@app.post("/api/index/health/repair")
def repair_index_health(user: dict = Depends(require_admin)):
    """手动触发一致性修复：清理残留向量、重建入库记录、重索引向量丢失的文件。"""
    try:
        with _index_write_section():
            _set_health_report(index_health.repair())
    except TimeoutError as e:
        print(f"[索引健康] 修复等待索引写锁超时: {e}")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        print(f"[索引健康] 修复失败: {e}")
        raise HTTPException(status_code=500, detail="修复失败，请稍后重试")
    return _health_report


if __name__ == "__main__":
    import uvicorn
    # 生产不使用 reload（文件监听会额外起进程并拖慢吞吐）。
    # workers>1 时每个进程各自持有连接池，启动检查会核算 PG 连接总量；
    # proxy_headers 让后端从反向代理拿到真实客户端 IP，否则限流会把所有用户算作同一来源。
    uvicorn.run(
        "api_server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=UVICORN_WORKERS,
        proxy_headers=True,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
    )