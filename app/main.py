"""FastAPI 应用入口。

本地启动::

    DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:55498/postgres \
      .venv/bin/uvicorn app.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from .api_quotes import router as quotes_router
from .errors import install_handlers
from .settings import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="会员空间平台 · 报价单服务",
        version="1.0.0",
        description="报价单（sales_quotes）接口：试算 / 建草稿 / 改草稿 / 改版 / 发送 / 作废 / 列表 / 详情。",
    )
    install_handlers(app)
    app.include_router(quotes_router)

    @app.get("/healthz", tags=["ops"], summary="健康检查")
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "sales-quotes",
            "quote_valid_days": settings.quote_valid_days,
            "engine": "stub-engine-1.0.0",
        }

    return app


app = create_app()
