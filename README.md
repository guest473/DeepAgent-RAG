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

- **Agent 与模型**：框架用 deepagents + langchain（底层执行引擎均为 LangGraph）；模型经 DashScope 的 OpenAI 兼容接口调用。

- **检索与解析**：Milvus（langchain-milvus / pymilvus）+ DashScope `qwen3.7-text-embedding`；pypdf / docx2txt 解析文档；Tavily 联网搜索。

- **服务与存储**：FastAPI + Uvicorn；PostgreSQL（业务表 + LangGraph 检查点 + 咨询锁）。

- **前端与部署**：Vue 3 + Vite + TypeScript；Nginx。

## 目录结构

```
DeepAgent-RAG/
├── api_server.py       # FastAPI 入口：对话 / 账户 / 知识库 API
├── main_deep_agent.py  # 主 Agent 构建
├── auth.py             # 账户、JWT、密码哈希、限流
├── rag/                # 检索子代理、Milvus 封装与入库闭环、索引健康
├── utils/              # 配置 / 提示词 / 文件 / 路径 / PG 咨询锁
├── config/             # agent.yaml、vector_db.yaml
├── prompts_data/       # 主 / 摘要提示词（main_prompt.txt、summary_prompt.txt）
├── knowledge/          # 知识库源文件（自动创建，已忽略）
├── frontend-chat/      # Vue 3 + Vite 前端
├── deploy/             # Nginx 配置
├── requirements.txt    # Python 依赖
├── md5.txt             # 入库记录（运行时生成，已忽略）
└── .env                # 环境变量（需自行创建，勿提交）
```

## 快速开始

前置依赖：Python 3.12+、Node.js 18+、PostgreSQL，以及用于在本地拉起 Milvus 的 Docker（Milvus 默认地址 `http://localhost:19530`）。

### 1. 准备 PostgreSQL 与 Milvus

**PostgreSQL**：用官方安装包本机安装，记下超级用户 `postgres` 的密码；把安装目录下的 `bin`（如 `<PostgreSQL 安装目录>\bin`）加入系统环境变量 `Path`，以便直接用 `psql` 建库：

```powershell
psql -U postgres -c "CREATE DATABASE deepagent_rag;"   # 按提示输入 postgres 密码
psql -U postgres -l                                    # 列出数据库，确认已创建
```

**Milvus**：先建一个存放数据的目录（如 `C:\milvus`），在其中用 Docker 启动：

```powershell
cd C:\milvus
Invoke-WebRequest -Uri https://github.com/milvus-io/milvus/releases/download/v2.4.23/milvus-standalone-docker-compose.yml -OutFile docker-compose.yml
docker compose up -d      # 旧版 Compose 为 docker-compose up -d；停止：docker compose down
```

数据保存在该目录的 `volumes/` 下，只要它还在，重新执行启动命令即可取回原有向量、集合与索引。Milvus 默认监听 `19530`（gRPC），`9091` 提供健康检查与 WebUI：`Invoke-WebRequest http://localhost:9091/healthz` 应返回 `OK`。

### 2. 安装依赖并配置环境变量

在项目根目录执行：

```powershell
pip install -r requirements.txt
```

然后在同目录创建 `.env`：

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

后端和前端各开一个终端。

**后端**（项目根目录）：

```powershell
python api_server.py             # 默认 0.0.0.0:8000，启动时自动建表；健康检查 GET /api/health
```

**前端**（`frontend-chat` 目录）：

```powershell
npm install                      # 首次或依赖变动时执行
npm run dev                      # 打开 http://localhost:5173（/api 已代理到 8000）
```

开发时用 `npm run dev` 就够了；要上线再执行 `npm run build`，产物在 `frontend-chat/dist`，用法见下一节。

打开前端注册登录：用户名 2–50 字符，密码至少 10 位且须含数字、字母与特殊符号。注册一律是普通用户；需要管理员时按第 1 步的连接方式登录数据库、执行下面这条，再重新登录：

```sql
UPDATE users SET role = 'admin' WHERE username = 'your-username';
```

管理员点左下角齿轮菜单进入「管理知识库」，上传 `.txt` / `.pdf` / `.docx`（上传即入库）后即可回对话页提问，并按需开启联网搜索。

### 4. 部署上线（可选）

生产部署用 Nginx：它托管前端静态文件，并把 `/api` 转发给后端。

1. **构建前端**：进入 `frontend-chat` 执行 `npm run build`，产物在 `frontend-chat/dist`。
2. **安装 Nginx**：下载 Nginx for Windows 的 zip（http://nginx.org/en/download.html），解压到如 `C:\nginx`。
3. **套用配置**：用 `deploy/nginx-windows.conf` 覆盖 `C:\nginx\conf\nginx.conf`，并把里面的 `root` 改成第 1 步 `dist` 的实际路径（如 `C:/code/DeepAgent-RAG/frontend-chat/dist`）。
4. **启动后端**：在项目根目录执行 `python -u api_server.py`。`-u` 不能省——非 TTY 下没有它，stdout 会块缓冲，日志要攒够一批才吐出来。
5. **启动 Nginx**：在 `C:\nginx` 目录下执行：

```powershell
start nginx          # 启动
nginx -s reload      # 改配置后重载
nginx -s stop        # 停止
```

完成后打开 `http://localhost` 就是前端页面；后端健康检查仍是 `GET /api/health`。

配置里已经替你做好的事：

- **SSE 流式对话**：`/api` 已关闭缓冲与压缩、延长超时；否则会出现「长时间没反应、最后整段答案一次性蹦出来」。
- **上传大小**：`client_max_body_size 50m` 已与后端 `MAX_UPLOAD_MB` 对齐。
- **安全响应头**：CSP、`X-Frame-Options` 等已内置。
- **真实客户端 IP**：默认只信任 `127.0.0.1`，让后端取到真实 IP 做限流。

关于 HTTPS：该配置只监听 80、不带证书，适合本机或内网。要让公网访问，建议在前面套 Cloudflare 代理或 cloudflared 隧道来终结 TLS——走隧道时 `set_real_ip_from 127.0.0.1` 保持不变；改走 Cloudflare 代理（对端是 Cloudflare 边缘）时，需把它换成 Cloudflare 官方网段，才能正确解析 `CF-Connecting-IP`（该头可被伪造，切勿写成 `0.0.0.0/0`）。

需要常驻运行时，可把后端注册为系统服务（如 `nssm`）。

## API 一览

除「公开」外均需带 `Authorization: Bearer <token>`。

| 权限  | 接口                                                                                                                                                                                                                      |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 公开  | `GET /api/health`；`POST /api/auth/register`；`POST /api/auth/login`（返回 JWT）                                                                                                                                              |
| 登录  | `GET /api/auth/me`；`GET` / `POST /api/conversations`（列表 / 创建，id 由服务端生成）；`PATCH` / `DELETE /api/conversations/{id}`；`POST /api/conversations/{id}/messages`；`POST /api/chat/stream`（SSE，事件 `content` / `done` / `error`） |
| 管理员 | `GET /api/files`、`POST /api/files/upload`、`DELETE /api/files/{filename}`；`POST /api/vector/build`；`GET /api/index/health`（`?refresh=1` 立即检查）、`POST /api/index/health/repair`                                            |

## 配置说明

`config/agent.yaml`：主 Agent（`main_agent_model*`）与 RAG 子代理（`rag_summary_model*`）可分别配置模型名、`provider` 与 `base_url`，两者都能指向任意 OpenAI 兼容服务。

`config/vector_db.yaml`：Milvus 集合与连接（`collection_name` / `milvus_uri` / `pool_size`）；向量化与切片（`embedding_model` / `chunk_size: 500` / `chunk_overlap: 50` / `separators`）；检索条数 `k: 3`；知识库目录与类型（`knowledge_dir` / `allowed_types`）；自检间隔 `health_check_interval_hours: 6`（下限 10 分钟）与自愈轮数 `health_auto_repair_max_rounds: 3`。

常用环境变量：

| 变量                                                   | 默认值                                           | 说明                                                                                                                            |
| ---------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `HOST` / `PORT`                                      | `0.0.0.0` / `8000`                            | 后端监听地址                                                                                                                        |
| `UVICORN_WORKERS`                                    | `1`                                           | 工作进程数                                                                                                                         |
| `THREADPOOL_SIZE`                                    | `200`                                         | 线程池上限，每个活跃对话流约占用 1 个线程，应不小于目标并发流数                                                                                           |
| `FORWARDED_ALLOW_IPS`                                | `127.0.0.1`                                   | 信任的代理地址，用于取真实客户端 IP                                                                                                           |
| `ENABLE_API_DOCS`                                    | `false`                                       | 是否开放 `/docs`、`/redoc` 与 `/openapi.json`                                                                                        |
| `MAX_UPLOAD_MB`                                      | `50`                                          | 单文件上传上限，需与 Nginx `client_max_body_size` 对齐                                                                                    |
| `PG_POOL_MIN` / `PG_POOL_MAX`、`PG_CHECKPOINT_POOL_*` | `2` / `20`                                    | 业务与检查点连接池                                                                                                                     |
| `CHAT_*`                                             | `30`/`60`、`3`、`8000`                          | 对话限流（`CHAT_RATE_LIMIT` / `CHAT_RATE_WINDOW_SECONDS`）、单用户并发上限（`MAX_CONCURRENT_CHAT_PER_USER`）、单条消息长度（`CHAT_MESSAGE_MAX_CHARS`） |
| `INDEX_WRITE_LOCK_TIMEOUT_SECONDS`                   | `300`                                         | 等待索引写锁上限，超时返回 503                                                                                                             |
| `LOGIN_*` / `REGISTER_*`                             | `10`/`60s`、`100`/`60s`、`5`/`900s`、`5`/`3600s` | 登录三层限流（账号 / IP / 单 IP×单账号）与注册限流                                                                                               |

> 多 worker / 多实例时，`workers × (PG_POOL_MAX + PG_CHECKPOINT_POOL_MAX)` 不得超过 PG 的 `max_connections`。

## 知识库一致性

知识库由三份数据构成：`knowledge/` 下的源文件、`md5.txt` 中的入库记录（一行一个文件内容 MD5）、Milvus 中的向量（`source` 元数据即文件绝对路径）。一致性设计围绕三者展开。

**以内容为准**：入库、判定与展示一律按内容 MD5 去重，基准是内容而非路径。同内容文件只切分入库一次、共享一组向量（向量挂在首个入库文件的 `source` 上）；只要该内容的任一副本在磁盘上且带向量，就视为「已入库」，与 `/api/files` 口径一致。

**单文件入库闭环**：每个文件独立走「切分 → 写入向量 → 写 `md5.txt` → 立即校验」，校验标准是该 `source` 在 Milvus 中的条数等于切块数且记录存在；不过关就删除该文件全部向量与记录后重建（最多 3 次），不依赖定时自检兜底。Milvus 默认 Bounded 一致性下新写入可能短暂不可见，计数不符时会先 `flush` 再复验一次，避免误判重建。`md5.txt` 采用「写临时文件 + 原子替换」，防止中途失败截断。

**删除的补偿**：先删记录、再删向量；向量删除失败即回滚记录，失败后状态要么完整未删、要么「记录已删而向量还在」，两者都会落入自检的重建记录修复路径，而不会误把待删文件重新索引。若删掉的正是持有向量的那一份、磁盘上尚有同内容副本，会立即以副本重新入库，避免「显示已入库却检索不到」。

**定时自检与自愈**：后台每 `health_check_interval_hours` 小时核对三类不一致并自动修复（新上传但从未构建的文件不算异常）：

| 现象 | 判定 | 修复动作 |
| --- | --- | --- |
| 向量在、记录缺失 | `missing_md5_record` | 以 Milvus 为准重建 `md5.txt` |
| 文件已删、向量残留 | `ghost_vectors` / `orphan_md5` | 清理残留向量、剔除孤立记录 |
| 记录在、向量缺失 | `missing_vectors` | 剔除记录并按内容重新入库一个代表文件 |

修复按固定顺序进行：先清理磁盘上已无对应文件的残留向量，再以「向量库与磁盘都还有」的内容为准重建入库记录，最后把「记录还在、向量却丢了」的内容重新入库。重索引失败会把记录补回，交由下一轮自检继续重试，避免停在「未入库」却永不重试。多 worker / 多实例下，索引写操作由 PG 咨询锁互斥，定时自检用会话级咨询锁选主，确保只由一个进程执行。

## 安全说明

- `.env` 含密钥，已在 `.gitignore` 中忽略，请勿提交；`JWT_SECRET` 变更会使所有已签发 token 失效。

- Agent 默认使用内存态虚拟文件系统后端，无法读写本机文件或执行命令，可抵御文档 / 网页携带的提示注入；若改用 `FilesystemBackend` / `LocalShellBackend`，需重新评估相应风险。

## 故障排查

- **启动即退出，提示「未配置 JWT_SECRET」**：该变量必填，按快速开始中的命令生成后填入 `.env`。

- **接口返回 429**：触发了滑动窗口限流。登录 / 注册看 `LOGIN_*` / `REGISTER_*`，对话看 `CHAT_*`（频率或并发上限）。等窗口结束或调大阈值即可；若多用户互相误伤，多半是后端没取到真实客户端 IP，检查 `FORWARDED_ALLOW_IPS` 与代理的 `set_real_ip_from` / `real_ip_header` 是否一致。

- **上传报「文件超过大小上限」**：`MAX_UPLOAD_MB` 与代理 `client_max_body_size` 不一致，把两处改成一样。

- **上传 / 删除 / 构建报 503「索引正被其他操作占用」**：等待索引写锁超过了 `INDEX_WRITE_LOCK_TIMEOUT_SECONDS`（默认 300 秒），说明别的进程正在做长操作，稍后重试。

- **删除报「请确认文件未被其他程序占用」**：磁盘文件正被外部程序打开，此时向量与入库记录都还没动，关掉占用后重试即可。

- **提示「无法连接 Milvus」**：Milvus 服务没启动，或 `milvus_uri` 配错。

- **提示「未找到相关资料」**：确实没有相关内容，或者文件没入库。先在文档列表确认该文件为「已入库」，再看「索引健康」的向量块数是否大于 0；必要时点「补建向量库」或「手动修复」。

- **提示「知识库检索服务暂时不可用」**：检索服务自身故障，跟「知识库没有资料」不是一回事，稍后重试。

- **长时间无输出、随后整段答案一次性返回**：反向代理缓冲所致，`deploy/` 配置已关闭 `proxy_buffering` 与 `gzip` 并延长超时，自建代理需照此配置。

- **启动日志警告 PG 连接需求逼近 `max_connections`**：调大 `max_connections`，或调小连接池上限。

## 许可证

MIT
