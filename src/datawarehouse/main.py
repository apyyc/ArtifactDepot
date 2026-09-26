"""DataWarehouse — 对象存储仓库站点（FastAPI 入口）

行业定位：对象存储/工件仓库（S3 模型）。收存处理产物（视频等），
提供上传/列表/下载(Range)/删除/预签名 URL API + 网页浏览。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from datawarehouse import storage
from datawarehouse.api.objects import router as objects_router
from datawarehouse.api.system import router as system_router
from datawarehouse.config import get_config
from datawarehouse.web.ui import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 确保仓库根目录存在，并从 DataHub 拉取用户 token（启动即最新）
    root = storage.get_root()
    root.mkdir(parents=True, exist_ok=True)
    await storage.sync_tokens_from_datahub(get_config().get("datahub_url", ""), force=True)
    yield


app = FastAPI(title="DataWarehouse - Object Storage", version="0.6.0", lifespan=lifespan)
app.include_router(system_router)
app.include_router(objects_router)
app.include_router(web_router)
