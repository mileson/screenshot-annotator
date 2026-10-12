"""截图标注助手 v2 的批量渲染与独立校验接口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .engine import run_batch


def validate_project(version_dir: str | Path) -> dict[str, Any]:
    """延迟导入独立校验器，避免批量入口依赖加载顺序。"""

    from .validator import validate_project as _validate_project

    return _validate_project(version_dir)

__all__ = ["run_batch", "validate_project"]
