"""网页 UI：单页目录浏览 + 上传 + 下载"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from datawarehouse.config import get_config

router = APIRouter(tags=["Web"])


def _index_html() -> str:
    p = Path(__file__).resolve().parent.parent / "resources" / "index.html"
    return p.read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
async def index():
    if not get_config().get("ui_enabled", True):
        return HTMLResponse("<h3>Web UI 已禁用（config.ui_enabled=false）</h3>", status_code=403)
    return _index_html()
