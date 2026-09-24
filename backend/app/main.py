"""FastAPI 应用入口。

该文件只做装配，不写业务逻辑：
1. 日志、CORS（开发态前端 5173 跨端口访问）、trace_id 中间件；
2. 全局异常处理器（统一错误结构，见 app/api/errors.py）；
3. 路由注册（事件接入、认证；P1/P2 继续挂载案件/大盘等）；
4. /healthz 健康检查（MySQL / Redis 连通性 + 版本信息），供 dev.ps1 与排障使用。

**trace_id 中间件的必要性**：一次失败的排障始于"把响应里的 trace_id 给我"。
若前端不来带、服务端也不生成，日志里的每条记录都是孤立的，
在并发场景下无法归属到具体某次请求。因此这里无条件生成/透传，
并在响应头回写，前端报错时可直接附上。
"""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.api import auth as auth_api
from app.api import events as events_api
from app.api.errors import register_exception_handlers
from app.core.config import settings
from app.core.logging import setup_logging, trace_id_ctx
from app.db.session import engine
import app.models  # noqa: F401 - 触发模型注册与「名单标记特征」的导入期回填

setup_logging()
logger = logging.getLogger("app.main")

app = FastAPI(
    title="电商风险控制系统",
    description="实时风控决策与管理服务（P0 决策底座）",
    version="0.1.0",
)

# 开发态：前端 Vite dev server 与本服务不同端口，需要放开跨域。
# 注意：这是本地演示配置，若对外暴露必须收紧到具体域名（docs/ARCHITECTURE.md §10）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局异常处理器必须在注册路由之前挂上：FastAPI 的中间件栈在第一次请求时固化，
# 后挂的处理器对已经"编译"过的路由不生效（表现为异常穿过到 500 HTML）。
register_exception_handlers(app)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """为每次请求分配 trace_id，并写进响应头。"""
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    trace_id_ctx.set(trace_id)
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response


# ---- 路由注册 ----
app.include_router(auth_api.router)
app.include_router(events_api.router)


@app.get("/healthz", tags=["system"], summary="健康检查")
def healthz() -> JSONResponse:
    """返回依赖健康状况。

    任一依赖不健康时返回 503，让启动脚本/监控可以据此判断，
    而不是"接口能通但用不了"（AGENTS.md §6.3 反对半成品交付）。
    """
    result: dict = {"app": "ok", "env": settings.APP_ENV}
    healthy = True

    # MySQL
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
        result["mysql"] = {"status": "ok", "version": version, "database": settings.DB_NAME}
    except Exception as exc:  # noqa: BLE001 - 健康检查需要吞掉异常并如实上报
        healthy = False
        result["mysql"] = {"status": "error", "detail": str(exc)[:200]}

    # Redis
    try:
        client = Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            socket_connect_timeout=1,
        )
        client.ping()
        info = client.info("server")
        result["redis"] = {"status": "ok", "version": info.get("redis_version")}
    except Exception as exc:  # noqa: BLE001
        healthy = False
        result["redis"] = {"status": "error", "detail": str(exc)[:200]}

    result["status"] = "ok" if healthy else "degraded"
    return JSONResponse(content=result, status_code=200 if healthy else 503)
