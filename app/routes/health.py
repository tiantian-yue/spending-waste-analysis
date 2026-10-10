"""健康检查路由。

用途：部署自检、答辩演示前的启动确认。
"""

from flask import Blueprint

bp = Blueprint("health", __name__)


@bp.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "textbook-trading"}
