#!/usr/bin/env python3
"""Suggest target rectangles from scene semantics and lightweight pixel evidence."""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Iterable

from PIL import Image


ROW_SCENES = {"menu_row", "option_row", "file_result_row", "message_row", "composite_step"}


def parse_box(values: Iterable[int], width: int, height: int) -> tuple[int, int, int, int]:
    box = tuple(int(value) for value in values)
    if len(box) != 4 or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
        raise ValueError(f"无效坐标：{box}")
    return box  # type: ignore[return-value]


def suggest_bottom_nav(
    container: tuple[int, int, int, int], index: int, count: int, inset: int = 0
) -> dict:
    if count < 1 or not 0 <= index < count:
        raise ValueError("bottom_nav_item 的 index/count 无效。")
    left, top, right, bottom = container
    cell_left = round(left + (right - left) * index / count)
    cell_right = round(left + (right - left) * (index + 1) / count)
    rect = [cell_left + inset, top + inset, cell_right - inset, bottom - inset]
    return {
        "rect": rect,
        "confidence": 0.72,
        "evidence": ["equal_width_navigation_container", "container_bounds", "requires_visual_container_review"],
    }


def _row_change_score(gray: Image.Image, y: int, left: int, right: int) -> float:
    if y <= 0 or y >= gray.height:
        return 0.0
    current = gray.crop((left, y, right, y + 1))
    previous = gray.crop((left, y - 1, right, y))
    a = list(current.getdata())
    b = list(previous.getdata())
    return sum(abs(int(x) - int(yv)) for x, yv in zip(a, b)) / max(1, len(a))


def suggest_row(
    image: Image.Image,
    seed: tuple[int, int, int, int],
    container: tuple[int, int, int, int] | None = None,
    search_radius: int = 220,
) -> dict:
    gray = image.convert("L")
    c_left, c_top, c_right, c_bottom = container or (0, 0, image.width, image.height)
    center_y = (seed[1] + seed[3]) // 2
    top_range = range(max(c_top + 1, center_y - search_radius), max(c_top + 2, seed[1]))
    bottom_range = range(min(c_bottom - 1, seed[3] + 1), min(c_bottom, center_y + search_radius))
    top_candidates = [(y, _row_change_score(gray, y, c_left, c_right)) for y in top_range]
    bottom_candidates = [(y, _row_change_score(gray, y, c_left, c_right)) for y in bottom_range]
    top = max(top_candidates, key=lambda item: item[1]) if top_candidates else (seed[1], 0.0)
    bottom = max(bottom_candidates, key=lambda item: item[1]) if bottom_candidates else (seed[3], 0.0)
    rect = [c_left, min(seed[1], top[0]), c_right, max(seed[3], bottom[0])]
    strength = min(1.0, (top[1] + bottom[1]) / 30.0)
    return {
        "rect": rect,
        "confidence": round(0.55 + 0.4 * strength, 3),
        "evidence": ["ocr_or_manual_seed", "horizontal_boundary_contrast"],
        "boundary_scores": {"top": round(top[1], 3), "bottom": round(bottom[1], 3)},
    }


def suggest_color_component(image: Image.Image, seed_point: tuple[int, int], tolerance: int = 42) -> dict:
    rgb = image.convert("RGB")
    width, height = rgb.size
    sx, sy = seed_point
    if not (0 <= sx < width and 0 <= sy < height):
        raise ValueError("seed_point 越界。")
    reference = rgb.getpixel((sx, sy))

    def similar(point: tuple[int, int]) -> bool:
        value = rgb.getpixel(point)
        return sum(abs(int(value[i]) - int(reference[i])) for i in range(3)) <= tolerance

    queue = deque([(sx, sy)])
    visited = {(sx, sy)}
    component = []
    maximum = min(width * height, 500_000)
    while queue and len(component) < maximum:
        point = queue.popleft()
        if not similar(point):
            continue
        component.append(point)
        x, y = point
        for candidate in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= candidate[0] < width and 0 <= candidate[1] < height and candidate not in visited:
                visited.add(candidate)
                queue.append(candidate)
    if not component:
        raise ValueError("未找到与种子颜色连通的按钮区域。")
    xs = [point[0] for point in component]
    ys = [point[1] for point in component]
    rect = [min(xs), min(ys), max(xs) + 1, max(ys) + 1]
    return {
        "rect": rect,
        "confidence": 0.9 if len(component) >= 100 else 0.65,
        "evidence": ["seed_color", "connected_component"],
        "component_pixels": len(component),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按教程场景生成目标框候选，不直接修改图片。")
    parser.add_argument("image", type=Path)
    parser.add_argument("--scene", required=True, choices=sorted(ROW_SCENES | {"bottom_nav_item", "action_button"}))
    parser.add_argument("--seed-box", nargs=4, type=int)
    parser.add_argument("--container-box", nargs=4, type=int)
    parser.add_argument("--seed-point", nargs=2, type=int)
    parser.add_argument("--index", type=int)
    parser.add_argument("--count", type=int)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with Image.open(args.image) as opened:
        image = opened.convert("RGB")
    container = parse_box(args.container_box, image.width, image.height) if args.container_box else None
    if args.scene == "bottom_nav_item":
        if container is None or args.index is None or args.count is None:
            raise SystemExit("bottom_nav_item 需要 --container-box、--index 和 --count")
        candidate = suggest_bottom_nav(container, args.index, args.count)
    elif args.scene == "action_button":
        if args.seed_point is None:
            raise SystemExit("action_button 需要 --seed-point")
        candidate = suggest_color_component(image, tuple(args.seed_point))
    else:
        if args.seed_box is None:
            raise SystemExit(f"{args.scene} 需要 --seed-box")
        candidate = suggest_row(image, parse_box(args.seed_box, image.width, image.height), container)
    payload = {"schema_version": 1, "scene": args.scene, "candidates": [candidate]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
