from deepagents import CompiledSubAgent
from langchain.chat_models import init_chat_model
from langchain.tools import tool
from utils.prompt_loader import load_summary_prompt
from rag.vector_store import get_vector_store
from utils.config_loader import agent_config
from langchain.agents import create_agent
from dotenv import load_dotenv
load_dotenv()
# 检索异常与"确实无结果"必须可区分：否则 Milvus 挂掉时用户只会看到
# "知识库中未找到相关资料"，既误导用户，管理员侧也没有任何信号。
# 异常时返回该标记，由子代理按提示词转成固定文案。
RETRIEVAL_ERROR_MARKER = "__RETRIEVAL_UNAVAILABLE__"


@tool
def do_retrieval(query):
    """从内部知识库检索与用户问题相关的专业文档和资料。

    优先使用此工具查询：概念解释、术语定义、专业知识、内部文档内容。

    Args:
        query: 用户的原始问题或需要检索的关键词。

    Returns:
        list[str]: 检索到的文档片段内容列表。无结果时返回空列表；
        检索服务异常时返回 [RETRIEVAL_ERROR_MARKER]。
    """
    if not isinstance(query, str) or not query.strip():
        return []
    try:
        retriever = get_vector_store().get_retriever()
        docs = retriever.invoke(query)
        return [doc.page_content for doc in docs]
    except Exception as e:
        # 异常原文只写服务端日志，避免上游细节进入模型上下文；
        # 用标记告诉子代理"这是检索故障，不是没有资料"
        print(f"检索失败: {e}")
        return [RETRIEVAL_ERROR_MARKER]


rag_summary_agent = create_agent(
    model=init_chat_model(
        model=agent_config["rag_summary_model"],
        model_provider=agent_config["rag_summary_model_provider"],
        base_url=agent_config["rag_summary_model_base_url"]),
    tools=[do_retrieval],
    system_prompt=load_summary_prompt(),
    # 有意不配 checkpointer：主图经 task 工具调用子代理时会继承主图的 checkpointer
    # （LangGraph 中继承来的 __pregel_checkpointer 优先于图自身配置），状态按
    # thread_id + 每次调用独立的 checkpoint_ns 隔离。自带 saver 永不生效，
    # 反而会让"主图之外单独调用"因缺少 thread_id 直接报错。
)

rag_summary_subagents = CompiledSubAgent(
    name="rag_summary_subagents",
    description="知识库检索专家。专门用于从内部向量数据库中检索专业文档、技术资料和知识库内容。当用户询问任何需要查阅内部资料的问题时（如特定概念解释、文档查询、专业知识等），必须首先调用此子代理。它会自动检索相关文档并生成基于知识库的准确回答。",
    runnable=rag_summary_agent,
)
