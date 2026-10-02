# 浅问深答

面向内部资料的检索增强问答系统：回答严格基于知识库检索结果而非模型自身知识，必要时可启用联网搜索；具备账户体系、会话持久化与流式输出；既可在本地自建、单人使用，也可部署到服务器供多用户远程访问。

## 核心特性

- **知识库优先的 RAG 问答**：主 Agent 先做意图识别，寒暄直接回应，其余交由检索子代理从 Milvus 取回 Top-K 文档块；回答严格基于检索结果，「检索无果」与「检索故障」以不同固定文案告知，不以模型自身知识补全。
- **可选联网搜索**：界面可开关 Tavily 联网搜索。
- **流式对话 + 持久化**：`POST /api/chat/stream` 以 SSE 增量输出；上下文按 `thread_id` 从 PostgreSQL 检查点恢复，消息与会话同时落库，超长对话自动摘要压缩。
- **账户与限流**：Argon2id 密码哈希 + JWT 鉴权，管理员 / 普通用户两种角色；登录、注册、对话均有滑动窗口限流。
- **知识库管理**：`.txt` / `.pdf` / `.docx` 按内容（MD5）去重入库，支持上传、删除与构建索引；上传即切分入库并当场校验，失败则重建复验。系统维护 `md5.txt`、`knowledge/`、Milvus 三份数据，判定基准是内容而非路径（内容相同的文件只入库一次、共享一组向量），后台每 `health_check_interval_hours` 小时核对并自动修复记录丢失、文件丢失、向量丢失三类异常。
- **多进程安全**：以 PostgreSQL 咨询锁实现索引写互斥与定时任务选主。

## 技术栈

- **Agent 与模型**：框架用 deepagents + langchain（底层执行引擎均为 LangGraph）；模型经 DashScope 的 OpenAI 兼容接口调用
- **检索与解析**：Milvus（langchain-milvus / pymilvus）+ DashScope `qwen3.7-text-embedding`；pypdf / docx2txt 解析文档；Tavily 联网搜索
- **服务与存储**：FastAPI + Uvicorn；PostgreSQL（业务表 + LangGraph 检查点 + 咨询锁）
- **前端与部署**：Vue 3 + Vite + TypeScript；Nginx

## 目录结构

```
DeepAgent-RAG/
├── api_server.py       # FastAPI 入口：对话 / 账户 / 知识库 API
├── main_deep_agent.py  # 主 Agent 构建
├── auth.py             # 账户、JWT、密码哈希、限流
├── rag/                # 检索子代理、Milvus 封装与入库闭环、索引健康
├── utils/              # 配置 / 提示词 / 文件 / 路径 / PG 咨询锁
├── config/             # agent.yaml、vector_db.yaml
├── knowledge/          # 知识库源文件（自动创建，已忽略）
├── frontend-chat/      # Vue 3 + Vite 前端
├── deploy/             # Nginx 配置
├── md5.txt             # 入库记录（运行时生成，已忽略）
└── .env                # 环境变量（需自行创建，勿提交）
```

## 快速开始

前置依赖：Python 3.12+、Node.js 18+、PostgreSQL、Milvus（默认 `http://localhost:19530`）。

### 1. 准备 PostgreSQL 与 Milvus

**PostgreSQL**：用官方安装包本机安装，记下超级用户 `postgres` 的密码；把安装目录下的 `bin`（如 `<PostgreSQL 安装目录>\bin`）加入系统环境变量 `Path`，以便直接用 `psql` 建库：

```powershell
psql -U postgres -c "CREATE DATABASE deepagent_rag;"   # 按提示输入 postgres 密码
psql -U postgres -l                                    # 列出数据库，确认已创建
```

**Milvus**：在任意目录（如 `C:\milvus`）下用 Docker 启动：

```powershell
cd C:\milvus
Invoke-WebRequest -Uri https://github.com/milvus-io/milvus/releases/download/v2.4.23/milvus-standalone-docker-compose.yml -OutFile docker-compose.yml
docker compose up -d      # 旧版 Compose 为 docker-compose up -d；停止：docker compose down
```

数据保存在该目录的 `volumes/` 下，只要它还在，重新执行启动命令即可取回原有向量、集合与索引。Milvus 默认监听 `19530`（gRPC），`9091` 提供健康检查与 WebUI：`Invoke-WebRequest http://localhost:9091/healthz` 应返回 `OK`。

### 2. 安装依赖并配置环境变量

```powershell
pip install -r requirements.txt
```

在项目根目录创建 `.env`：

```ini
DASHSCOPE_API_KEY=your-dashscope-key   # 供 embedding
OPENAI_API_KEY=your-dashscope-key      # 供 langchain 的 openai provider，与上者同源，填同一个 Key
TAVILY_API_KEY=your-tavily-key         # 可选，不填则联网搜索不可用
PG_CONNECTION_STRING=postgresql://postgres:password@localhost:5432/deepagent_rag   # 密码换成安装 PostgreSQL 时设置的
JWT_SECRET=your-random-secret          # 必填，缺失会拒绝启动
JWT_EXPIRE_HOURS=72
LANGSMITH_TRACING=true                 # 以下为可观测性配置，可选
LANGSMITH_API_KEY=your-langsmith-key
LANGSMITH_PROJECT=DeepAgent-RAG
LANGSMITH_ENDPOINT=https://apac.api.smith.langchain.com
```

`JWT_SECRET` 生成：`python -c "import secrets; print(secrets.token_hex(32))"`

### 3. 启动并开始使用

```powershell
python api_server.py             # 默认 0.0.0.0:8000，启动时自动建表；健康检查 GET /api/health
cd frontend-chat
npm install
npm run dev                      # 开发模式 http://localhost:5173（/api 已代理到 8000）
npm run build                    # 生产构建，产物在 frontend-chat/dist
```

打开前端注册登录：用户名 2–50 字符，密码至少 10 位且须含数字、字母与特殊符号。注册固定为普通用户，需要管理员时更新数据库再重新登录：

```sql
UPDATE users SET role = 'admin' WHERE username = 'your-username';
```

管理员点左下角齿轮菜单进入「管理知识库」，上传 `.txt` / `.pdf` / `.docx`（上传即入库）后即可回对话页提问，并按需开启联网搜索。

## API 一览

除「公开」外均需带 `Authorization: Bearer <token>`。

| 权限 | 接口 |
| --- | --- |
| 公开 | `GET /api/health`；`POST /api/auth/register`；`POST /api/auth/login`（返回 JWT） |
| 登录 | `GET /api/auth/me`；`GET` / `POST /api/conversations`（列表 / 创建，id 由服务端生成）；`PATCH` / `DELETE /api/conversations/{id}`；`POST /api/conversations/{id}/messages`；`POST /api/chat/stream`（SSE，事件 `content` / `done` / `error`） |
| 管理员 | `GET /api/files`、`POST /api/files/upload`、`DELETE /api/files/{filename}`；`POST /api/vector/build`；`GET /api/index/health`（`?refresh=1` 立即检查）、`POST /api/index/health/repair` |

## 配置说明

`config/agent.yaml`：主 Agent（`main_agent_model*`）与 RAG 子代理（`rag_summary_model*`）可分别配置 `model` / `provider` / `base_url`，后者可替换为任意 OpenAI 兼容服务。

`config/vector_db.yaml`：Milvus 地址与连接池（`milvus_uri` / `pool_size`）；向量化与切片（`embedding_model` / `chunk_size: 500` / `chunk_overlap: 50` / `separators`）；检索条数 `k: 3`；知识库目录与类型（`knowledge_dir` / `allowed_types`）；自检间隔 `health_check_interval_hours: 6`（下限 10 分钟）与自愈轮数 `health_auto_repair_max_rounds: 3`。

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 后端监听地址 |
| `UVICORN_WORKERS` | `1` | 工作进程数 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | 信任的代理地址，用于取真实客户端 IP |
| `MAX_UPLOAD_MB` | `50` | 单文件上传上限，需与 Nginx `client_max_body_size` 对齐 |
| `PG_POOL_MIN` / `PG_POOL_MAX`、`PG_CHECKPOINT_POOL_*` | `2` / `20` | 业务与检查点连接池 |
| `CHAT_*` | `30`/`60`、`3`、`8000` | 对话限流（`CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW_SECONDS`）、单用户并发上限（`MAX_CONCURRENT_CHAT_PER_USER`）、单条消息长度（`CHAT_MESSAGE_MAX_CHARS`） |
| `INDEX_WRITE_LOCK_TIMEOUT_SECONDS` | `300` | 等待索引写锁上限，超时返回 503 |
| `LOGIN_*` / `REGISTER_*` | `10`/`60s`、`100`/`60s`、`5`/`900s`、`5`/`3600s` | 登录三层限流（账号 / IP / 单 IP×单账号）与注册限流 |

> 多 worker / 多实例时，`workers × (PG_POOL_MAX + PG_CHECKPOINT_POOL_MAX)` 不得超过 PG 的 `max_connections`。

## 部署与安全

`deploy/nginx-windows.conf` 提供前端静态托管 + `/api` 反向代理，已处理 SSE 转发的关键项（关闭缓冲与压缩、延长超时）；本机不配 HTTPS，TLS 交由外层 Cloudflare / 隧道终结，生产环境应由它启用 HTTPS 与安全响应头。使用前把 `root` 改为本机 `frontend-chat/dist` 的实际路径。启动后端请加 `-u`（或设 `PYTHONUNBUFFERED=1`），否则非 TTY 下 stdout 块缓冲会让日志成批延迟输出；需要常驻时可注册为系统服务（如 `nssm`）。

- `.env` 含密钥，已在 `.gitignore` 中忽略，请勿提交；`JWT_SECRET` 变更会使所有已签发 token 失效。
- Agent 默认使用内存态虚拟文件系统后端，无法读写本机文件或执行命令，可抵御文档 / 网页携带的提示注入；若改用 `FilesystemBackend` / `LocalShellBackend`，需重新评估相应风险。

## 故障排查

- **启动即退出，提示「未配置 JWT_SECRET」**：该变量必填，按快速开始中的命令生成后填入 `.env`。
- **返回 429**：登录 / 注册触发滑动窗口限流（`LOGIN_*` / `REGISTER_*`），对话则可能是频率限制或并发上限（`CHAT_*`）。若多用户互相误伤，说明后端未取到真实客户端 IP，检查 `FORWARDED_ALLOW_IPS` 与代理的 `set_real_ip_from` / `real_ip_header` 是否一致；否则等窗口结束或调大阈值。
- **上传 / 删除 / 构建失败**：报「文件超过大小上限」是 `MAX_UPLOAD_MB` 与代理 `client_max_body_size` 不一致；报 503「索引正被其他操作占用」是等待索引写锁超过 `INDEX_WRITE_LOCK_TIMEOUT_SECONDS`，稍后重试；删除时提示「请确认文件未被其他程序占用」是文件被外部程序打开，向量与记录均未改动，关闭后重试。
- **知识库提示异常**：「无法连接 Milvus」是服务未启动或 `milvus_uri` 配错；「未找到相关资料」是确实无结果或文件未入库（确认状态为「已入库」且块数大于 0，必要时点「补建向量库」或「手动修复」）；检索服务自身故障会提示「知识库检索服务暂时不可用」。
- **长时间无输出、随后整段答案一次性返回**：反向代理缓冲所致，`deploy/` 配置已关闭 `proxy_buffering` 与 `gzip` 并延长超时，自建代理需照此配置。
- **启动日志警告 PG 连接需求逼近 `max_connections`**：调大 `max_connections`，或调小连接池上限。

## 许可证

MIT
