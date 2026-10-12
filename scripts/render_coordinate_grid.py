#!/usr/bin/env python3
"""为一张或多张截图生成可读的像素坐标网格。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from screenshot_annotator_v2.rendering import render_coordinate_grid


def render_coordinate_grids(
    input_paths: list[str | Path],
    output_dir: str | Path,
    step: int = 100,
) -> list[Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    used_names: set[str] = set()
    for index, raw_path in enumerate(input_paths, start=1):
        input_path = Path(raw_path)
        if not input_path.is_file():
            raise ValueError(f"找不到输入图片：{input_path}")
        stem = input_path.stem or f"image-{index}"
        name = f"{stem}-grid.png"
        if name in used_names:
            name = f"{stem}-{index:03d}-grid.png"
        used_names.add(name)
        output_path = directory / name
        if output_path.exists():
            raise FileExistsError(f"坐标网格已存在，拒绝覆盖：{output_path}")
        render_coordinate_grid(input_path, output_path, step)
        outputs.append(output_path)
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        type=Path,
        help="输入图片；可重复传入",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="网格图片输出目录")
    parser.add_argument("--step", type=int, default=100, help="网格间距，默认 100px")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        outputs = render_coordinate_grids(args.input, args.output_dir, args.step)
    except (ValueError, FileExistsError, OSError) as exc:
        raise SystemExit(f"坐标网格生成失败：{exc}") from exc
    print(
        json.dumps(
            {"status": "created", "files": [str(path) for path in outputs]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

