"""
RAG 系统 FastAPI 服务化入口（v5.0，服务化版）

把 v4.x 的 RAG 能力（入库 / 问答 / 检索缓存 / 多会话记忆）包装成程序可调的 REST API。

启动命令（在 rag_system 目录下）：
    python -m uvicorn app_api:app --host 127.0.0.1 --port 8000 --reload

接口清单（自动文档：打开浏览器访问 http://127.0.0.1:8000/docs 查看并可点 Try it out 实测）：
    GET    /health               健康检查（真正 ping Redis，非只看开关）
    POST   /ingest               上传文档 → 解析 → 向量化增量入库（MD5 去重）→ 返回入库统计
    POST   /query                问答（收 session_id → 多会话记忆；stream=true 走 SSE 流式）
    GET    /documents            列出已入库文档（读 Chroma 的 source 去重）

与 Gradio 的关系：
    - app_chat.py / app_file_loader.py 是给人点的演示界面，保留不动；
    - 本文件是给程序调的接口，任一前端/小程序/其他后端发 HTTP JSON 即可使用同一套 RAG 逻辑。
      （Gradio 仅供本地演示，不推送到 open_source / github）

异步与流式（对应 L10）：
    qa_service 保持同步方法不动，接口层用 anyio.to_thread.run_sync 包一层，
    等待期间事件循环去服务别的请求，多用户并发不排队；流式用
    starlette.concurrency.iterate_in_threadpool 驱动同步生成器，真·逐 token 推送。
"""
import os
import sys
import tempfile
import uuid

# ---- sys.path 兜底：按脚本所在目录定位，保证平级 import（qa_service 等）从本目录触达 ----
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import anyio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool  # 驱动同步生成器的异步迭代（L10 真·逐字流式）

import config_data as config
from qa_service import DocxQAService
from loader_factory import LoaderFactory
from knowledge_base import KnowledgeBase


# ============================================================
# FastAPI 应用对象（title/version 会显示在 /docs 页面）
# ============================================================
app = FastAPI(title="multi-format-rag API", version="5.0.0")

# 全局只建一次问答服务（加载/连接一次，所有请求共用），避免重复初始化
qa = DocxQAService()


# ============================================================
# GET / —— 前端页面（static/index.html，ui-ux-pro-max 设计系统）
# ============================================================
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(_THIS_DIR, "static", "index.html"))


# ============================================================
# 请求体"报名表"（Pydantic）：客户端必须按此格式发 /query
# ============================================================
class QueryRequest(BaseModel):
    query: str = Field(..., description="必填：用户问题")          # 必填，字符串
    session_id: str = Field(None, description="可选：会话标识。缺省时服务端自动生成")  # 多会话（L11）
    stream: bool = Field(False, description="可选：True 走 SSE 流式回答，False 一次性返回")


# ============================================================
# GET /health —— 健康检查：服务活着没 + 真的 ping 一下 Redis
# ============================================================
@app.get("/health")
def health():
    redis_ok = False
    if config.REDIS_ENABLED:
        # 真正 ping（连接失败会抛异常），而不是只看配置开关——这才是"活没活"
        try:
            import redis
            client = redis.Redis(
                host=config.REDIS_HOST, port=config.REDIS_PORT,
                db=config.REDIS_DB, password=config.REDIS_PASSWORD,
                socket_timeout=config.REDIS_SOCKET_TIMEOUT,
            )
            redis_ok = bool(client.ping())
        except Exception:
            redis_ok = False
    return {"status": "ok", "version": "5.0.0", "redis": redis_ok}


# ============================================================
# POST /ingest —— 上传文件入库：Loader 解析 → 增量入库（MD5 去重）→ 清缓存 → 返回统计
# ============================================================
@app.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    # 1) 校验扩展名是否支持
    if not LoaderFactory.is_supported(file.filename or ""):
        supported = ", ".join(LoaderFactory.supported_extensions())
        raise HTTPException(status_code=400, detail=f"不支持的文件格式，支持：{supported}")

    # 2) 存到临时文件（Loader 需要本地路径；中文文件名容易被中间件改坏，这里统一重命名）
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            suffix=os.path.splitext(file.filename or "")[1], dir=config.RUNTIME_DIR
        )
        with open(fd, "wb") as f:
            f.write(await file.read())

        # 3) 解析 + 入库（同步慢活丢线程池，不让事件循环死等）
        def _do_ingest():
            loader = LoaderFactory.create(tmp_path)
            documents = loader.load()
            return KnowledgeBase().upload_documents(
                tmp_path, documents, source_name=file.filename or ""
            )

        result = await anyio.to_thread.run_sync(_do_ingest)
        return {"status": "ok", **result, "filename": file.filename}
    except HTTPException:
        raise
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"解析缺少依赖：{e}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"入库失败：{str(e)[:200]}") from e
    finally:
        # 清理临时文件，避免 /runtime 堆积
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ============================================================
# POST /query —— 问答：非流式一次返回；stream=true 走 SSE 逐字流式
# ============================================================
@app.post("/query")
async def query(req: QueryRequest):
    # 缺省 session_id → 服务端自动生成一个，回给客户端；下次带上即恢复该会话记忆
    session_id = req.session_id or str(uuid.uuid4())

    if req.stream:
        # 流式分支（L10）：iterate_in_threadpool 把同步生成器丢线程池逐段驱动，
        # 返回 async iterable（用 async for 消费），实现真·打字机效果。
        # 首个事件先推 session_id（前端据此保存/恢复会话），再逐字推文本片段。
        async def gen():
            yield f"data: {{\"session_id\": \"{session_id}\"}}\n\n"
            async for chunk in iterate_in_threadpool(
                qa.ask_stream(req.query, session_id)
            ):
                yield f"data: {chunk}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    # 非流式分支：同步 ask 丢线程池跑，等待期间事件循环去服务别的请求（L10）
    answer = await anyio.to_thread.run_sync(qa.ask, req.query, session_id)
    return {"answer": answer, "session_id": session_id}


# ============================================================
# GET /documents —— 列出已入库文档（读 Chroma 按 source 去重取文件名）
# ============================================================
@app.get("/documents")
def documents():
    try:
        # 从 Chroma 集合抓取元数据里的 source（入库时 source = 原文件名），去重列出
        chroma = qa.retriever.chroma
        source_names = set()
        # 用原生 collection 读所有 metadatas（列举全量，不只检索 topk）
        raw = chroma._collection.get(include=["metadatas"], limit=10000)
        for md in raw.get("metadatas") or []:
            src = (md or {}).get("source")
            if src:
                source_names.add(src)
        return {"documents": sorted(source_names), "count": len(source_names)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"列文档失败：{str(e)[:200]}") from e


# ============================================================
# 提示：直接运行本文件只打印启动命令（uvicorn 才真正开服务）
# ============================================================
if __name__ == "__main__":
    print("请用以下命令启动：")
    print("  python -m uvicorn app_api:app --host 127.0.0.1 --port 8000 --reload")