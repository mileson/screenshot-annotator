#!/usr/bin/env python3
"""按 schema_version=2.0 manifest 原子生成一版批量截图标注项目。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from screenshot_annotator_v2.engine import run_batch as _run_batch


def run_batch(
    manifest_path: str | Path,
    output_root: str | Path,
    version: str | int | None = None,
) -> Path:
    """可导入入口；成功时返回新建的 ``vNNN`` 目录。"""

    return _run_batch(manifest_path, output_root, version)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        "--plan",
        dest="manifest",
        required=True,
        type=Path,
        help="schema_version=2.0 的批量作业 JSON",
    )
    parser.add_argument(
        "--output-root",
        "--output-dir",
        dest="output_root",
        required=True,
        type=Path,
        help=(
            "项目输出根目录；脚本会新建 "
            "<job_id>/versions/v001 与 <job_id>/reviews/v001"
        ),
    )
    parser.add_argument(
        "--version",
        help="可选的显式版本号（v001 到 v999）；已存在时拒绝覆盖",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        version_dir = run_batch(args.manifest, args.output_root, args.version)
    except (ValueError, FileExistsError, OSError) as exc:
        raise SystemExit(f"批量标注失败：{exc}") from exc
    payload: dict[str, Any] = {
        "status": "created",
        "version_dir": str(version_dir),
        "manifest": str(version_dir / "manifest.json"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
