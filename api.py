"""
对外 HTTP 接口（/api/v1）

挂载方式刻意选得最简单：Gradio 的 `App` 本身就是 `fastapi.FastAPI` 的子类
（`gradio.routes.App.__mro__` → `fastapi.applications.FastAPI`），
`launch()` 里会 `App.create_app(...)` 把它造出来。所以只要给 `launch()`
传 `app_kwargs={"routes": ...}`，自定义路由就进了同一个 app，
不需要换成 `uvicorn.run(mount_gradio_app(...))` —— 那样会打断
desktop.py 里 `demo.launch(prevent_thread_lock=True)` 的启动方式。

端点设计原则：
- 长耗时用 SSE 推步骤，而不是让客户端干等一个 60 秒的响应体
  （Agent 一轮可能走 5~8 次工具调用，普通 HTTP 客户端默认 30 秒就超时了）
- 会话用 session_id 显式隔离，不依赖 cookie
- 默认只读：能读文档、能检索，不能写文件、不能执行命令
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from myagent.config import config
from myagent.origin import reset_origin, set_origin
from myagent.tools import external_tool_specs

VERSION = "0.2.0"

# 由 web.py 在初始化完成后注入（避免 api.py 反向 import web 造成循环）
_STATE: dict[str, Any] = {"pool": None, "rag": None, "started": time.time()}

_token_lock = threading.Lock()
_token_value: str | None = None


# ---------- 注入 ----------


def bind(pool: Any, rag: Any = None) -> None:
    """把会话池和检索器接进来。必须在 launch 之前调用。"""
    _STATE["pool"] = pool
    _STATE["rag"] = rag


# ---------- 鉴权 ----------


def auth_enabled() -> bool:
    raw = (os.getenv("API_AUTH", "on") or "on").strip().lower()
    return raw not in ("off", "0", "false", "no")


def api_token() -> str:
    """
    取本次运行的 token。

    优先级：API_TOKEN 环境变量 → 数据目录里已保存的 → 新生成并落盘。
    落盘是为了「服务重启后调用方不用改配置」，文件放在数据目录而不是
    项目目录，避免被 git 带走。
    """
    global _token_value
    with _token_lock:
        if _token_value:
            return _token_value

        env = (os.getenv("API_TOKEN") or "").strip()
        if env:
            _token_value = env
            return env

        path = config.data_dir / "api_token.txt"
        try:
            if path.exists():
                saved = path.read_text(encoding="utf-8").strip()
                if saved:
                    _token_value = saved
                    return saved
        except OSError:
            pass

        fresh = secrets.token_urlsafe(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fresh, encoding="utf-8")
        except OSError:
            # 写不了也要能用，只是重启后 token 会变
            pass
        _token_value = fresh
        return fresh


# 用 HTTPBearer 而不是手写 Header：这样 Swagger UI 上会出现 Authorize 按钮，
# 调用方不用自己拼 curl -H，也能看清哪些端点要鉴权
_bearer = HTTPBearer(auto_error=False)


def require_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not auth_enabled():
        return
    got = (creds.credentials if creds else "") or ""
    # 定长比较，别用 == 泄漏前缀信息
    if not got or not hmac.compare_digest(got, api_token()):
        raise HTTPException(
            status_code=401,
            detail="需要 Authorization: Bearer <token>，token 见数据目录下的 api_token.txt",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _pool():
    pool = _STATE.get("pool")
    if pool is None:
        raise HTTPException(status_code=503, detail="Agent 尚未就绪，请稍后重试")
    return pool


# ---------- 请求/响应模型 ----------


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, description="用户这句话")
    session_id: str = Field("default", description="会话标识，同一 id 共享上下文")


class ChatOut(BaseModel):
    reply: str
    session_id: str
    elapsed_ms: int


class SearchIn(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(4, ge=1, le=20)


# ---------- 免鉴权：探活 ----------

public = APIRouter(prefix="/api/v1", tags=["public"])


@public.get("/health", summary="探活")
def health() -> dict:
    pool = _STATE.get("pool")
    rag = _STATE.get("rag")
    segments = None
    if rag is not None:
        try:
            segments = rag.stats().get("knowledge_chunks")
        except Exception:
            segments = None
    return {
        "status": "ok" if pool is not None else "starting",
        "version": VERSION,
        "uptime_seconds": int(time.time() - _STATE["started"]),
        "sessions": pool.stats()["sessions"] if pool is not None else 0,
        "knowledge_segments": segments,
        "auth": "bearer" if auth_enabled() else "off",
    }


# ---------- 接口文档 ----------
#
# Gradio 的 App.create_app 把 docs_url / redoc_url 都设成了 None，
# 也就是说 /docs 是它主动禁掉的，不是挂载失败。直接把它那份 openapi.json
# 喂给 Swagger 也不好看 —— 里面混着几十个 gradio 内部端点。
# 这里单独造一个只含自己路由的 FastAPI，专门用来生成 schema。
_doc_app: FastAPI | None = None


def _build_doc_app() -> FastAPI:
    doc = FastAPI(title="XY Agent API", version=VERSION, docs_url=None, redoc_url=None)
    doc.include_router(public)
    doc.include_router(router)
    return doc


@public.get("/openapi.json", include_in_schema=False)
def openapi_json() -> dict:
    global _doc_app
    if _doc_app is None:
        _doc_app = _build_doc_app()
    return _doc_app.openapi()


@public.get("/docs", include_in_schema=False)
def docs_page():
    return get_swagger_ui_html(openapi_url="/api/v1/openapi.json", title="XY Agent API")


# ---------- 需要鉴权 ----------

router = APIRouter(prefix="/api/v1", tags=["xy"], dependencies=[Depends(require_token)])


@router.get("/tools", summary="可用工具清单")
def tools() -> dict:
    specs = external_tool_specs()
    return {
        "count": len(specs),
        "tools": [
            {
                "name": s["name"],
                "description": s.get("description", ""),
                "parameters": s.get("parameters", {}),
            }
            for s in specs
        ],
    }


@router.post("/chat", response_model=ChatOut, summary="一次问答（等结果）")
def chat(body: ChatIn) -> ChatOut:
    pool = _pool()
    token = set_origin("api")
    t0 = time.time()
    try:
        reply = pool.run(body.session_id, body.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    finally:
        reset_origin(token)
    return ChatOut(
        reply=reply,
        session_id=body.session_id,
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream", summary="一次问答（SSE 推步骤）")
def chat_stream(body: ChatIn) -> StreamingResponse:
    """
    为什么要流式：Agent 一轮可能 5~8 次工具调用、跑 30~60 秒。
    普通 HTTP 客户端默认 30 秒超时，等不到结果就断开，而我们这边还在跑，
    结果直接丢掉。推流 + 15 秒心跳能让任意客户端安全地等下去。
    """
    pool = _pool()
    q: queue.Queue = queue.Queue()
    box: dict[str, Any] = {}

    def worker() -> None:
        token = set_origin("api")
        t0 = time.time()
        try:
            box["reply"] = pool.run(
                body.session_id,
                body.message,
                on_step=lambda s: q.put(("step", s)),
            )
        except Exception as e:
            box["error"] = f"{type(e).__name__}: {e}"
        finally:
            box["elapsed_ms"] = int((time.time() - t0) * 1000)
            reset_origin(token)
            q.put(("done", None))

    threading.Thread(target=worker, daemon=True, name="xy-api-stream").start()

    def gen():
        yield _sse("start", {"session_id": body.session_id})
        while True:
            try:
                kind, payload = q.get(timeout=15)
            except queue.Empty:
                # 注释行不触发客户端 onmessage，但能让连接保持活着
                yield ": keep-alive\n\n"
                continue
            if kind == "step":
                yield _sse("step", {"text": payload})
                continue
            break

        if "error" in box:
            yield _sse("error", {"message": box["error"]})
        else:
            yield _sse(
                "done",
                {
                    "reply": box.get("reply", ""),
                    "session_id": body.session_id,
                    "elapsed_ms": box.get("elapsed_ms", 0),
                },
            )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/kb/stats", summary="知识库统计")
def kb_stats() -> dict:
    rag = _STATE.get("rag")
    if rag is None:
        raise HTTPException(status_code=503, detail="检索器未就绪")
    try:
        return rag.stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


@router.post("/kb/search", summary="语义检索")
def kb_search(body: SearchIn) -> dict:
    rag = _STATE.get("rag")
    if rag is None:
        raise HTTPException(status_code=503, detail="检索器未就绪")
    try:
        hits = rag.search(body.query, top_k=body.top_k)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
    return {
        "query": body.query,
        "hits": [
            {
                "text": h.get("text", ""),
                "score": round(float(h.get("score", 0.0)), 4),
                # search() 直接把来源放在顶层 source 上，不在 metadata 里
                "source": h.get("source"),
            }
            for h in hits
        ],
    }


@router.get("/sessions", summary="会话池状态")
def sessions() -> dict:
    return _pool().stats()


@router.delete("/sessions/{session_id}", summary="结束会话")
def drop_session(session_id: str) -> dict:
    return {"dropped": _pool().drop(session_id), "session_id": session_id}


def all_routes() -> list:
    """交给 demo.launch(app_kwargs={"routes": ...}) 用。"""
    return list(public.routes) + list(router.routes)
