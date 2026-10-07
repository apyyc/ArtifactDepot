"""ArtifactDepot — 对象存储仓库站点（FastAPI 入口）

行业定位：对象存储/工件仓库（S3 模型）。收存处理产物（视频等），
提供上传/列表/下载(Range)/删除/预签名 URL API + 网页浏览。
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from artifactdepot import storage
from artifactdepot.api.docs import router as docs_router
from artifactdepot.api.objects import router as objects_router
from artifactdepot.api.system import router as system_router
from artifactdepot.config import config_warnings, get_config
from artifactdepot.logging_utils import install_access_log_redaction
from artifactdepot.web.ui import router as web_router


logger = logging.getLogger("artifactdepot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 访问日志脱敏（token 等 query 参数不入日志）
    install_access_log_redaction()
    # 启动配置体检：默认仅告警；ARTIFACT_DEPOT_STRICT_CONFIG=1 时直接拒绝启动
    warnings = config_warnings()
    for w in warnings:
        logger.warning("[配置] %s", w)
    if warnings and os.getenv("ARTIFACT_DEPOT_STRICT_CONFIG", "").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError("配置校验未通过：" + "；".join(warnings))
    # 确保仓库根目录存在，并从 DataHub 拉取用户 token（启动即最新）
    root = storage.get_root()
    root.mkdir(parents=True, exist_ok=True)
    await storage.sync_tokens_from_datahub(get_config().get("datahub_url", ""), force=True)
    yield


_STATIC_DIR = Path(__file__).resolve().parent / "resources" / "static"
_FAVICON = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

app = FastAPI(title="ArtifactDepot - Object Storage", version="0.7.2", lifespan=lifespan,
              docs_url=None, redoc_url=None)

# CORS / TrustedHost：默认都不限制（内网部署）；生产按 config 收紧
_cfg = get_config()
if _cfg.get("cors_origins"):
    app.add_middleware(CORSMiddleware,
                       allow_origins=list(_cfg["cors_origins"]),
                       allow_methods=["*"], allow_headers=["*"],
                       allow_credentials=False)
if _cfg.get("allowed_hosts") and list(_cfg["allowed_hosts"]) != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_cfg["allowed_hosts"]))


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """基础安全响应头（CSP 暂不加：UI/Swagger 依赖内联脚本）。"""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return resp


# Swagger / ReDoc 静态资源自托管（内网无需 CDN）
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/docs", include_in_schema=False)
    async def _swagger_ui():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url, title=app.title + " - Swagger UI",
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
            swagger_favicon_url=_FAVICON)

    @app.get("/redoc", include_in_schema=False)
    async def _redoc_ui():
        return get_redoc_html(
            openapi_url=app.openapi_url, title=app.title + " - ReDoc",
            redoc_js_url="/static/redoc.standalone.js",
            redoc_favicon_url=_FAVICON)


app.include_router(system_router)
app.include_router(objects_router)
app.include_router(docs_router)
app.include_router(web_router)
