# 多格式 RAG 智能问答系统

> 基于 LangChain + Chroma + DeepSeek 的离线文档知识库问答系统。支持 Word / PDF / TXT / Excel 多格式文档解析、表格结构化、图片自动描述、向量检索、多轮对话记忆和流式回答。

---

## 一、项目作用

本项目是一个**检索增强生成（RAG）系统**，解决大模型"不知道私有文档内容"和"容易幻觉"的问题。

核心能力：

1. **多格式文档入库**：上传 Word / PDF / TXT / Excel → 自动解析标题层级、正文、表格、图片 → 语义切分 → 向量化存入 Chroma。
2. **表格结构化解析**：docx 表格转 Markdown 独立入库；PDF 表格用 pdfplumber 提取；Excel 按 sheet 转表格。
3. **在线智能问答**：用户提问 → Redis 检索缓存命中？（4.3）→ 自动查询改写 → 混合检索（向量 + BM25）→ RRF 融合 → Rerank 精排 → 拼接上下文 → DeepSeek 大模型生成基于文档的准确回答；无历史对话时命中回答缓存直接返回（4.3）。
4. **图文混合理解**：文档中的图片在入库时由视觉模型（qwen-vl-max）生成文字描述，查询时随文本一起进入上下文。
5. **多轮对话记忆（5.0 升级为多会话隔离）**：滑动窗口保留最近 N 轮对话，支持连贯追问；按 `session_id` 隔离各会话记忆，互不串扰（Redis Hash `session:{sid}` + TTL，不可用自动回退 JSONL 冷备份）。
6. **增量更新**：同名文件修改后重新上传，自动删除旧版本数据再插入新版本。
7. **流式输出**：逐 token 返回回答，前端打字机效果。
8. **服务化接口（5.0）**：FastAPI 暴露 `/health`、`/ingest`、`/query`、`/documents` REST 接口，供任意前端/小程序/后端调用；异步并发 + SSE 流式。
9. **前端界面（5.2）**：浏览器打开 `http://127.0.0.1:8000/` 即见产品级对话界面（ui-ux-pro-max 设计系统：极简瑞士风 + 流式打字机 + 文档上传 + 会话管理），无需 curl 也能用。

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        离线入库流程                          │
│                                                             │
│  文档（docx/pdf/txt/excel）                                  │
│      │                                                      │
│      ▼                                                      │
│  LoaderFactory.create()  — 按扩展名分发                      │
│      │                                                      │
│      ├── DocxLoader    → 段落+表格+图片，标题语义切分        │
│      ├── PdfLoader     → 页文本+表格+图片，页内标题切分      │
│      ├── TxtLoader     → 空行分段+递归字符分割               │
│      └── ExcelLoader   → 按 sheet 转 Markdown，大表切块     │
│      │                                                      │
│      ▼  统一输出 List[Document]                              │
│                                                             │
│  KnowledgeBase.upload_documents()                           │
│  ├── MD5 去重（文件内容不变则跳过）                          │
│  ├── 增量更新（同名文件先删旧数据）                          │
│  ├── 图片预计算描述（qwen-vl-max，4 线程并行）→ metadata     │
│  ├── 文本向量化（text-embedding-v4，按批 32 提交）           │
│  ├── 存入 Chroma（runtime/chroma_db/）                          │
│  └── 自动术语提取（term_extractor → runtime/auto_dict.json）   │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                        在线问答流程                          │
│                                                             │
│  用户提问                                                    │
│      │                                                      │
│      ▼                                                      │
│  _get_history(session_id)                                    │
│  ├── 传了 session_id → SessionMemory（Redis Hash，不可用退 JSONL）│
│  └── 未传 → ConversationMemory（全局 JSONL + deque 滑动窗口）  │
│      │                                                      │
│      ▼                                                      │
│  DocxRetriever.retrieve(query)                              │
│  ├── [4.3] 检索缓存命中？（键=原问题）→ 直接返回缓存结果        │
│  ├── 查询改写（自动术语扩展，L04）                              │
│  ├── 混合检索：向量 + BM25 → RRF 融合（L03）                │
│  ├── Rerank 精排 top_k（L05）                               │
│  └── 读取 metadata 中预计算的图片描述                        │
│      │                                                      │
│      ▼                                                      │
│  _format_context()                                          │
│  └── 历史对话 + 文档片段 + 图片描述 → 结构化上下文            │
│      │                                                      │
│      ▼                                                      │
│  [4.3] 回答缓存命中？（仅无历史时）→ 直接返回缓存回答          │
│      │                                                      │
│      ▼                                                      │
│  ChatPromptTemplate → ChatDeepSeek → StrOutputParser        │
│  └── LCEL 链式调用，流式生成回答                             │
│      │                                                      │
│      ▼                                                      │
│  SessionMemory / ConversationMemory.save()                  │
│  └── 传 session_id → Redis Hash `session:{sid}`（TTL）        │
│      未传 → runtime/conversation_memory.jsonl                │
└─────────────────────────────────────────────────────────────┘

   ----[ 服务化接入层（5.0，FastAPI）]----
  POST /ingest / POST /query / GET /documents / GET /health
  接口层用 anyio.to_thread.run_sync 跑同步逻辑，事件循环并发服务多请求；
  stream=true 走 SSE + iterate_in_threadpool，真·逐 token 推送。
```

---

## 三、模块说明

### 3.1 文件清单

| 文件 | 职责 | 核心类/函数 |
|---|---|---|
| `config_data.py` | 全局配置 | 向量库路径、模型名、TOP_K、记忆窗口等 |
| `loader_factory.py` | Loader 工厂 | `LoaderFactory`，按扩展名分发 |
| `loaders/base_loader.py` | 解析器抽象基类 | `BaseLoader` |
| `loaders/docx_loader.py` | Word 解析（含表格） | `DocxLoader` |
| `loaders/pdf_loader.py` | PDF 解析（含表格） | `PdfLoader` |
| `loaders/txt_loader.py` | 纯文本/Markdown 解析 | `TxtLoader` |
| `loaders/excel_loader.py` | Excel 解析 | `ExcelLoader` |
| `knowledge_base.py` | 通用入库服务 | `KnowledgeBase`，MD5去重+增量更新+图片并行+向量批量（4.0）+IPv4补丁（4.1）+自动术语提取（4.2）+入库清空Redis缓存（4.3） |
| `qa_service.py` | 在线问答编排 | `DocxRetriever`（混合检索链）、`DocxQAService` |
| `ipv4_patch.py` | IPv4 优先补丁（4.1 hotfix） | `apply_ipv4_first()`，幂等 |
| `bm25_index.py` | BM25 词法索引（自实现，混合检索用） | `BM25Index`、`tokenize` |
| `term_extractor.py` | 入库自动术语提取（自动改写，零手写） | `TermExtractor`，入库时更新 `runtime/auto_dict.json` |
| `query_rewriter.py` | 查询改写（自动词典版，口语→文档术语） | `QueryRewriter` |
| `reranker.py` | Rerank 精排（百炼 API，熔断降级） | `Reranker` |
| `redis_cache.py` | Redis 两级缓存（检索 + 回答，熔断降级）+ 会话记忆 Hash | `RedisCache`、`SessionMemory`，入库联动失效（4.3）、会话隔离（5.0） |
| `memory.py` | 全局对话记忆管理 | `ConversationMemory` |
| `app_api.py` | FastAPI 服务化入口（5.0，:8000） | `/health` `/ingest` `/query` `/documents`，异步并发 + SSE 流式 |
| `app_file_loader.py` | 文档上传 Web 界面（Gradio，:7860） | `process_and_store()` |
| `app_chat.py` | 问答聊天 Web 界面（Gradio，:7861） | `respond()` |
| `versions/` | 版本化改进计划快照（`IMPROVEMENT_PLAN_<版本>.md`） | 1.0 / 2.0 / 3.0 / 4.0 / 4.1 / 4.2 / 4.3 / 5.0 / 5.1 / 5.2 快照 |
| `CHANGELOG.md` | 版本演进摘要（0 → 5.2.2） | 面向成果 |
| `DEV_LOG.md` | 开发日志（每次修改详细记录，时间线溯源） | 任务 0 → 26 |
| `OPENSOURCE_CHECKLIST.md` | 开源前检查清单（私人） | 面向作者 |
| `LEARNING_PLAN.md` | 对标企业级 RAG 的分阶段学习计划 | 位于 `rag_learning/`（学习侧） |
| `README.md` | 项目总览与使用方法 | 本文档 |

### 3.2 各格式 Loader 对比

| 格式 | 解析库 | 切分策略 | 表格支持 | 图片支持 | 额外依赖 |
|---|---|---|---|---|---|
| docx | python-docx | 标题层级语义切分 | Markdown 独立 chunk | 提取并关联 | 已有 |
| pdf | PyMuPDF + pdfplumber | 按页+页内标题切分 | pdfplumber 提取 | 提取并关联 | `pip install pymupdf pdfplumber` |
| txt/md | 标准库 | 空行分段+递归字符分割 | 无 | 无 | 无 |
| xlsx/xls | openpyxl | 按 sheet，大表按行切块 | 整表 Markdown | 无 | `pip install openpyxl` |

### 3.3 Document metadata 统一字段

所有 Loader 输出的 Document 都包含以下 metadata 字段：

| 字段 | 说明 |
|---|---|
| `source` | 源文件名 |
| `chunk_id` | 文档块序号 |
| `heading` | 块标题（章节名 / sheet名 / "表格"） |
| `heading_level` | 标题级别（1=一级, 2=二级, 0=表格/数据） |
| `chapter` | 所属一级章节 |
| `doc_type` | 块类型：paragraph / table / sheet |
| `images` | 关联图片路径列表 |
| `image_count` | 关联图片数量 |
| `table_rows` / `table_cols` | 表格行列数（非表格块为 0） |
| `image_description` | 入库时预计算的图片描述（入库阶段补充） |
| `create_time` / `operator` | 入库时间和操作者（入库阶段补充） |

---

## 四、技术栈

| 层级 | 技术选型 | 说明 |
|---|---|---|
| 文档解析 | python-docx / PyMuPDF / pdfplumber / openpyxl | 多格式解析 |
| 向量数据库 | Chroma | 本地持久化，SQLite 单文件存储 |
| 嵌入模型 | text-embedding-v4（DashScope） | 1536 维向量 |
| 聊天模型 | deepseek-v4-pro（DeepSeek） | 问答生成 |
| 视觉模型 | qwen-vl-max（DashScope） | 图片转文字描述 |
| 编排框架 | LangChain | Document、Chroma 封装、LCEL 链式调用 |
| 服务化 | FastAPI + uvicorn | 异步接口 / SSE 流式（5.0） |
| 前端 | Gradio + FastAPI | Gradio 演示界面（本地）；FastAPI 程序接口（5.0） |
| 记忆存储 | Redis Hash + JSONL 文件 | 多会话隔离 Redis `session:{sid}`，不可用退 JSONL（5.0）；全局记忆 JSONL |
| 缓存 | Redis（redis-py） | 两级缓存：检索 + 回答，熔断降级（4.3） |

---

## 五、使用方法

### 5.1 环境准备

**Python 版本**：3.10+

**基础依赖**（docx 解析 + 问答）：

```bash
pip install python-docx langchain langchain-chroma langchain-community \
    langchain-deepseek dashscope gradio
```

**可选依赖**（按需安装）：

```bash
# PDF 解析
pip install pymupdf pdfplumber

# Excel 解析
pip install openpyxl

# Redis 缓存（4.3，可选：不装不启动会自动降级直连）
pip install redis
```

**Redis 启动（可选）**：

Windows 本机没有 `redis-server` 命令时，三选一：
- 下载 redis-windows（GitHub 发行版，解压后运行 `redis-server.exe`）
- 安装 Memurai（Windows 原生 Redis 兼容服务）
- 用 WSL / 容器跑 `redis-server`

```bash
# 启动 Redis 服务（未启动时系统自动降级为直连，问答不受影响）
redis-server
```

**API Key 配置**：

```bash
# Windows PowerShell
$env:DASHSCOPE_API_KEY = "your-dashscope-api-key"
$env:DEEPSEEK_API_KEY = "your-deepseek-api-key"
```

### 5.2 第一步：启动文档入库服务

```bash
python app_file_loader.py
```

- 浏览器自动打开 `http://127.0.0.1:7860`
- 拖拽或选择文档（支持 .docx / .pdf / .txt / .md / .xlsx / .xls）
- 点击「解析并入库」
- 查看解析统计（语义块数、表格数、图片数、耗时）

### 5.3 第二步：启动问答服务

```bash
python app_chat.py
```

- 浏览器自动打开 `http://127.0.0.1:7861`
- 输入问题，按 Enter 或点击「发送」
- AI 流式输出回答，支持多轮追问

### 5.4 （服务化，5.0）启动 FastAPI 接口服务

> Gradio 是给人点的演示界面；FastAPI 是给程序调的接口。两者共用同一套 RAG 逻辑，可用其一或同时使用。

```bash
# 需先安装服务化依赖
pip install fastapi uvicorn

# 启动（在 rag_system 目录下，用 python -m 保证 PATH/模块解析正确）
python -m uvicorn app_api:app --host 127.0.0.1 --port 8000 --reload
```

接口清单（浏览器打开 `http://127.0.0.1:8000/docs` 可交互测试）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（真正 ping Redis） |
| POST | `/ingest` | 上传文档 → 解析 → 增量入库（MD5 去重） |
| POST | `/query` | 问答；`stream=true` 走 SSE 流式；`session_id` 隔离会话记忆，缺省自动生成 |
| GET | `/documents` | 列出已入库文档 |

`/query` 调用示例：

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"文档中的关键指标？","session_id":"user-001"}'
```

### 5.5 （容器化，5.1）Docker 一键启动

> Docker 把「代码 + Python 环境 + Redis」打包成标准集装箱，任何装了 Docker 的机器一条命令跑起整个系统，告别环境地狱（阶段 3，L12/L13 落地）。

```bash
# 前置：已安装 Docker Desktop（Windows）并启动引擎

# 第 1 步：配置 API Key（二选一）
#   方式 A：复制 .env.example 为 .env 并填入密钥（.env 已被 .dockerignore 排除，不进镜像/提交）
#   方式 B：在本机环境变量设置 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY（compose 自动透传）

# 第 2 步：一键构建 + 启动（rag 服务 + redis 容器）
docker compose up -d --build

# 第 3 步：验证
docker compose ps                          # 两个容器都 Up
curl.exe http://127.0.0.1:8000/health      # {"status":"ok","redis":true,...}

# 第 4 步：打开前端界面（v5.2）
#   浏览器访问 http://127.0.0.1:8000/ —— 对话 + 上传一体的网页界面，开箱即用
```

- **端口**：8000（与 5.4 本地启动一致，接口清单相同）
- **数据持久化**：`./runtime` 挂载进容器（知识库/索引/会话留在宿主机，容器删了数据不丢）；Redis 数据在命名卷 `redis_data`
- **常用命令**：`docker compose logs rag`（看日志）｜`docker compose down`（停+删容器，数据保留）｜`docker compose up -d --build`（改代码后重建）
- **容器内 Redis 地址**：compose 传 `REDIS_HOST=redis`，代码读环境变量（本机直跑默认 127.0.0.1，行为不变）

### 5.6 命令行直接使用

```python
from loader_factory import LoaderFactory
from knowledge_base import KnowledgeBase

# 解析（自动识别格式）
loader = LoaderFactory.create("文档.pdf")
documents = loader.load()

# 入库
kb = KnowledgeBase()
result = kb.upload_documents("文档.pdf", documents)

# 问答
from qa_service import DocxQAService
qa = DocxQAService()
answer = qa.ask("文档中提到的关键指标有哪些？")
print(answer)
```

---

## 六、核心设计亮点

1. **多格式统一接口**：`BaseLoader` 抽象 + `LoaderFactory` 工厂，新增格式只需加一个 Loader 类并注册，入库和检索层零改动。
2. **表格结构化解析**：docx 遍历 `doc.element.body` 保持段落表格原始顺序，转 Markdown 独立入库；PDF 用 pdfplumber 提取表格；Excel 按 sheet 转表格。
3. **图片描述预计算 + 并行化**：入库时一次性调用视觉模型生成描述存入 metadata（4 线程并行，4.0），查询时零视觉 API 调用。
4. **增量更新**：同名文件修改后重新上传，自动按 source 文件名删除旧数据再插入新版本，无需删库重来。
5. **入库性能优化（4.0，L06）**：图片描述 `ThreadPoolExecutor(4)` 并行 + 向量化按批 `add_texts`（batch=32），大幅减少串行等待与网络往返；MD5 去重保持串行保证正确性。
6. **语义切分**：按文档标题层级切分 chunk，保留语义完整性；超长段落递归分割带重叠窗口。
7. **滑动窗口记忆**：`deque(maxlen=N)` 从文件尾部读取最近 N 轮，内存 O(N)。
8. **网络环境适配（4.1，hotfix）**：IPv4 优先补丁解决本机 IPv6 不通导致 API 每次调用多等 ~42s 的问题（实测 embedding 42.9s → 0.7s，入库 13 图 docx 228s → 11.8s）。
9. **查询改写自动化（4.2）**：入库时 `term_extractor.py` 自动提取文档高频实词生成 `runtime/auto_dict.json`，查询时 `query_rewriter.py` 自动匹配扩展（如"成绩"→"学习成绩"），**全程零手写、随文档自动演进**，彻底摆脱手写词典。
10. **Redis 两级缓存（4.3，L07）**：高频问题命中缓存 → 跳过检索链与 LLM 生成（省 embedding/rerank/DeepSeek 成本）；缓存键用原问题（改写前后命中同一条）；文档更新自动清空缓存；Redis 不可用自动熔断降级为直连，**问答零风险**。
11. **服务化接入（5.0，L10）**：FastAPI 接口层用 `anyio.to_thread.run_sync` 跑同步 RAG 逻辑，等待期间事件循环去服务别的请求（多用户并发不排队）；`/query` 的 `stream=true` 用 SSE + `starlette.concurrency.iterate_in_threadpool` 驱动同步生成器，真·逐 token 推送。
12. **多会话隔离（5.0，L11）**：会话记忆存 Redis Hash `session:{sid}`，每个会话独立多轮历史 + TTL 自动清理；Redis 不可用自动回退按会话分文件的 JSONL 冷备份，贴近生产"热会话（Redis）+ 冷会话（DB）"范式。
13. **容器化交付（5.1，L12/L13）**：Dockerfile（python:3.11-slim + 清华 pip 源解决国内下载超时）+ docker-compose.yml（rag + redis 双容器，compose 服务名互通）+ volume 持久化（`./runtime` 与 `redis_data`），任何装了 Docker 的机器 `docker compose up -d --build` 一键跑起；`config_data.py` 的 Redis 地址改为读 `REDIS_HOST` 环境变量（唯一代码改动，本机默认 127.0.0.1 行为不变）。
14. **前端界面（5.2，ui-ux-pro-max）**：基于 GitHub 开源技能 ui-ux-pro-max-skill 生成的设计系统（极简瑞士风 + 中性灰/链接蓝 tokens + 对话 UX 指南）重做服务化前端——单文件 `static/index.html`（约 27KB，零外部依赖），浏览器开 `http://127.0.0.1:8000/` 即见：对话流式打字机、文档拖拽上传、会话管理、Redis 状态徽章、响应式 375/768/1024/1440。

---

## 七、当前局限与后续方向

### 当前局限
1. ~~纯向量检索~~ **已解决（2026-09-15）**：混合检索（向量 + BM25 + RRF）与 Rerank 精排已落地。
2. ~~无查询改写~~ **已解决（2026-09-15，2026-09-22 升级为自动化）**：查询改写已落地；2026-09-22 起由手写词典升级为**入库自动术语提取**（`term_extractor.py` 自动生成 `runtime/auto_dict.json`，`query_rewriter.py` 查询时自动匹配扩展，全程零手写）。
3. ~~入库性能（串行）~~ **已解决（2026-09-21）**：图片描述 4 线程并行 + 向量按批 32 提交（4.0）；再叠加 IPv4 优先补丁（4.1）解决本机 IPv6 超时，实测同一份 13 图 docx 入库 **228s → 11.8s（约 19 倍提速）**。
4. **本地 Chroma**：不支持并发写入和多租户。
5. **评测体系**：已在学习侧 `rag_learning/` 建立（RAGAS 四项指标 + 人工评测集），主项目未内置评测脚本。
6. **Rerank 实际使用状态**：`reranker.py` 功能已完整实现，但作者本机未开通百炼 `gte-rerank` 模型（403 AccessDenied），实际运行自动降级为 RRF 排序；**使用者如需启用 Rerank，须自行在百炼控制台开通 `gte-rerank` 模型**（通常有免费额度），开通后无需改代码，重启即生效。
7. **Redis 缓存（4.3）**：本机未常驻启动 Redis 服务时自动降级为直连（功能完整可用）；如需缓存生效需自行启动 `redis-server`（未启动只影响命中率，不影响正确性）。
8. **多会话热存储（5.0）**：会话记忆当前存 Redis Hash + JSONL 回退；生产落到 MySQL/pgvector 等持久库、接入完整热/冷分层（如冷会话归档）仍在探索中。

### 后续方向（详见 `versions/IMPROVEMENT_PLAN_5.0.md`）
- P0：语义级查询改写（LLM 改写 / HyDE，补齐词级边界）、访问鉴权 / API Key、会话冷数据持久化归档
- P1：MD5 升级 SQLite 文档元数据表、并发入库任务队列、上下文来源标注
- P2：向量库迁移到 Milvus / pgvector、异步任务队列 + Docker、Langfuse 追踪

---

## 八、项目结构

```
rag_system/
├── config_data.py              # 全局配置
├── loader_factory.py           # Loader 工厂（按扩展名分发）
├── knowledge_base.py           # 通用入库服务（MD5去重+增量更新）
├── qa_service.py               # 问答服务（检索+LLM编排+流式输出）
├── bm25_index.py              # BM25 词法索引（自实现，混合检索）
├── term_extractor.py          # 入库自动术语提取（自动改写，零手写）
├── query_rewriter.py          # 查询改写（自动词典版）
├── reranker.py                # Rerank 精排（API，熔断降级）
├── redis_cache.py              # Redis 两级缓存（4.3，检索+回答，熔断降级）
├── ipv4_patch.py              # IPv4 优先补丁（4.1 hotfix，幂等）
├── memory.py                   # 对话记忆（Redis Hash 会话隔离 + JSONL+滑动窗口）
├── app_api.py                  # FastAPI 服务化入口（5.0，:8000，/health /ingest /query /documents）
├── app_file_loader.py          # 上传界面（Gradio :7860，多格式）
├── app_chat.py                 # 聊天界面（Gradio :7861）
├── loaders/                    # 多格式解析器
│   ├── __init__.py
│   ├── base_loader.py          # 抽象基类
│   ├── docx_loader.py          # Word 解析（含表格）
│   ├── pdf_loader.py           # PDF 解析（含表格）
│   ├── txt_loader.py           # 纯文本/Markdown 解析
│   └── excel_loader.py         # Excel 解析
├── versions/                   # 版本化改进计划快照
│   ├── IMPROVEMENT_PLAN_1.0.md # v1.0 改进计划（历史快照）
│   ├── IMPROVEMENT_PLAN_2.0.md # v2.0 差距分析 + 改造路线（历史快照）
│   ├── IMPROVEMENT_PLAN_3.0.md # v3.0 检索增强版（历史快照）
│   ├── IMPROVEMENT_PLAN_4.0.md # v4.0 入库性能优化版（历史快照）
│   ├── IMPROVEMENT_PLAN_4.1.md # v4.1 性能 hotfix 版（历史快照）
│   ├── IMPROVEMENT_PLAN_4.2.md # v4.2 查询改写自动化版（历史快照）
│   ├── IMPROVEMENT_PLAN_4.3.md # v4.3 Redis 两级缓存版（历史快照）
│   ├── IMPROVEMENT_PLAN_5.0.md # v5.0 服务化 + 多会话隔离版（历史快照）
│   └── IMPROVEMENT_PLAN_5.2.md # v5.2 前端界面版（当前）
├── static/                      # 前端页面（v5.2，浏览器开 http://127.0.0.1:8000/ 即见）
│   └── index.html               # 单文件界面（对话 + 上传 + 会话，ui-ux-pro-max 设计系统）
├── Dockerfile                  # 容器化构建（python:3.11-slim + 清华 pip 源，L12）
├── docker-compose.yml          # 双容器编排（rag + redis，compose 服务名互通，L13）
├── requirements.txt            # Python 依赖清单（容器与本地共用）
├── .dockerignore               # 构建镜像时排除 runtime/.git/open_source/.env 等
├── .env.example                # API Key 配置示例（复制为 .env 使用）
├── open_source/                # 发布副本（GitHub: Olvenint/multi-format-rag）
├── runtime/                    # [自动生成] 运行时数据目录
│   ├── chroma_db/              # Chroma 向量库
│   ├── output/images/          # 提取的图片
│   ├── file_md5.txt            # MD5 去重记录
│   ├── conversation_memory.jsonl  # 全局对话历史
│   ├── auto_dict.json          # 自动术语词典（入库时生成）
│   └── bm25_index.pkl        # BM25 索引（自动生成）
├── CHANGELOG.md                # 版本演进摘要（0 → 5.2.2）
├── DEV_LOG.md                  # 开发日志（每次修改的详细记录，时间线溯源，任务 0 → 26）
├── OPENSOURCE_CHECKLIST.md     # 开源前检查清单（私人）
└── README.md                   # 本文档
```
