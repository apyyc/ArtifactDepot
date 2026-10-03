"""文档 API：向前端「API 文档」入口提供项目内的 Markdown 文档。

- `GET /api/docs`            列出可用文档（含文件是否存在）
- `GET /api/docs/{name}`     返回文档内容（JSON 包装，统一响应结构）
- `GET /api/docs/{name}?raw=1` 直接返回 text/plain（便于新窗口查看原文件）

文档按**项目根**解析：开发态 `<repo>/docs/...`，容器内 `/app/artifactdepot/docs/...`
（Dockerfile 已 COPY docs/ 与 README/CHANGELOG）。文件缺失时返回 404 并给出可读提示，前端可降级。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/docs", tags=["Docs"])

# 文档白名单：name -> (标题, 相对项目根的路径)；只暴露这几个文件，避免任意路径读取
CATALOG = (
    ("api_rules", "接口文档（docs/api_rules.md）", "docs/api_rules.md"),
    ("architecture", "架构说明（docs/architecture.md）", "docs/architecture.md"),
    ("readme", "项目说明（README.md）", "README.md"),
    ("changelog", "变更日志（CHANGELOG.md）", "CHANGELOG.md"),
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _path(rel: str) -> Path:
    return PROJECT_ROOT / rel


def _lookup(name: str):
    for key, title, rel in CATALOG:
        if key == name:
            return title, _path(rel)
    return None, None


@router.get("")
async def list_docs():
    """列出可查看的文档（公开）：前端渲染下拉框"""
    items = [{"name": key, "title": title, "available": _path(rel).is_file()}
             for key, title, rel in CATALOG]
    return {"code": 0, "message": "success", "data": items}


@router.get("/{name}")
async def get_doc(name: str, raw: bool = Query(False, description="true = 直接返回 text/plain")):
    """返回指定文档内容（公开）。容器镜像未包含 docs/ 时返回 404 + 明确原因。"""
    title, path = _lookup(name)
    if path is None:
        raise HTTPException(404, detail=f"文档不存在：{name}")
    if not path.is_file():
        raise HTTPException(404, detail=f"文档文件缺失：{name}（镜像未包含文档，请重建镜像）")
    content = path.read_text(encoding="utf-8")
    if raw:
        return PlainTextResponse(content, media_type="text/plain; charset=utf-8")
    return {"code": 0, "message": "success",
            "data": {"name": name, "title": title, "content": content}}
