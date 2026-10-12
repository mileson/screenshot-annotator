#!/usr/bin/env python3
"""独立重读并校验 screenshot-annotator v2 的一个 vNNN 版本目录。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from screenshot_annotator_v2.validator import validate_project as _validate_project


def validate_project(version_dir: str | Path) -> dict[str, Any]:
    """可导入入口；只读校验版本目录，不把人工复核标记为通过。"""

    return _validate_project(version_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version_dir", nargs="?", type=Path, help="v001 等版本目录")
    parser.add_argument(
        "--version-dir",
        dest="version_dir_option",
        type=Path,
        help="v001 等版本目录（与位置参数二选一）",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="可选：把本次只读校验结果另存为 JSON；不会写入版本目录",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    version_dir = args.version_dir_option or args.version_dir
    if version_dir is None:
        raise SystemExit("校验失败：必须提供版本目录。")
    summary = validate_project(version_dir)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        output = args.json_output.expanduser().resolve()
        version_root = Path(version_dir).expanduser().resolve()
        try:
            common = os.path.commonpath((str(version_root), str(output)))
        except ValueError as exc:
            raise SystemExit(f"校验失败：输出路径无效：{output}") from exc
        if common == str(version_root):
            raise SystemExit(
                "校验失败：--json-output 必须位于版本目录之外，"
                "避免污染不可变产物清单。"
            )
        if output.exists():
            raise SystemExit(f"校验失败：输出文件已存在，拒绝覆盖：{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if summary.get("status") == "blocked":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
