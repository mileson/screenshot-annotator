#!/usr/bin/env python3
"""根据 JSON 计划在截图上渲染红色教程标注。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "缺少 Pillow。可安装：python3 -m pip install Pillow；"
        "或使用 Codex 打包 Python 运行时。"
    ) from exc


DEFAULT_STYLE = {
    "color": "#E62323",
    "text_color": "#FFFFFF",
    "line_width": 7,
    "radius": 14,
    "badge_radius": 28,
    "badge_font_size": 34,
    "crop_padding": 90,
}

FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="原始截图路径")
    parser.add_argument("--plan", required=True, type=Path, help="标注计划 JSON")
    parser.add_argument("--output", required=True, type=Path, help="标注图输出路径")
    parser.add_argument(
        "--qa-dir",
        type=Path,
        help="可选 QA 目录，用于保存裁切图、contact sheet、review.json 和 qa-summary.json",
    )
    return parser.parse_args()


def load_plan(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        plan = json.load(handle)
    if not isinstance(plan, dict):
        raise ValueError("标注计划必须是 JSON object。")
    annotations = plan.get("annotations")
    if not isinstance(annotations, list) or not annotations:
        raise ValueError("标注计划必须包含非空 annotations 数组。")
    return plan


def style_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    style = dict(DEFAULT_STYLE)
    plan_style = plan.get("style", {})
    if isinstance(plan_style, dict):
        style.update({k: v for k, v in plan_style.items() if v is not None})
    return style


def load_font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        font_path = Path(candidate)
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def normalize_rect(raw: Any) -> tuple[int, int, int, int]:
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("标注 rect 必须是 [x1, y1, x2, y2]。")
    values = [int(round(float(v))) for v in raw]
    x1, y1, x2, y2 = values
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def normalize_center(raw: Any) -> tuple[int, int]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("编号圆心必须是 [x, y]。")
    return int(round(float(raw[0]))), int(round(float(raw[1])))


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
) -> dict[str, Any]:
    cx, cy = center
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = right - left
    height = bottom - top
    x = cx - width / 2 - left
    y = cy - height / 2 - top
    draw.text((x, y), text, fill=fill, font=font)
    return {
        "text": text,
        "center": [cx, cy],
        "text_position": [round(x, 2), round(y, 2)],
        "text_bbox": [left, top, right, bottom],
    }


def clamp_rect(
    rect: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = rect
    return max(0, x1), max(0, y1), min(width, x2), min(height, y2)


def expanded_bounds(
    rect: tuple[int, int, int, int],
    center: tuple[int, int],
    badge_radius: int,
    padding: int,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = rect
    cx, cy = center
    bounds = (
        min(x1, cx - badge_radius) - padding,
        min(y1, cy - badge_radius) - padding,
        max(x2, cx + badge_radius) + padding,
        max(y2, cy + badge_radius) + padding,
    )
    return clamp_rect(bounds, width, height)


def save_image(image: Image.Image, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    if suffix in {".jpg", ".jpeg"} and image.mode != "RGB":
        image = image.convert("RGB")
    image.save(output)


def create_contact_sheet(
    items: list[dict[str, Any]],
    qa_dir: Path,
    output_path: Path,
) -> Path | None:
    crop_items = [item for item in items if item.get("qa_crop")]
    if not crop_items:
        return None

    label_height = 34
    cell_width = 640
    cell_height = 280
    columns = 1 if len(crop_items) <= 2 else 2
    rows = (len(crop_items) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (columns * cell_width, rows * (cell_height + label_height)),
        "#f6f7f9",
    )
    draw = ImageDraw.Draw(sheet)
    label_font = load_font(20)

    for index, item in enumerate(crop_items):
        crop_path = Path(str(item["qa_crop"]))
        with Image.open(crop_path) as opened:
            crop = opened.convert("RGB")
        crop.thumbnail((cell_width - 24, cell_height - 24), Image.Resampling.LANCZOS)

        column = index % columns
        row = index // columns
        x0 = column * cell_width
        y0 = row * (cell_height + label_height)

        label = (
            f"标注 {item['id']}  "
            f"rect={item['rect']}  badge={item['badge_center']}"
        )
        draw.rectangle((x0, y0, x0 + cell_width - 1, y0 + label_height - 1), fill="#111111")
        draw.text((x0 + 10, y0 + 6), label, fill="#ffffff", font=label_font)

        paste_x = x0 + (cell_width - crop.width) // 2
        paste_y = y0 + label_height + (cell_height - crop.height) // 2
        sheet.paste(crop, (paste_x, paste_y))
        draw.rectangle(
            (x0 + 4, y0 + label_height + 4, x0 + cell_width - 5, y0 + label_height + cell_height - 5),
            outline="#d9dde3",
            width=2,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)
    return output_path


def build_review(summary: dict[str, Any], contact_sheet: Path | None) -> dict[str, Any]:
    items = summary["annotations"]
    deterministic_errors: list[str] = []
    for item in items:
        if not item["rect_inside_image"]:
            deterministic_errors.append(f"标注 {item['id']} 的 rect 超出图片范围。")
        if not item["badge_inside_image"]:
            deterministic_errors.append(f"标注 {item['id']} 的 badge 超出图片范围。")
        if not item.get("qa_crop"):
            deterministic_errors.append(f"标注 {item['id']} 缺少 QA 裁切图。")

    return {
        "status": "needs_manual_review" if not deterministic_errors else "deterministic_failed",
        "deterministic_passed": not deterministic_errors,
        "deterministic_errors": deterministic_errors,
        "manual_review_required": True,
        "contact_sheet": str(contact_sheet) if contact_sheet else None,
        "manual_checks": [
            {
                "id": item["id"],
                "target_alignment": "unchecked",
                "coordinate_basis": "unchecked",
                "visual_region_fit": "unchecked",
                "badge_centering": "unchecked",
                "readability": "unchecked",
                "non_obstruction": "unchecked",
                "spacing_and_crossing": "unchecked",
                "data_richness": "not_applicable",
                "decision": "unchecked",
                "notes": "",
            }
            for item in items
        ],
        "blocking_rules": [
            "红框框错区域或明显偏移。",
            "坐标基准错误，因缩放、裁切、Retina 或窗口尺寸变化导致标注整体错位。",
            "红框没有框住完整视觉区域，或框选过紧、过大、切断组件边界。",
            "编号圆内数字未视觉居中。",
            "标注遮挡用户必须阅读或点击的内容。",
            "多个标注交叉、贴线、共线过密或视觉上难以分辨。",
            "数据型页面的数据过少，无法展示趋势、分类、状态差异、Top 项或异常/空态。",
            "红色线条或白色数字不可读。",
            "没有打开完整标注图和 qa-contact-sheet.png 做人工复核。",
        ],
        "repair_policy": "优先修最小失败范围：只改失败标注坐标；数据不足就补对应页面演示数据并重截；截图截错才重截；目标流程变化才重做计划。",
    }


def render(input_path: Path, plan_path: Path, output_path: Path, qa_dir: Path | None) -> None:
    input_path = input_path.expanduser().resolve()
    plan_path = plan_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path == input_path:
        raise ValueError("输出路径不能与原始截图相同，拒绝覆盖源图。")
    if output_path.exists():
        raise FileExistsError(f"输出文件已存在，拒绝覆盖：{output_path}")
    if qa_dir is not None:
        qa_dir = qa_dir.expanduser().resolve()
        if qa_dir.exists() and any(qa_dir.iterdir()):
            raise FileExistsError(f"QA 目录不是空目录，拒绝覆盖既有产物：{qa_dir}")

    plan = load_plan(plan_path)
    style = style_from_plan(plan)

    with Image.open(input_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")

    draw = ImageDraw.Draw(image)
    width, height = image.size
    line_width = int(style["line_width"])
    radius = int(style["radius"])
    badge_radius = int(style["badge_radius"])
    font = load_font(int(style["badge_font_size"]))
    qa_items: list[dict[str, Any]] = []

    if qa_dir:
        qa_dir.mkdir(parents=True, exist_ok=True)

    for index, annotation in enumerate(plan["annotations"], start=1):
        if not isinstance(annotation, dict):
            raise ValueError(f"第 {index} 个标注必须是 object。")
        annotation_id = str(annotation.get("id", index))
        rect = normalize_rect(annotation.get("rect"))
        badge = annotation.get("badge", {})
        if not isinstance(badge, dict):
            raise ValueError(f"标注 {annotation_id} 的 badge 必须是 object。")
        text = str(badge.get("text", annotation_id))
        center = normalize_center(badge.get("center"))
        item_badge_radius = int(badge.get("radius", badge_radius))

        draw.rounded_rectangle(
            rect,
            radius=radius,
            outline=str(style["color"]),
            width=line_width,
        )
        cx, cy = center
        draw.ellipse(
            (
                cx - item_badge_radius,
                cy - item_badge_radius,
                cx + item_badge_radius,
                cy + item_badge_radius,
            ),
            fill=str(style["color"]),
        )
        text_info = draw_centered_text(
            draw,
            center,
            text,
            font,
            fill=str(style["text_color"]),
        )

        crop_path = None
        if qa_dir:
            crop_rect = expanded_bounds(
                rect,
                center,
                item_badge_radius,
                int(style["crop_padding"]),
                width,
                height,
            )
            crop_path = qa_dir / f"qa-{annotation_id}.png"
            image.crop(crop_rect).save(crop_path)
        else:
            crop_rect = None

        qa_items.append(
            {
                "id": annotation_id,
                "rect": list(rect),
                "badge_center": [cx, cy],
                "badge_radius": item_badge_radius,
                "text": text_info,
                "qa_crop": str(crop_path) if crop_path else None,
                "qa_crop_bounds": list(crop_rect) if crop_rect else None,
                "rect_inside_image": rect == clamp_rect(rect, width, height),
                "badge_inside_image": (
                    0 <= cx - item_badge_radius
                    and cx + item_badge_radius <= width
                    and 0 <= cy - item_badge_radius
                    and cy + item_badge_radius <= height
                ),
            }
        )

    save_image(image, output_path)

    summary = {
        "input": str(input_path),
        "plan": str(plan_path),
        "output": str(output_path),
        "image_size": [width, height],
        "annotations": qa_items,
    }
    if qa_dir:
        contact_sheet_path = create_contact_sheet(
            qa_items,
            qa_dir,
            qa_dir / "qa-contact-sheet.png",
        )
        summary["qa_contact_sheet"] = str(contact_sheet_path) if contact_sheet_path else None
        review = build_review(summary, contact_sheet_path)
        review_path = qa_dir / "review.json"
        review_path.write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary["review"] = str(review_path)
        summary_path = qa_dir / "qa-summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    if not args.input.exists():
        raise SystemExit(f"找不到原始截图：{args.input}")
    if not args.plan.exists():
        raise SystemExit(f"找不到标注计划：{args.plan}")
    try:
        render(args.input, args.plan, args.output, args.qa_dir)
    except (ValueError, FileExistsError, OSError) as exc:
        raise SystemExit(f"单图标注失败：{exc}") from exc


if __name__ == "__main__":
    main()
