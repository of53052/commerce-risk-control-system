"""FastAPI 应用入口。

P0 阶段该文件只做三件事：
1. 装配日志、CORS（开发态前端 5173 跨端口访问）；
2. 提供 /healthz 健康检查（MySQL / Redis 连通性 + 版本信息），供 dev.ps1 与排障使用；
3. 预留路由注册位（P0 后续步骤与 P1/P2 逐步挂载）。
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import engine

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
