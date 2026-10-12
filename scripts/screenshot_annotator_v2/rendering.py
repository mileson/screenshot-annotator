"""Pillow 渲染、自动锚点、总览图和坐标网格。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from .common import (
    ANCHORS,
    Rect,
    clamp_label_rect,
    load_font,
    rect_area,
    rect_distance,
    rect_inside,
    rect_intersection_area,
    rect_to_list,
    save_clean_image,
    sha256_file,
)


AUTO_ANCHOR_ORDER = [
    "bottom_right",
    "inside_top_right",
    "top_right",
    "bottom_left",
    "top_left",
    "inside_bottom_right",
    "inside_top_left",
]


def lanczos_resample_filter() -> Any:
    """Return Pillow's LANCZOS filter across enum and legacy-constant versions."""

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return getattr(resampling, "LANCZOS")
    return getattr(Image, "LANCZOS")


def scaled_style(style: dict[str, Any], image_width: int) -> dict[str, Any]:
    result = dict(style)
    reference = max(1, int(style.get("reference_width", 1179)))
    scale = image_width / reference if bool(style.get("scale_with_image", True)) else 1.0
    for key in (
        "line_width",
        "rect_radius",
        "callout_border_width",
        "callout_radius",
        "callout_padding_x",
        "callout_padding_y",
        "anchor_gap",
        "marker_radius",
        "marker_font_size",
        "max_label_width",
        "max_manual_label_distance",
    ):
        result[key] = max(1, int(round(float(style[key]) * scale)))
    base_font = int(round(float(style["callout_font_size"]) * scale))
    minimum_font = int(style.get("callout_min_font_size", style["callout_font_size"]))
    result["callout_font_size"] = max(minimum_font, base_font)
    result["image_scale"] = round(scale, 6)
    return result


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    maximum_width: int,
) -> list[str]:
    maximum_width = max(24, maximum_width)
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        bounds = draw.textbbox((0, 0), candidate, font=font)
        if current and bounds[2] - bounds[0] > maximum_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current or not lines:
        lines.append(current)
    return lines


def measure_callout(
    image_width: int,
    text: str,
    marker: str | None,
    style: dict[str, Any],
) -> dict[str, Any]:
    probe = Image.new("RGB", (8, 8), "white")
    draw = ImageDraw.Draw(probe)
    font = load_font(int(style["callout_font_size"]))
    marker_font = load_font(int(style["marker_font_size"]))
    padding_x = int(style["callout_padding_x"])
    padding_y = int(style["callout_padding_y"])
    marker_diameter = int(style["marker_radius"]) * 2 if marker else 0
    marker_gap = max(6, padding_x // 2) if marker else 0
    maximum_width = min(
        int(style["max_label_width"]),
        max(80, image_width - 8),
    )
    text_width_budget = max(
        24,
        maximum_width - 2 * padding_x - marker_diameter - marker_gap,
    )
    lines = wrap_text(draw, text, font, text_width_budget)
    spacing = max(4, int(style["callout_font_size"]) // 6)
    line_bounds = [draw.textbbox((0, 0), line or " ", font=font) for line in lines]
    text_width = max(bounds[2] - bounds[0] for bounds in line_bounds)
    line_heights = [bounds[3] - bounds[1] for bounds in line_bounds]
    text_height = sum(line_heights) + spacing * max(0, len(lines) - 1)
    content_height = max(text_height, marker_diameter)
    width = padding_x * 2 + marker_diameter + marker_gap + text_width
    height = padding_y * 2 + content_height
    marker_bounds = draw.textbbox((0, 0), marker or "", font=marker_font)
    return {
        "width": max(1, int(math.ceil(width))),
        "height": max(1, int(math.ceil(height))),
        "lines": lines,
        "line_heights": line_heights,
        "text_width": text_width,
        "text_height": text_height,
        "spacing": spacing,
        "marker_diameter": marker_diameter,
        "marker_gap": marker_gap,
        "marker_text_bbox": list(marker_bounds),
        "font_size": int(style["callout_font_size"]),
        "marker_font_size": int(style["marker_font_size"]),
    }


def anchor_rect(
    anchor: str,
    target: Rect,
    label_width: int,
    label_height: int,
    gap: int,
) -> Rect:
    x1, y1, x2, y2 = target
    if anchor == "bottom_right":
        left, top = x2 + gap, y2 + gap
    elif anchor == "inside_top_right":
        left, top = x2 - gap - label_width, y1 + gap
    elif anchor == "top_right":
        left, top = x2 + gap, y1 - gap - label_height
    elif anchor == "bottom_left":
        left, top = x1 - gap - label_width, y2 + gap
    elif anchor == "top_left":
        left, top = x1 - gap - label_width, y1 - gap - label_height
    elif anchor == "inside_bottom_right":
        left, top = x2 - gap - label_width, y2 - gap - label_height
    elif anchor == "inside_top_left":
        left, top = x1 + gap, y1 + gap
    else:
        raise ValueError(f"不支持的自动锚点：{anchor}")
    return left, top, left + label_width, top + label_height


def score_candidate(
    desired: Rect,
    actual: Rect,
    image_size: tuple[int, int],
    target: Rect,
    all_targets: list[Rect],
    protected: list[dict[str, Any]],
    placed_labels: list[dict[str, Any]],
    rank: int,
) -> dict[str, Any]:
    width, height = image_size
    canvas = (0, 0, width, height)
    outside_area = max(0, rect_area(desired) - rect_intersection_area(desired, canvas))
    protected_area = sum(
        rect_intersection_area(actual, item["rect"]) for item in protected
    )
    label_collision_area = sum(
        rect_intersection_area(actual, item["rect"]) for item in placed_labels
    )
    other_target_area = sum(
        rect_intersection_area(actual, other)
        for other in all_targets
        if other != target
    )
    own_target_area = rect_intersection_area(actual, target)
    moved = sum(abs(desired[index] - actual[index]) for index in range(4))
    cost = (
        outside_area * 100_000
        + protected_area * 50_000
        + label_collision_area * 75_000
        + other_target_area * 25
        + own_target_area * 2
        + moved * 50
        + rank
    )
    return {
        "score": int(cost),
        "outside_area": int(outside_area),
        "protected_overlap_area": int(protected_area),
        "label_overlap_area": int(label_collision_area),
        "other_target_overlap_area": int(other_target_area),
        "own_target_overlap_area": int(own_target_area),
        "moved_pixels": int(moved),
        "hard_conflict": bool(
            outside_area
            or protected_area
            or label_collision_area
            or other_target_area
        ),
    }


def resolve_placements(
    annotations: list[dict[str, Any]],
    protected: list[dict[str, Any]],
    image_size: tuple[int, int],
    style: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    width, height = image_size
    all_targets = [item["rect"] for item in annotations]
    placed_labels: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for annotation in annotations:
        callout = annotation["callout"]
        measurement = measure_callout(
            width,
            callout["text"],
            callout["resolved_marker"],
            style,
        )
        natural_width = measurement["width"]
        natural_height = measurement["height"]
        requested_anchor = callout["anchor"]
        candidate_records: list[dict[str, Any]] = []
        if requested_anchor == "manual":
            actual = callout["label_rect"]
            desired = actual
            score = score_candidate(
                desired,
                actual,
                image_size,
                annotation["rect"],
                all_targets,
                protected,
                placed_labels,
                0,
            )
            candidate_records.append(
                {
                    "anchor": "manual",
                    "desired_rect": rect_to_list(desired),
                    "resolved_rect": rect_to_list(actual),
                    **score,
                }
            )
            selected = candidate_records[0]
        else:
            choices = (
                AUTO_ANCHOR_ORDER if requested_anchor == "auto" else [requested_anchor]
            )
            for rank, candidate_anchor in enumerate(choices):
                desired = anchor_rect(
                    candidate_anchor,
                    annotation["rect"],
                    natural_width,
                    natural_height,
                    int(style["anchor_gap"]),
                )
                actual = clamp_label_rect(desired, width, height)
                score = score_candidate(
                    desired,
                    actual,
                    image_size,
                    annotation["rect"],
                    all_targets,
                    protected,
                    placed_labels,
                    rank,
                )
                candidate_records.append(
                    {
                        "anchor": candidate_anchor,
                        "desired_rect": rect_to_list(desired),
                        "resolved_rect": rect_to_list(actual),
                        **score,
                    }
                )
            selected = min(candidate_records, key=lambda item: (item["score"], item["anchor"]))
        selected_rect = tuple(selected["resolved_rect"])
        target_distance = rect_distance(selected_rect, annotation["rect"])
        if requested_anchor == "auto" and selected["hard_conflict"]:
            blockers.append(
                {
                    "code": "no_safe_anchor",
                    "asset_id": annotation["asset_id"],
                    "annotation_id": annotation["id"],
                    "message": (
                        f"素材 {annotation['asset_id']} 的标注 {annotation['id']} "
                        "没有无碰撞锚点，已渲染最小代价位置，必须人工修正。"
                    ),
                }
            )
        if (
            requested_anchor == "manual"
            and target_distance > int(style["max_manual_label_distance"])
        ):
            blockers.append(
                {
                    "code": "manual_label_detached",
                    "asset_id": annotation["asset_id"],
                    "annotation_id": annotation["id"],
                    "target_distance": target_distance,
                    "maximum_distance": int(style["max_manual_label_distance"]),
                    "message": (
                        f"素材 {annotation['asset_id']} 的标注 {annotation['id']} "
                        "手动标签离目标框过远，无法确认归属。"
                    ),
                }
            )
        if not rect_inside(selected_rect, width, height):
            blockers.append(
                {
                    "code": "label_out_of_bounds",
                    "asset_id": annotation["asset_id"],
                    "annotation_id": annotation["id"],
                    "message": (
                        f"素材 {annotation['asset_id']} 的标注 {annotation['id']} "
                        "动作标签超出图片边界。"
                    ),
                }
            )
        label_width = selected_rect[2] - selected_rect[0]
        label_height = selected_rect[3] - selected_rect[1]
        content_fits = label_width >= natural_width and label_height >= natural_height
        if not content_fits:
            blockers.append(
                {
                    "code": "callout_content_does_not_fit",
                    "asset_id": annotation["asset_id"],
                    "annotation_id": annotation["id"],
                    "message": (
                        f"素材 {annotation['asset_id']} 的标注 {annotation['id']} "
                        "动作标签区域不足以容纳文字。"
                    ),
                }
            )
        placed = {
            "annotation_id": annotation["id"],
            "rect": selected_rect,
        }
        placed_labels.append(placed)
        resolved.append(
            {
                **annotation,
                "callout": {
                    **callout,
                    "resolved_anchor": selected["anchor"],
                    "label_rect": rect_to_list(selected_rect),
                },
                "placement": {
                    "natural_size": [natural_width, natural_height],
                    "content_fits": content_fits,
                    "target_distance": target_distance,
                    "selected_score": selected["score"],
                    "candidates": candidate_records,
                },
                "_measurement": measurement,
            }
        )

    for index, first in enumerate(resolved):
        first_rect = tuple(first["callout"]["label_rect"])
        for second in resolved[index + 1 :]:
            second_rect = tuple(second["callout"]["label_rect"])
            overlap = rect_intersection_area(first_rect, second_rect)
            if overlap:
                blockers.append(
                    {
                        "code": "label_collision",
                        "asset_id": first["asset_id"],
                        "annotation_ids": [first["id"], second["id"]],
                        "overlap_area": overlap,
                        "message": (
                            f"素材 {first['asset_id']} 的标签 {first['id']} 与 "
                            f"{second['id']} 相撞。"
                        ),
                    }
                )
    for annotation in resolved:
        label_rect = tuple(annotation["callout"]["label_rect"])
        for other in resolved:
            if other["id"] == annotation["id"]:
                continue
            overlap = rect_intersection_area(label_rect, tuple(other["rect"]))
            if overlap:
                blockers.append(
                    {
                        "code": "unrelated_target_collision",
                        "asset_id": annotation["asset_id"],
                        "annotation_id": annotation["id"],
                        "other_annotation_id": other["id"],
                        "overlap_area": overlap,
                        "message": (
                            f"素材 {annotation['asset_id']} 的标签 {annotation['id']} "
                            f"遮挡了无关目标框 {other['id']}。"
                        ),
                    }
                )
        for region in protected:
            overlap = rect_intersection_area(label_rect, region["rect"])
            if overlap:
                blockers.append(
                    {
                        "code": "protected_region_collision",
                        "asset_id": annotation["asset_id"],
                        "annotation_id": annotation["id"],
                        "protected_region_id": region["id"],
                        "overlap_area": overlap,
                        "message": (
                            f"素材 {annotation['asset_id']} 的标签 {annotation['id']} "
                            f"遮挡保护区域 {region['id']}。"
                        ),
                    }
                )
    return resolved, blockers, warnings


def draw_centered(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    text: str,
    font: Any,
    fill: str,
) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    x = center[0] - (bounds[2] - bounds[0]) / 2 - bounds[0]
    y = center[1] - (bounds[3] - bounds[1]) / 2 - bounds[1]
    draw.text((x, y), text, font=font, fill=fill)


def draw_callout(
    draw: ImageDraw.ImageDraw,
    annotation: dict[str, Any],
    style: dict[str, Any],
) -> None:
    rect = tuple(annotation["callout"]["label_rect"])
    draw.rounded_rectangle(
        rect,
        radius=int(style["callout_radius"]),
        fill=str(style["callout_fill"]),
        outline=str(style["color"]),
        width=int(style["callout_border_width"]),
    )
    x1, y1, x2, y2 = rect
    padding_x = int(style["callout_padding_x"])
    marker = annotation["callout"]["resolved_marker"]
    marker_diameter = int(style["marker_radius"]) * 2 if marker else 0
    marker_gap = max(6, padding_x // 2) if marker else 0
    if marker:
        radius = min(
            int(style["marker_radius"]),
            max(3, (y2 - y1 - 8) // 2),
        )
        center = (x1 + padding_x + radius, (y1 + y2) / 2)
        draw.ellipse(
            (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            ),
            fill=str(style["marker_fill"]),
        )
        draw_centered(
            draw,
            center,
            marker,
            load_font(int(style["marker_font_size"])),
            str(style["marker_text_color"]),
        )
    measurement = annotation["_measurement"]
    font = load_font(int(style["callout_font_size"]))
    text_x = x1 + padding_x + marker_diameter + marker_gap
    total_text_height = measurement["text_height"]
    current_y = y1 + ((y2 - y1) - total_text_height) / 2
    for line, line_height in zip(
        measurement["lines"], measurement["line_heights"]
    ):
        bounds = draw.textbbox((0, 0), line, font=font)
        draw.text(
            (text_x - bounds[0], current_y - bounds[1]),
            line,
            font=font,
            fill=str(style["text_color"]),
        )
        current_y += line_height + measurement["spacing"]


def render_annotated_asset(
    source_path: Path,
    output_path: Path,
    annotations: list[dict[str, Any]],
    protected: list[dict[str, Any]],
    style: dict[str, Any],
) -> dict[str, Any]:
    with Image.open(source_path) as source:
        frame_count = int(getattr(source, "n_frames", 1))
        source_format = str(source.format or "").upper()
        if source_format not in {"PNG", "JPEG", "WEBP"}:
            return {
                "render_status": "unsupported",
                "unsupported_reason": (
                    f"文件格式 {source_format or '未知'} 不在 PNG/JPEG/WebP 范围内。"
                ),
                "detected_format": source_format or None,
                "frame_count": frame_count,
            }
        if frame_count != 1 or bool(getattr(source, "is_animated", False)):
            return {
                "render_status": "unsupported",
                "unsupported_reason": "仅支持单帧栅格图片；该素材包含多帧。",
                "detected_format": source_format,
                "frame_count": frame_count,
            }
        transposed = ImageOps.exif_transpose(source)
        image = transposed.convert("RGBA")
    width, height = image.size
    actual_style = scaled_style(style, width)
    resolved, blockers, warnings = resolve_placements(
        annotations,
        protected,
        (width, height),
        actual_style,
    )
    draw = ImageDraw.Draw(image)
    for annotation in resolved:
        draw.rounded_rectangle(
            annotation["rect"],
            radius=int(actual_style["rect_radius"]),
            outline=str(actual_style["color"]),
            width=int(actual_style["line_width"]),
        )
    for annotation in resolved:
        draw_callout(draw, annotation, actual_style)
    save_clean_image(image, output_path)
    serializable_annotations = []
    for annotation in resolved:
        cleaned = {key: value for key, value in annotation.items() if not key.startswith("_")}
        cleaned["rect"] = rect_to_list(cleaned["rect"])
        serializable_annotations.append(cleaned)
    return {
        "render_status": "rendered",
        "detected_format": source_format,
        "frame_count": 1,
        "image_size": [width, height],
        "style": actual_style,
        "annotations": serializable_annotations,
        "blockers": blockers,
        "warnings": warnings,
    }


def build_annotation_crops(
    annotated_path: Path,
    asset_id: str,
    annotations: list[dict[str, Any]],
    output_dir: Path,
    padding: int = 90,
) -> list[dict[str, Any]]:
    """为每个标注输出同时包含目标框与标签的无元数据 QA 裁切图。"""

    if padding < 0:
        raise ValueError("QA crop padding 不能小于 0。")
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with Image.open(annotated_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGBA")
    width, height = image.size
    for annotation in annotations:
        target = tuple(annotation["rect"])
        label = tuple(annotation["callout"]["label_rect"])
        crop_rect = (
            max(0, min(target[0], label[0]) - padding),
            max(0, min(target[1], label[1]) - padding),
            min(width, max(target[2], label[2]) + padding),
            min(height, max(target[3], label[3]) + padding),
        )
        output = output_dir / f"{annotation['id']}.png"
        crop = image.crop(crop_rect)
        save_clean_image(crop, output)
        records.append(
            {
                "asset_id": asset_id,
                "annotation_id": annotation["id"],
                "name": output.name,
                "crop_rect": rect_to_list(crop_rect),
                "size": [crop.width, crop.height],
                "sha256": sha256_file(output),
            }
        )
    return records


def _thumbnail(image: Image.Image, maximum_size: tuple[int, int]) -> Image.Image:
    converted = image.convert("RGB")
    converted.thumbnail(maximum_size, lanczos_resample_filter())
    return converted


def build_overview(
    items: list[dict[str, Any]],
    output_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    requested_mode = str(config.get("mode", "auto"))
    resolved_mode = (
        ("vertical" if len(items) <= 3 else "grid")
        if requested_mode == "auto"
        else requested_mode
    )
    maximum_pixels = int(config.get("max_pixels", 40_000_000))
    maximum_height = int(config.get("max_page_height", 12_000))
    background = str(config.get("background", "#F5F5F4"))
    minimum_label_font_px = float(config.get("min_label_font_px", 14))
    title_font = load_font(int(config.get("title_font_size", 28)))
    pages: list[dict[str, Any]] = []
    readability_issues: list[dict[str, Any]] = []

    if not items:
        canvas = Image.new("RGB", (1200, 220), background)
        draw = ImageDraw.Draw(canvas)
        draw.text((40, 80), "暂无可渲染的单帧 PNG/JPEG/WebP 素材", fill="#444444", font=title_font)
        output = output_dir / "overview.png"
        save_clean_image(canvas, output)
        return {
            "requested_mode": requested_mode,
            "resolved_mode": resolved_mode,
            "columns": None,
            "max_pixels": maximum_pixels,
            "max_page_height": maximum_height,
            "min_label_font_px": minimum_label_font_px,
            "readability_issues": [],
            "pages": [
                {
                    "path": output.name,
                    "size": list(canvas.size),
                    "pixel_count": canvas.width * canvas.height,
                    "placements": [],
                }
            ],
        }

    if resolved_mode == "vertical":
        maximum_width = int(config.get("max_width", 1600))
        rows: list[dict[str, Any]] = []
        page_width = 1
        for item in items:
            with Image.open(item["path"]) as opened:
                ratio = min(1.0, maximum_width / opened.width)
                shown_size = (
                    max(1, int(round(opened.width * ratio))),
                    max(1, int(round(opened.height * ratio))),
                )
            row_height = 52 + shown_size[1] + 24
            page_width = max(page_width, shown_size[0] + 48)
            rows.append({**item, "shown_size": shown_size, "row_height": row_height})
        page_width = min(maximum_width + 48, page_width)
        maximum_row_height = max(
            1,
            min(maximum_height, maximum_pixels // max(1, page_width)),
        )
        for row in rows:
            if row["row_height"] <= maximum_row_height:
                continue
            allowed_image_height = max(1, maximum_row_height - 76)
            scale = min(1.0, allowed_image_height / row["shown_size"][1])
            row["shown_size"] = (
                max(1, int(round(row["shown_size"][0] * scale))),
                max(1, int(round(row["shown_size"][1] * scale))),
            )
            row["row_height"] = 52 + row["shown_size"][1] + 24
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_height = 0
        for row in rows:
            prospective = current_height + row["row_height"]
            exceeds = prospective > maximum_height or prospective * page_width > maximum_pixels
            if current and exceeds:
                groups.append(current)
                current = []
                current_height = 0
            current.append(row)
            current_height += row["row_height"]
        if current:
            groups.append(current)
        for page_index, group in enumerate(groups, start=1):
            page_height = max(1, sum(row["row_height"] for row in group))
            canvas = Image.new("RGB", (page_width, page_height), background)
            draw = ImageDraw.Draw(canvas)
            y = 0
            placements = []
            for row in group:
                draw.text((24, y + 10), row["title"], fill="#262626", font=title_font)
                with Image.open(row["path"]) as opened:
                    thumb = _thumbnail(opened, row["shown_size"])
                image_x = (page_width - thumb.width) // 2
                image_y = y + 52
                canvas.paste(thumb, (image_x, image_y))
                placements.append(
                    {
                        "asset_id": row["asset_id"],
                        "rect": [image_x, image_y, image_x + thumb.width, image_y + thumb.height],
                        "source_size": row["source_size"],
                        "rendered_size": [thumb.width, thumb.height],
                        "scale": round(
                            min(
                                thumb.width / max(1, row["source_size"][0]),
                                thumb.height / max(1, row["source_size"][1]),
                            ),
                            6,
                        ),
                    }
                )
                placement = placements[-1]
                effective_font = float(row.get("label_font_size", 24)) * float(
                    placement["scale"]
                )
                placement["source_label_font_px"] = int(
                    row.get("label_font_size", 24)
                )
                placement["effective_label_font_px"] = round(effective_font, 3)
                if effective_font < minimum_label_font_px:
                    readability_issues.append(
                        {
                            "code": "overview_label_unreadable",
                            "asset_id": row["asset_id"],
                            "page": page_index,
                            "effective_label_font_px": round(effective_font, 3),
                            "minimum_label_font_px": minimum_label_font_px,
                        }
                    )
                y += row["row_height"]
            name = "overview.png" if len(groups) == 1 else f"overview-{page_index:03d}.png"
            output = output_dir / name
            save_clean_image(canvas, output)
            pages.append(
                {
                    "path": name,
                    "size": [canvas.width, canvas.height],
                    "pixel_count": canvas.width * canvas.height,
                    "placements": placements,
                }
            )
        columns: int | None = 1
    else:
        columns = int(config.get("columns") or min(7, max(4, math.ceil(math.sqrt(len(items))))))
        cell_width = int(config.get("cell_width", 480))
        # 竖屏手机截图在 4–7 列网格中需要足够的纵向空间，否则标签虽未
        # 被裁切，却会缩成不可读小字。默认按教程阅读图而非普通缩略图设置。
        maximum_image_height = int(config.get("cell_image_height", 1000))
        title_height = 52
        row_gap = 20
        page_width = columns * cell_width
        maximum_row_height = max(
            1,
            min(maximum_height, maximum_pixels // max(1, page_width)),
        )
        prepared_items: list[dict[str, Any]] = []
        for item in items:
            source_width, source_height = item["source_size"]
            ratio = min(
                1.0,
                max(1, cell_width - 28) / max(1, source_width),
                max(1, maximum_image_height - 16) / max(1, source_height),
            )
            shown_size = (
                max(1, int(round(source_width * ratio))),
                max(1, int(round(source_height * ratio))),
            )
            prepared_items.append({**item, "shown_size": shown_size})

        rows: list[dict[str, Any]] = []
        for index in range(0, len(prepared_items), columns):
            row_items = prepared_items[index : index + columns]
            row_image_height = max(item["shown_size"][1] for item in row_items)
            row_height = title_height + row_image_height + row_gap
            if row_height > maximum_row_height:
                allowed_image_height = max(
                    1,
                    maximum_row_height - title_height - row_gap,
                )
                scale = min(1.0, allowed_image_height / max(1, row_image_height))
                for item in row_items:
                    width, height = item["shown_size"]
                    item["shown_size"] = (
                        max(1, int(round(width * scale))),
                        max(1, int(round(height * scale))),
                    )
                row_image_height = max(item["shown_size"][1] for item in row_items)
                row_height = title_height + row_image_height + row_gap
            rows.append(
                {
                    "items": row_items,
                    "image_height": row_image_height,
                    "row_height": row_height,
                }
            )

        groups: list[list[dict[str, Any]]] = []
        current_rows: list[dict[str, Any]] = []
        current_height = 0
        for row in rows:
            prospective = current_height + row["row_height"]
            exceeds = (
                prospective > maximum_height
                or prospective * page_width > maximum_pixels
            )
            if current_rows and exceeds:
                groups.append(current_rows)
                current_rows = []
                current_height = 0
            current_rows.append(row)
            current_height += row["row_height"]
        if current_rows:
            groups.append(current_rows)

        for page_index, group in enumerate(groups, start=1):
            page_height = max(1, sum(row["row_height"] for row in group))
            canvas = Image.new("RGB", (page_width, page_height), background)
            draw = ImageDraw.Draw(canvas)
            placements = []
            y = 0
            for row in group:
                for column, item in enumerate(row["items"]):
                    cell_x = column * cell_width
                    draw.text(
                        (cell_x + 14, y + 10),
                        item["title"],
                        fill="#262626",
                        font=title_font,
                    )
                    with Image.open(item["path"]) as opened:
                        thumb = _thumbnail(opened, item["shown_size"])
                    image_x = cell_x + (cell_width - thumb.width) // 2
                    image_y = (
                        y
                        + title_height
                        + (row["image_height"] - thumb.height) // 2
                    )
                    canvas.paste(thumb, (image_x, image_y))
                    placements.append(
                        {
                            "asset_id": item["asset_id"],
                            "rect": [
                                image_x,
                                image_y,
                                image_x + thumb.width,
                                image_y + thumb.height,
                            ],
                            "source_size": item["source_size"],
                            "rendered_size": [thumb.width, thumb.height],
                            "scale": round(
                                min(
                                    thumb.width / max(1, item["source_size"][0]),
                                    thumb.height / max(1, item["source_size"][1]),
                                ),
                                6,
                            ),
                        }
                    )
                    placement = placements[-1]
                    effective_font = float(item.get("label_font_size", 24)) * float(
                        placement["scale"]
                    )
                    placement["source_label_font_px"] = int(
                        item.get("label_font_size", 24)
                    )
                    placement["effective_label_font_px"] = round(effective_font, 3)
                    if effective_font < minimum_label_font_px:
                        readability_issues.append(
                            {
                                "code": "overview_label_unreadable",
                                "asset_id": item["asset_id"],
                                "page": page_index,
                                "effective_label_font_px": round(effective_font, 3),
                                "minimum_label_font_px": minimum_label_font_px,
                            }
                        )
                y += row["row_height"]
            name = "overview.png" if len(groups) == 1 else f"overview-{page_index:03d}.png"
            output = output_dir / name
            save_clean_image(canvas, output)
            pages.append(
                {
                    "path": name,
                    "size": [canvas.width, canvas.height],
                    "pixel_count": canvas.width * canvas.height,
                    "placements": placements,
                }
            )
    return {
        "requested_mode": requested_mode,
        "resolved_mode": resolved_mode,
        "columns": columns,
        "max_pixels": maximum_pixels,
        "max_page_height": maximum_height,
        "min_label_font_px": minimum_label_font_px,
        "readability_issues": readability_issues,
        "pages": pages,
    }


def build_contact_sheet(
    items: list[dict[str, Any]],
    output: Path,
    maximum_pixels: int = 20_000_000,
) -> dict[str, Any]:
    columns = 4
    cell_width = 400
    image_height = 260
    title_height = 44
    if not items:
        canvas = Image.new("RGB", (1200, 180), "#F5F5F4")
        ImageDraw.Draw(canvas).text(
            (32, 68),
            "暂无可渲染素材",
            fill="#444444",
            font=load_font(28),
        )
        save_clean_image(canvas, output)
        return {
            "path": output.name,
            "size": list(canvas.size),
            "pixel_count": canvas.width * canvas.height,
            "placements": [],
        }
    rows = math.ceil(len(items) / columns)
    page_width = columns * cell_width
    page_height = rows * (title_height + image_height)
    if page_width * page_height > maximum_pixels:
        scale = math.sqrt(maximum_pixels / (page_width * page_height))
        cell_width = max(120, int(cell_width * scale))
        image_height = max(90, int(image_height * scale))
        title_height = max(28, int(title_height * scale))
        page_width = columns * cell_width
        page_height = rows * (title_height + image_height)
    canvas = Image.new("RGB", (page_width, page_height), "#F5F5F4")
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(max(16, min(26, title_height - 10)))
    placements = []
    for index, item in enumerate(items):
        column = index % columns
        row = index // columns
        x0 = column * cell_width
        y0 = row * (title_height + image_height)
        draw.text((x0 + 10, y0 + 7), item["title"], fill="#222222", font=title_font)
        with Image.open(item["path"]) as opened:
            thumb = _thumbnail(opened, (cell_width - 18, image_height - 18))
        x = x0 + (cell_width - thumb.width) // 2
        y = y0 + title_height + (image_height - thumb.height) // 2
        canvas.paste(thumb, (x, y))
        placements.append(
            {
                "asset_id": item["asset_id"],
                "rect": [x, y, x + thumb.width, y + thumb.height],
            }
        )
    save_clean_image(canvas, output)
    return {
        "path": output.name,
        "size": [canvas.width, canvas.height],
        "pixel_count": canvas.width * canvas.height,
        "placements": placements,
    }


def render_coordinate_grid(
    input_path: Path,
    output_path: Path,
    step: int = 100,
) -> dict[str, Any]:
    if step < 10 or step > 2000:
        raise ValueError("坐标网格 step 必须在 10 到 2000 之间。")
    with Image.open(input_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    font = load_font(max(14, min(28, step // 4)))
    line_width = max(1, min(4, step // 50))
    for x in range(0, width, step):
        draw.line((x, 0, x, height), fill=(249, 115, 22, 180), width=line_width)
        draw.rectangle((x + 2, 2, x + 82, 32), fill=(255, 255, 255, 215))
        draw.text((x + 6, 5), f"x={x}", fill=(194, 65, 12, 255), font=font)
    for y in range(0, height, step):
        draw.line((0, y, width, y), fill=(249, 115, 22, 180), width=line_width)
        label_y = min(max(2, y + 2), max(2, height - 34))
        draw.rectangle((2, label_y, 84, label_y + 30), fill=(255, 255, 255, 215))
        draw.text((6, label_y + 3), f"y={y}", fill=(194, 65, 12, 255), font=font)
    save_clean_image(image, output_path)
    return {"path": str(output_path), "step": step, "size": [width, height]}
