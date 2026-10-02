import os
import re
import random
import threading
import time
from datetime import datetime, timedelta, timezone

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from dotenv import load_dotenv

from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section

load_dotenv()

PG_CONN = os.getenv("PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/deepagent_rag")
PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "2"))
# 业务池承载全部鉴权查询、限流读写与会话增删改查（均为短操作），
# 多 worker 部署时注意 workers × (本池上限 + 检查点池上限) 不能超过 PG max_connections
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "20"))
# JWT 签名密钥必须显式配置：随机兜底会导致重启后已签发 token 全部失效，
# 多 worker 下更会让各进程互不认签、用户随机掉线，均属不可接受的状态，故缺失即拒绝启动。
# 生成方式：python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "未配置 JWT_SECRET，拒绝启动。请在 .env 中写入随机长字符串，"
        '例如执行 python -c "import secrets; print(secrets.token_hex(32))" 生成'
    )
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

# 用户名长度上限，与 users.username VARCHAR(50) 对齐：超长用户名在入口直接拒绝，
# 否则插入 users 或写入 rate_limits.key 时会抛 DataError，接口变成 500
USERNAME_MAX_LENGTH = 50

# ---------- 登录安全配置 ----------
# 账号级限流：按用户名计数，抵挡针对单一账号的多 IP 爆破；不按来源 IP，避免误伤同出口的其他用户
LOGIN_ACCOUNT_RATE_LIMIT = int(os.getenv("LOGIN_ACCOUNT_RATE_LIMIT", "10"))
LOGIN_ACCOUNT_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_ACCOUNT_RATE_WINDOW_SECONDS", "60"))
# IP 级限流：仅作粗粒度兜底（防单机横扫多账号），阈值放宽，避免 NAT 共享出口被整体拒绝
LOGIN_IP_RATE_LIMIT = int(os.getenv("LOGIN_IP_RATE_LIMIT", "100"))
LOGIN_IP_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_IP_RATE_WINDOW_SECONDS", "60"))
# 单 IP 针对单账号的失败冷却：只挡该来源对该账号的尝试，其他 IP 的正常登录不受影响
LOGIN_ACCOUNT_IP_MAX_ATTEMPTS = int(os.getenv("LOGIN_ACCOUNT_IP_MAX_ATTEMPTS", "5"))
LOGIN_ACCOUNT_IP_WINDOW_SECONDS = int(os.getenv("LOGIN_ACCOUNT_IP_WINDOW_SECONDS", "900"))
# JWT 复核用户信息的缓存时长（秒）：越大越省数据库，代价是删号/降权生效延迟
USER_CACHE_TTL_SECONDS = int(os.getenv("USER_CACHE_TTL_SECONDS", "30"))
REGISTER_RATE_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "5"))          # 同一 IP 在窗口期内允许的最大注册次数
REGISTER_RATE_WINDOW_SECONDS = int(os.getenv("REGISTER_RATE_WINDOW_SECONDS", "3600"))  # 注册限流窗口（秒）

# ---------- DB helpers ----------

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    # 双重检查锁：并发首次调用时只创建一个连接池，避免竞态导致池泄漏
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=PG_CONN,
                    min_size=PG_POOL_MIN,
                    max_size=PG_POOL_MAX,
                )
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    # psycopg3 中 SELECT 也会开启事务；归还前回滚未提交的读事务，避免池打印警告
    if conn.info.transaction_status == TransactionStatus.INTRANS:
        conn.rollback()
    _get_pool().putconn(conn)


def init_db():
    """创建所需表（users、conversations、messages、rate_limits）。"""
    # 多 worker 同时启动会并发执行 DDL 而撞车，用 PG 咨询锁串行化；
    # 与检查点建表（main_deep_agent）共用同一把锁，表已存在时 DDL 极快，后续 worker 依次通过
    with exclusive_section(PG_CONN, PG_LOCK_KEY_SETUP):
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id          SERIAL PRIMARY KEY,
                        username    VARCHAR(50) UNIQUE NOT NULL,
                        password    VARCHAR(255) NOT NULL,
                        role        VARCHAR(10) NOT NULL DEFAULT 'user'
                            CHECK (role IN ('user', 'admin')),
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 清理早期"账号锁定"方案的遗留字段：该方案已废弃（爆破防护完全由三层限流承担，
                # 尝试与失败记录都在 rate_limits 表中，无需在 users 上重复计数）
                cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS locked_until")
                cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS failed_attempts")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id          VARCHAR(36) PRIMARY KEY,
                        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        title       VARCHAR(255) NOT NULL DEFAULT '新对话',
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id              SERIAL PRIMARY KEY,
                        conversation_id VARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        role            VARCHAR(10) NOT NULL CHECK (role IN ('user', 'assistant')),
                        content         TEXT NOT NULL,
                        status          VARCHAR(20),
                        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        id           BIGSERIAL PRIMARY KEY,
                        scope        VARCHAR(20) NOT NULL,
                        key          VARCHAR(128) NOT NULL,
                        attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # 兼容迁移：早期 key 为 VARCHAR(64)，装不下 login_fail_ip 的
                # "{用户名}|{IP}" 复合键（用户名 50 + "|" + IPv6 45 = 96），
                # 超长会抛 DataError 使登录返回 500
                cur.execute("ALTER TABLE rate_limits ALTER COLUMN key TYPE VARCHAR(128)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rate_limits_lookup "
                    "ON rate_limits (scope, key, attempted_at)"
                )
                # 惰性清理按 attempted_at 过滤，缺这个索引时清理会退化为全表扫描；
                # 对话接口也写入本表后（每用户可达上千行/小时）这一条尤为重要
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rate_limits_attempted_at "
                    "ON rate_limits (attempted_at)"
                )
            conn.commit()
        finally:
            _put_conn(conn)


# ---------- password ----------

_ph = PasswordHasher()


def _hash_password(password: str) -> str:
    """使用 Argon2id 生成密码哈希（自动加盐）。"""
    return _ph.hash(password)


def _verify_password(password: str, stored: str) -> bool:
    try:
        _ph.verify(stored, password)
        return True
    except VerificationError:
        return False


def _validate_password(password: str) -> str | None:
    """校验密码强度：至少 10 字符，含数字、英文字母、特殊符号。返回错误信息，通过则返回 None。"""
    problems = []
    if len(password) < 10:
        problems.append("至少10个字符")
    if not re.search(r"[0-9]", password):
        problems.append("数字")
    if not re.search(r"[a-zA-Z]", password):
        problems.append("英文字母")
    if not re.search(r"[^0-9a-zA-Z\s]", password):
        problems.append("特殊符号")
    if problems:
        return "密码需包含" + "、".join(problems)
    return None


# ---------- JWT ----------

def _create_token(user_id: int, username: str, role: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """验证 JWT，返回 payload 或 None。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def get_user_by_id(user_id: int) -> dict | None:
    """按 id 查询用户（不缓存），返回 {'id', 'username', 'role'}；不存在返回 None。"""
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, username, role FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        _put_conn(conn)


# 用户信息短 TTL 缓存：每个鉴权请求都要复核账号，直连数据库会放大连接池压力。
# 缓存后删号/降权的最长生效延迟 = USER_CACHE_TTL_SECONDS。
_user_cache: dict[int, tuple[float, dict | None]] = {}
_user_cache_lock = threading.Lock()
_USER_CACHE_MAX_ENTRIES = 20000


def get_user_cached(user_id: int) -> dict | None:
    """带短 TTL 缓存的用户查询，供 JWT 验签后的账号复核使用。"""
    now = time.monotonic()
    with _user_cache_lock:
        cached = _user_cache.get(user_id)
        if cached is not None and now - cached[0] < USER_CACHE_TTL_SECONDS:
            return cached[1]
    user = get_user_by_id(user_id)
    with _user_cache_lock:
        if len(_user_cache) >= _USER_CACHE_MAX_ENTRIES:
            # 先淘汰过期项；仍满则整体清空（TTL 很短，重建代价可控）
            expired = [k for k, v in _user_cache.items() if now - v[0] >= USER_CACHE_TTL_SECONDS]
            if expired:
                for k in expired:
                    _user_cache.pop(k, None)
            else:
                _user_cache.clear()
        _user_cache[user_id] = (now, user)
    return user


# ---------- 通用速率限制（DB 版滑动窗口，跨多进程共享，内存有界） ----------

# 惰性清理概率：每次记录后以该概率清理过期行，防止 rate_limits 表无限增长
_RATE_PURGE_PROBABILITY = 0.01
# 各 scope 的窗口最长 1 小时，保留 2 小时可安全清理窗口外记录而不误删
_RATE_RETENTION = timedelta(hours=2)


def _rate_exceeded(cur, scope: str, key: str, limit: int, window_seconds: int, now: datetime) -> bool:
    """在已有游标上判断 key 在窗口期内的记录数是否已达上限。"""
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM rate_limits "
        "WHERE scope = %s AND key = %s AND attempted_at > %s",
        (scope, key, now - timedelta(seconds=window_seconds)),
    )
    return cur.fetchone()["cnt"] >= limit


def _rate_record(cur, scope: str, key: str, now: datetime) -> None:
    """在已有游标上记录一次尝试，并惰性清理过期行。"""
    cur.execute(
        "INSERT INTO rate_limits (scope, key, attempted_at) VALUES (%s, %s, %s)",
        (scope, key, now),
    )
    if random.random() < _RATE_PURGE_PROBABILITY:
        cur.execute("DELETE FROM rate_limits WHERE attempted_at <= %s", (now - _RATE_RETENTION,))


def check_rate(scope: str, key: str, limit: int, window_seconds: int) -> bool:
    """记录并检查 key 在窗口期内的尝试次数，超限返回 False。

    以数据库为计数载体：并发安全、多进程部署下同样生效，
    且不依赖长期驻留内存的字典。
    """
    conn = _get_conn()
    try:
        now = datetime.now()
        with conn.cursor(row_factory=dict_row) as cur:
            if _rate_exceeded(cur, scope, key, limit, window_seconds, now):
                return False
            _rate_record(cur, scope, key, now)
        conn.commit()
        return True
    finally:
        _put_conn(conn)


# ---------- 业务接口 ----------

def register(username: str, password: str, ip: str | None = None) -> dict:
    """注册新用户（角色固定为 user）。返回 {'ok': True} 或 {'ok': False, 'error': ...}。"""
    username = username.strip()
    if not username or len(username) < 2:
        return {"ok": False, "error": "用户名至少 2 个字符"}
    if len(username) > USERNAME_MAX_LENGTH:
        return {"ok": False, "error": f"用户名最长 {USERNAME_MAX_LENGTH} 个字符"}
    err = _validate_password(password)
    if err:
        return {"ok": False, "error": err}

    # 注册限流：必须在 Argon2 哈希等昂贵计算之前拒绝，防止批量注册刷 CPU
    if ip and not check_rate("register", ip, REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW_SECONDS):
        return {"ok": False, "error": "注册过于频繁，请稍后再试", "rate_limited": True}

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # 唯一性交给数据库约束判断：SELECT-then-INSERT 在并发下会双双通过检查，
            # 后插入者撞 UNIQUE 约束抛异常变成 500，而不是返回"用户名已存在"
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (username, _hash_password(password), "user"),
            )
            created = cur.rowcount > 0
        conn.commit()
        if not created:
            return {"ok": False, "error": "用户名已存在"}
        return {"ok": True}
    finally:
        _put_conn(conn)


def login(username: str, password: str, ip: str | None = None) -> dict:
    """登录，返回 token 和用户信息。

    限流策略（目标：既不误伤共享出口的正常用户，也不让攻击者定向踢人）：
    - 账号级限流：按用户名计数，挡住针对单一账号的多 IP 爆破，与来源 IP 无关；
    - IP 级限流：阈值放宽，仅作粗粒度兜底，避免 NAT / 机房共享出口被整体拒绝；
    - 单 IP × 单账号失败冷却：只挡该来源对该账号的尝试，该账号在其他 IP 的正常登录不受影响。

    爆破防护完全由上述三层限流承担，不做账号级锁定：否则攻击者只要知道用户名、
    故意输错密码，就能把真实用户踢下线。
    """
    username = username.strip()
    # 超过列上限的用户名不可能对应真实账号，直接按凭据错误处理：既避免超长复合键
    # 写入 rate_limits 抛 DataError（接口变 500），也不额外暴露「账号是否存在」
    if len(username) > USERNAME_MAX_LENGTH:
        return {"ok": False, "error": "用户名或密码错误"}
    now = datetime.now()
    acct_ip_key = f"{username}|{ip}" if ip else None

    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # ---- 限流判定（同一连接内完成，不额外占用连接池）----
            checks = [
                ("login_acct", username, LOGIN_ACCOUNT_RATE_LIMIT, LOGIN_ACCOUNT_RATE_WINDOW_SECONDS),
            ]
            if ip:
                checks.append(("login_ip", ip, LOGIN_IP_RATE_LIMIT, LOGIN_IP_RATE_WINDOW_SECONDS))
            if acct_ip_key:
                checks.append(
                    ("login_fail_ip", acct_ip_key,
                     LOGIN_ACCOUNT_IP_MAX_ATTEMPTS, LOGIN_ACCOUNT_IP_WINDOW_SECONDS)
                )
            for scope, key, limit, window in checks:
                if _rate_exceeded(cur, scope, key, limit, window, now):
                    # 带 rate_limited 标记，让接口层回 429 而不是 401，
                    # 客户端与监控才能区分「密码错」和「被限流」
                    return {"ok": False, "error": "尝试过于频繁，请稍后再试", "rate_limited": True}

            # ---- 记录本次尝试 ----
            _rate_record(cur, "login_acct", username, now)
            if ip:
                _rate_record(cur, "login_ip", ip, now)

            cur.execute(
                "SELECT id, username, password, role FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return {"ok": False, "error": "用户名或密码错误"}

            if not _verify_password(password, row["password"]):
                # 失败只记入限流表（login_fail_ip），统一返回通用错误，不向外暴露账号状态
                if acct_ip_key:
                    _rate_record(cur, "login_fail_ip", acct_ip_key, now)
                conn.commit()
                return {"ok": False, "error": "用户名或密码错误"}

            # 密码正确：提交本次尝试记录（login_acct / login_ip）并签发 token
            conn.commit()
            token = _create_token(row["id"], row["username"], row["role"])
            return {
                "ok": True,
                "token": token,
                "user": {
                    "id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                },
            }
    finally:
        _put_conn(conn)


# ---------- 对话记录 ----------

def list_conversations(user_id: int) -> list[dict]:
    """返回用户所有会话（含消息），按最近更新倒序。"""
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT c.id, c.title, m.role, m.content, m.status
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id = %s
                ORDER BY c.updated_at DESC, c.id DESC, m.id ASC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        convs: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            cid = r["id"]
            if cid not in convs:
                convs[cid] = {"id": cid, "title": r["title"], "messages": []}
                order.append(cid)
            if r["role"] is not None:
                convs[cid]["messages"].append(
                    {"role": r["role"], "content": r["content"], "status": r["status"]}
                )
        return [convs[cid] for cid in order]
    finally:
        _put_conn(conn)


def create_conversation(user_id: int, conv_id: str, title: str) -> bool:
    """创建会话；id 已存在（含属于他人的会话）时返回 False，不抛异常。"""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (conv_id, user_id, title),
            )
            created = cur.rowcount > 0
        conn.commit()
        return created
    finally:
        _put_conn(conn)


def update_conversation_title(user_id: int, conv_id: str, title: str) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND user_id = %s",
                (title, conv_id, user_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        _put_conn(conn)


def append_message(user_id: int, conv_id: str, role: str, content: str, status: str | None) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
            if not cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, status) VALUES (%s, %s, %s, %s)",
                (conv_id, role, content, status),
            )
            cur.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (conv_id,),
            )
        conn.commit()
        return True
    finally:
        _put_conn(conn)


def delete_conversation(user_id: int, conv_id: str) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        _put_conn(conn)
