"""PostgreSQL 咨询锁工具：多进程/多实例部署下的跨进程互斥。

- `exclusive_section`：阻塞式互斥，用于启动期一次性初始化（如建表）。
- `SingleInstanceGuard`：会话级锁，让定时任务在多实例中只由一个执行。

进程退出或连接断开时会话级锁自动释放，其他实例会在下一次尝试时接管。
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg

# 启动期 DDL 串行化锁键：建检查点表（main_deep_agent）与建业务表（auth.init_db）共用，
# 多 worker 同时启动时避免并发 DDL 撞车
PG_LOCK_KEY_SETUP = 0x64656571


@contextmanager
def exclusive_section(conninfo: str, lock_key: int, timeout_seconds: float | None = None):
    """阻塞式互斥：同一时刻只有一个持有者能进入该段。

    `timeout_seconds` 不为 None 时用 `lock_timeout` 限制等待时长，超时抛 `TimeoutError`。
    持锁段内含外部 embedding 调用，一旦某个持锁者卡死，无限等待会让其余写请求各自
    占住一个线程池线程与一条 PG 连接，最终把整个服务拖垮。
    """
    conn = psycopg.connect(conninfo, autocommit=True)
    try:
        with conn.cursor() as cur:
            if timeout_seconds is None:
                cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            else:
                # 用 set_config 而不是 SET：SET 不接受参数占位符
                cur.execute(
                    "SELECT set_config('lock_timeout', %s, false)",
                    (str(int(timeout_seconds * 1000)),),
                )
                try:
                    cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
                except psycopg.errors.LockNotAvailable as exc:
                    raise TimeoutError(
                        f"等待锁 {hex(lock_key)} 超过 {timeout_seconds:g} 秒"
                    ) from exc
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
    finally:
        conn.close()


@contextmanager
def shared_section(conninfo: str, lock_key: int):
    """共享互斥：多个持有者可同时进入，但与 `exclusive_section` 互斥。

    用于只读操作：并发的读彼此不阻塞，但要与正在进行的写隔开，
    避免读到写了一半的状态。
    """
    conn = psycopg.connect(conninfo, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock_shared(%s)", (lock_key,))
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock_shared(%s)", (lock_key,))
    finally:
        conn.close()


class SingleInstanceGuard:
    """跨进程单例执行锁，基于 PG 会话级咨询锁。

    持有者崩溃或连接中断时锁自动释放，因此不需要额外的选主组件。
    """

    def __init__(self, conninfo: str, lock_key: int):
        self._conninfo = conninfo
        self._lock_key = lock_key
        self._conn: psycopg.Connection | None = None
        self._held = False

    def try_acquire(self) -> bool:
        """尝试成为执行者；已持有时先用一次轻量往返确认会话仍然有效。"""
        if self._held and self._conn is not None and not self._conn.closed:
            # 不能只凭 _held 直接返回 True：连接若被静默掐断（网络抖动、PG 重启、
            # 中间设备掐空闲连接），服务端的会话级锁已释放、别的进程可能已抢到，
            # 而本进程仍以为自己是执行者 —— 会出现两个 leader 同时跑定时任务。
            # 这里做一次往返探测，连接已死则关闭并重新竞争。
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        # 连接已断开时锁随会话释放，需要重新竞争
        self._held = False
        try:
            if self._conn is None or self._conn.closed:
                # 专用长连接：锁的存续完全跟随该会话
                self._conn = psycopg.connect(self._conninfo, autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_key,))
                self._held = bool(cur.fetchone()[0])
        except Exception:
            self._held = False
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        return self._held
