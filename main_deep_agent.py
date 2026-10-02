import os
import threading
from langchain.agents.middleware import SummarizationMiddleware
from langchain.chat_models import init_chat_model
from deepagents import create_deep_agent
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from utils.prompt_loader import load_main_prompt
from utils.config_loader import agent_config
from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section
from rag.rag_summary import rag_summary_subagents
from langchain_tavily import TavilySearch
from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

# Postgres 连接串（与 auth.py 保持一致，复用同一 PostgreSQL 实例）
_PG_CONNECTION_STRING = os.getenv(
    "PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/deepagent_rag"
)

# 检查点连接池大小（本进程共享）：检查点是"每次读写借还一次连接"的短操作，
# 池上限只需覆盖同时并发执行的检查点操作数，不必等于并发流数。
# 多 worker 部署时注意 workers × (本池上限 + 业务池上限) 不能超过 PG max_connections。
PG_CHECKPOINT_POOL_MIN = int(os.getenv("PG_CHECKPOINT_POOL_MIN", "2"))
PG_CHECKPOINT_POOL_MAX = int(os.getenv("PG_CHECKPOINT_POOL_MAX", "20"))

_tavily: TavilySearch | None = None
_tavily_lock: threading.Lock = threading.Lock()

# 联网搜索故障与"确实没有搜到"必须可区分：否则模型会把两者合并成"未找到相关资料"。
# 与 rag/rag_summary.py 的 RETRIEVAL_ERROR_MARKER 同一思路：工具返回标记，提示词据此回固定文案
SEARCH_ERROR_MARKER = "__SEARCH_UNAVAILABLE__"


def _get_tavily() -> TavilySearch:
    global _tavily
    if _tavily is None:
        with _tavily_lock:
            if _tavily is None:
                _tavily = TavilySearch(max_results=5, include_answer=True)
    return _tavily


@tool
def tavily_search(query: str) -> str:
    """联网搜索工具，获取实时公开网络信息。

    适用场景：
    - 知识库检索结果不足或未找到相关资料时
    - 用户明确要求最新实时信息（如新闻、事件、数据）
    - 涉及知识库不包含的公开信息查询

    Args:
        query: 搜索关键词或自然语言问题。

    Returns:
        str: 格式化的搜索结果，包含 AI 总结和编号的网页标题与链接列表；
        搜索服务异常时返回 SEARCH_ERROR_MARKER（与"没有搜到结果"需区分处理）。
    """
    try:
        resp = _get_tavily().invoke(query)
        return f"总结：{resp.get('answer', '')}\n" + "\n".join(
            f"{i}. {r['title']}\n   {r['url']}"
            for i, r in enumerate(resp.get('results', []), 1)
        )
    except Exception as e:
        # 异常原文只写服务端日志：工具结果会被喂给模型，既可能被当成正文，
        # 也会把上游服务的地址/鉴权等细节带进模型上下文。
        # 返回标记而非"没有结果"的措辞，让主 Agent 能区分故障与无果
        print(f"[联网搜索] 失败: {e}")
        return SEARCH_ERROR_MARKER


class MainDeepAgent:
    def __init__(self):
        self.model=init_chat_model(
            model=agent_config["main_agent_model"],
            model_provider=agent_config["main_agent_model_provider"],
            base_url=agent_config["main_agent_model_base_url"],
        )
        # 共享同一个 checkpointer，保证启用/禁用联网搜索切换时对话上下文不丢失
        # 对话记录持久化到 PostgreSQL（LangGraph 检查点），重启后仍可恢复上下文
        self._pg_pool = ConnectionPool(
            conninfo=_PG_CONNECTION_STRING,
            min_size=PG_CHECKPOINT_POOL_MIN,
            max_size=PG_CHECKPOINT_POOL_MAX,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        self.checkpointer = PostgresSaver(self._pg_pool)
        # 多 worker 同时启动会并发执行建表 DDL 而撞车，用 PG 咨询锁串行化；
        # 表已存在时 setup 极快，后续 worker 依次通过
        with exclusive_section(_PG_CONNECTION_STRING, PG_LOCK_KEY_SETUP):
            self.checkpointer.setup()
        self.prompt=load_main_prompt()
        # 预构建两个 agent：一个带联网搜索工具，一个不带
        self._agent_with_search = self._build_agent(use_search=True)
        self._agent_without_search = self._build_agent(use_search=False)

    def _build_agent(self, use_search: bool):
        tools = [tavily_search] if use_search else []
        # create_deep_agent 会额外注入默认工具集（write_todos、filesystem 工具、execute、task）。
        # 默认后端是 StateBackend（内存态虚拟文件系统），execute 在非沙箱后端下只返回错误信息，
        # 因此即便文档或网页携带提示注入，也读不到本机文件、执行不了命令。
        # 若将来改用 FilesystemBackend / LocalShellBackend，必须先评估提示注入导致读写本地文件的风险。
        return create_deep_agent(
            model=self.model,
            tools=tools,
            system_prompt=self.prompt,
            checkpointer=self.checkpointer,
            subagents=[rag_summary_subagents],
            middleware=[
                SummarizationMiddleware(
                    model=self.model,
                    # 未配置 trigger 时 _should_summarize 恒为 False，摘要永不触发；
                    # 达到任一阈值即触发（OR 语义），摘要后保留最近 10 条消息，
                    # 防止长对话无限增长撑爆上下文上限、检查点随之外膨胀
                    trigger=[("messages", 30), ("tokens", 20000)],
                    keep=("messages", 10),
                )
            ],
        )

    def ask_deep_agent(self, query, thread_id, use_online_search=False):
        agent = self._agent_with_search if use_online_search else self._agent_without_search
        for chunk, metadata in agent.stream(
            {"messages": [{"role": "human", "content": query}]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode="messages",
        ):
            # 过滤掉 tool_message，不返回给前端用户
            if chunk.type == "tool":
                continue
            if chunk.content:
                yield chunk.content
