"""从版本目录重新读取并独立校验 screenshot-annotator v2 产物。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageOps, UnidentifiedImageError

from .common import (
    ANCHORS,
    SCHEMA_VERSION,
    STYLE_PRESET,
    V2_STYLE_PRESETS,
    all_pairs,
    image_has_sensitive_metadata,
    lint_callout_text,
    lint_copy_for_kind,
    normalize_rect,
    normalize_semantic_role,
    portable_name_key,
    read_json,
    rect_distance,
    rect_inside,
    rect_intersection_area,
    resolve_marker,
    sha256_file,
)
from .rendering import lanczos_resample_filter, scaled_style


ALLOWED_AUTO_STATUSES = {"blocked", "awaiting_manual_review"}
VERSION_PATTERN = re.compile(r"^v\d{3}$")
REVIEW_FILE_PATTERN = re.compile(r"^review-(\d{3})\.json$")
TARGET_KINDS = {
    "bottom_nav_item",
    "menu_row",
    "option_row",
    "action_button",
    "file_result_row",
    "message_row",
    "composite_step",
    "custom",
}
TARGET_KIND_REQUIRED_ROLES = {
    "bottom_nav_item": {"icon", "label", "container"},
    "menu_row": {"icon", "label"},
    "option_row": {"label"},
    "action_button": {"button"},
    "file_result_row": {"file_row"},
    "message_row": {"message_row"},
}


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message}
    issue.update(details)
    return issue


def _bounded_int(
    value: Any,
    *,
    code: str,
    message: str,
    blockers: list[dict[str, Any]],
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        blockers.append(_issue(code, message))
        return None
    if (
        (minimum is not None and normalized < minimum)
        or (maximum is not None and normalized > maximum)
    ):
        blockers.append(_issue(code, message))
        return None
    return normalized


def _inside_version(version_dir: Path, relative: Any) -> tuple[Path | None, str | None]:
    if not isinstance(relative, str) or not relative.strip():
        return None, "路径必须是非空相对字符串。"
    if "\\" in relative or any(ord(character) < 32 for character in relative):
        return None, f"路径不得包含反斜杠或控制字符：{relative!r}"
    candidate_text = relative
    candidate = Path(candidate_text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None, f"路径必须留在版本目录内：{relative!r}"
    resolved = (version_dir / candidate).resolve()
    try:
        common = os.path.commonpath((str(version_dir.resolve()), str(resolved)))
    except ValueError:
        return None, f"路径无法归入版本目录：{relative!r}"
    if common != str(version_dir.resolve()):
        return None, f"路径越出版本目录：{relative!r}"
    return resolved, None


def _read_optional_json(
    path: Path,
    label: str,
    blockers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        return read_json(path, label)
    except ValueError as exc:
        blockers.append(_issue("json_unreadable", str(exc), path=str(path.name)))
        return None


def _metadata_keys(image: Image.Image) -> list[str]:
    sensitive = {key.lower() for key in image_has_sensitive_metadata(image)}
    for key, value in image.info.items():
        if str(key).lower() in {
            "exif",
            "gps",
            "xmp",
            "xml:com.adobe.xmp",
            "author",
            "comment",
            "description",
            "copyright",
            "icc_profile",
        } and value is not None and value != "" and value != b"":
            sensitive.add(str(key))
    return sorted(sensitive)


def _validate_annotated_pixels(
    source_path: Path,
    annotated_path: Path,
    asset: dict[str, Any],
    base_style: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    asset_id = str(asset.get("id", ""))
    try:
        with Image.open(source_path) as source_opened:
            source = ImageOps.exif_transpose(source_opened).convert("RGBA")
        with Image.open(annotated_path) as annotated_opened:
            annotated = annotated_opened.convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        blockers.append(
            _issue(
                "annotated_pixel_check_unreadable",
                f"素材 {asset_id} 无法执行派生像素门禁：{exc}",
                asset_id=asset_id,
            )
        )
        return
    if source.size != annotated.size:
        return
    try:
        expected_style = scaled_style(base_style, source.width)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        blockers.append(
            _issue(
                "resolved_style_unverifiable",
                f"素材 {asset_id} 无法根据 manifest.style 复算实际样式：{exc}",
                asset_id=asset_id,
            )
        )
        return
    recorded_style = asset.get("style")
    if recorded_style != expected_style:
        blockers.append(
            _issue(
                "resolved_style_mismatch",
                f"素材 {asset_id} 的实际样式记录与参考宽度缩放结果不一致。",
                asset_id=asset_id,
            )
        )
    allowed = Image.new("L", source.size, 0)
    draw = ImageDraw.Draw(allowed)
    annotations = asset.get("annotations", [])
    if isinstance(annotations, list):
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            try:
                target = normalize_rect(annotation.get("rect"), "annotation.rect")
            except (TypeError, ValueError, OverflowError):
                continue
            draw.rounded_rectangle(
                target,
                radius=int(expected_style["rect_radius"]),
                outline=255,
                width=int(expected_style["line_width"]),
            )
            callout = annotation.get("callout")
            if not isinstance(callout, dict):
                continue
            try:
                label = normalize_rect(callout.get("label_rect"), "callout.label_rect")
            except (TypeError, ValueError, OverflowError):
                continue
            draw.rectangle(label, fill=255)
    difference = ImageChops.difference(source, annotated)
    channels = difference.split()
    changed = channels[0]
    for channel in channels[1:]:
        changed = ImageChops.lighter(changed, channel)
    outside = ImageChops.multiply(changed, ImageOps.invert(allowed))
    bounds = outside.getbbox()
    if bounds is not None:
        changed_pixels = sum(
            count
            for value, count in enumerate(outside.histogram())
            if value != 0
        )
        blockers.append(
            _issue(
                "annotated_unexpected_pixel_change",
                (
                    f"素材 {asset_id} 的标注图在框线或标签允许区域之外发生像素变化。"
                ),
                asset_id=asset_id,
                changed_pixel_count=changed_pixels,
                changed_bounds=list(bounds),
            )
        )


def _validate_inventory(
    version_dir: Path,
    manifest: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    inventory_raw = files.get("inventory") if isinstance(files, dict) else None
    if not isinstance(inventory_raw, list):
        blockers.append(
            _issue("file_inventory_missing", "manifest.files.inventory 必须是数组。")
        )
        inventory_raw = []
    inventory: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(inventory_raw, start=1):
        if not isinstance(record, dict):
            blockers.append(
                _issue(
                    "file_inventory_record_invalid",
                    f"文件清单第 {index} 项必须是 object。",
                )
            )
            continue
        relative = record.get("path")
        path, error = _inside_version(version_dir, relative)
        if error:
            blockers.append(_issue("file_path_unsafe", error))
            continue
        assert path is not None
        if relative in inventory:
            blockers.append(
                _issue("file_inventory_duplicate", f"文件清单路径重复：{relative}")
            )
            continue
        inventory[str(relative)] = record
        if not path.is_file() or path.is_symlink():
            blockers.append(
                _issue(
                    "file_missing",
                    f"文件清单中的普通文件不存在：{relative}",
                    path=relative,
                )
            )
            continue
        actual_size = path.stat().st_size
        if record.get("size_bytes") != actual_size:
            blockers.append(
                _issue(
                    "file_size_mismatch",
                    f"文件大小不一致：{relative}",
                    path=relative,
                )
            )
        claimed_hash = str(record.get("sha256", "")).lower()
        actual_hash = sha256_file(path)
        if claimed_hash != actual_hash:
            blockers.append(
                _issue(
                    "file_hash_mismatch",
                    f"文件 SHA-256 不一致：{relative}",
                    path=relative,
                )
            )
    actual_files = {
        path.relative_to(version_dir).as_posix()
        for path in version_dir.rglob("*")
        if path.is_file()
    }
    expected_files = {"manifest.json"} | set(inventory)
    for relative in sorted(expected_files - actual_files):
        blockers.append(
            _issue("expected_file_missing", f"版本目录缺少文件：{relative}", path=relative)
        )
    for relative in sorted(actual_files - expected_files):
        blockers.append(
            _issue(
                "unexpected_file",
                f"版本目录出现未登记文件：{relative}",
                path=relative,
            )
        )
    return inventory


def _validate_marker_and_copy(
    asset: dict[str, Any],
    blockers: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    number_index: int,
    letter_index: int,
) -> tuple[int, int]:
    asset_id = str(asset.get("id", ""))
    kind = str(asset.get("kind", ""))
    annotations = asset.get("annotations")
    if not isinstance(annotations, list):
        blockers.append(
            _issue(
                "annotations_invalid",
                f"素材 {asset_id} 的 annotations 不是数组。",
                asset_id=asset_id,
            )
        )
        return number_index, letter_index
    seen: set[str] = set()
    for index, annotation in enumerate(annotations, start=1):
        if not isinstance(annotation, dict):
            blockers.append(
                _issue(
                    "annotation_invalid",
                    f"素材 {asset_id} 的第 {index} 个 annotation 不是 object。",
                    asset_id=asset_id,
                )
            )
            continue
        annotation_id = str(annotation.get("id", ""))
        if not annotation_id or annotation_id in seen:
            blockers.append(
                _issue(
                    "annotation_id_invalid",
                    f"素材 {asset_id} 的 annotation ID 为空或重复：{annotation_id!r}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        seen.add(annotation_id)
        callout = annotation.get("callout")
        if not isinstance(callout, dict):
            blockers.append(
                _issue(
                    "callout_invalid",
                    f"素材 {asset_id} 的标注 {annotation_id} 缺少 callout object。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
            continue
        text, text_blockers, text_warnings = lint_callout_text(callout.get("text"))
        kind_blockers, kind_warnings = lint_copy_for_kind(text, kind)
        for issue in text_blockers + kind_blockers:
            blockers.append(
                _issue(
                    issue["code"],
                    f"素材 {asset_id} 的标注 {annotation_id}：{issue['message']}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        for issue in text_warnings + kind_warnings:
            warnings.append(
                _issue(
                    issue["code"],
                    f"素材 {asset_id} 的标注 {annotation_id}：{issue['message']}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        try:
            role = normalize_semantic_role(annotation.get("semantic_role"))
        except (TypeError, ValueError, OverflowError) as exc:
            blockers.append(
                _issue(
                    "semantic_role_invalid",
                    str(exc),
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
            continue
        requested = callout.get(
            "requested_marker",
            annotation.get("requested_marker", callout.get("marker", "auto")),
        )
        actual = callout.get(
            "resolved_marker",
            annotation.get("resolved_marker", callout.get("marker")),
        )
        try:
            expected, number_index, letter_index = resolve_marker(
                requested,
                role,
                number_index,
                letter_index,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            blockers.append(
                _issue(
                    "marker_request_invalid",
                    str(exc),
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
            continue
        if actual != expected:
            blockers.append(
                _issue(
                    "marker_semantic_mismatch",
                    (
                        f"素材 {asset_id} 的标注 {annotation_id} marker 语义不一致："
                        f"应为 {expected!r}，实际为 {actual!r}。"
                    ),
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
    return number_index, letter_index


def _effective_manifest_flow_order(manifest: dict[str, Any]) -> list[str]:
    assets = manifest.get("assets", [])
    declared = [
        str(asset.get("id"))
        for asset in assets
        if isinstance(asset, dict) and asset.get("id") is not None
    ]
    declared_set = set(declared)
    flow = manifest.get("flow")
    raw_order = flow.get("order") if isinstance(flow, dict) else None
    result: list[str] = []
    if isinstance(raw_order, list):
        for value in raw_order:
            asset_id = str(value)
            if asset_id in declared_set and asset_id not in result:
                result.append(asset_id)
    result.extend(asset_id for asset_id in declared if asset_id not in result)
    return result


def _validate_job_markers_and_copy(
    manifest: dict[str, Any],
    blockers: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    assets = manifest.get("assets", [])
    asset_map = {
        str(asset.get("id")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("id") is not None
    }
    flow = manifest.get("flow")
    numbering_scope = (
        str(flow.get("numbering_scope", "job")).strip().lower()
        if isinstance(flow, dict)
        else "job"
    )
    if numbering_scope not in {"job", "section"}:
        blockers.append(
            _issue(
                "flow_numbering_scope_invalid",
                "flow.numbering_scope 必须是 job 或 section。",
            )
        )
        numbering_scope = "job"
    counters: dict[str, tuple[int, int]] = {"__job__": (1, 1)}
    for asset_id in _effective_manifest_flow_order(manifest):
        asset = asset_map.get(asset_id)
        if asset is None:
            continue
        if numbering_scope == "section":
            section_id = str(asset.get("section_id") or "").strip()
            if not section_id:
                blockers.append(
                    _issue(
                        "section_id_required",
                        f"按章节编号时，素材 {asset_id} 必须提供非空 section_id。",
                        asset_id=asset_id,
                    )
                )
                section_id = f"__missing__:{asset_id}"
            counter_key = section_id
        else:
            counter_key = "__job__"
        number_index, letter_index = counters.setdefault(counter_key, (1, 1))
        number_index, letter_index = _validate_marker_and_copy(
            asset,
            blockers,
            warnings,
            number_index,
            letter_index,
        )
        counters[counter_key] = (number_index, letter_index)


def _validate_target_contract(
    asset_id: str,
    annotation_id: str,
    target: tuple[int, int, int, int],
    raw: Any,
    image_size: tuple[int, int],
    blockers: list[dict[str, Any]],
) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        blockers.append(
            _issue(
                "target_contract_invalid",
                f"素材 {asset_id} 的标注 {annotation_id} target_contract 不是 object。",
                asset_id=asset_id,
                annotation_id=annotation_id,
            )
        )
        return
    target_kind = str(raw.get("target_kind", "custom")).strip().lower()
    if target_kind not in TARGET_KINDS:
        blockers.append(
            _issue(
                "target_contract_kind_invalid",
                f"素材 {asset_id} 的标注 {annotation_id} target_kind 无效。",
                asset_id=asset_id,
                annotation_id=annotation_id,
            )
        )
        return
    tolerance = _bounded_int(
        raw.get("boundary_tolerance_px", 0),
        code="target_contract_tolerance_invalid",
        message=f"素材 {asset_id} 的标注 {annotation_id} 边界容差无效。",
        blockers=blockers,
        minimum=0,
        maximum=100,
    )
    if tolerance is None:
        return
    width, height = image_size
    expanded = (
        max(0, target[0] - tolerance),
        max(0, target[1] - tolerance),
        min(width, target[2] + tolerance),
        min(height, target[3] + tolerance),
    )
    include = raw.get("must_include", [])
    exclude = raw.get("must_exclude", [])
    if not isinstance(include, list) or not isinstance(exclude, list):
        blockers.append(
            _issue(
                "target_contract_items_invalid",
                f"素材 {asset_id} 的标注 {annotation_id} include/exclude 必须是数组。",
                asset_id=asset_id,
                annotation_id=annotation_id,
            )
        )
        return
    present_roles: set[str] = set()
    for index, item in enumerate(include, start=1):
        if not isinstance(item, dict):
            blockers.append(_issue("target_contract_include_invalid", f"素材 {asset_id} 的 include[{index}] 无效。", asset_id=asset_id, annotation_id=annotation_id))
            continue
        present_roles.add(str(item.get("role", "")).strip().lower())
        try:
            rect = normalize_rect(item.get("rect"), "must_include.rect")
        except (TypeError, ValueError, OverflowError):
            blockers.append(_issue("target_contract_include_rect_invalid", f"素材 {asset_id} 的 include[{index}] 坐标无效。", asset_id=asset_id, annotation_id=annotation_id))
            continue
        area = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
        covered = rect_intersection_area(expanded, rect)
        if not rect_inside(rect, width, height) or covered != area:
            blockers.append(
                _issue(
                    "target_contract_include_incomplete",
                    f"素材 {asset_id} 的标注 {annotation_id} 未完整覆盖必要目标。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                    include_id=str(item.get("id", index)),
                    coverage_ratio=round(covered / area, 6),
                )
            )
    missing = TARGET_KIND_REQUIRED_ROLES.get(target_kind, set()) - present_roles
    if missing:
        blockers.append(
            _issue(
                "target_contract_required_roles_missing",
                f"素材 {asset_id} 的标注 {annotation_id} 缺少场景必要角色。",
                asset_id=asset_id,
                annotation_id=annotation_id,
                missing_roles=sorted(missing),
            )
        )
    for index, item in enumerate(exclude, start=1):
        if not isinstance(item, dict):
            blockers.append(_issue("target_contract_exclude_invalid", f"素材 {asset_id} 的 exclude[{index}] 无效。", asset_id=asset_id, annotation_id=annotation_id))
            continue
        try:
            rect = normalize_rect(item.get("rect"), "must_exclude.rect")
        except (TypeError, ValueError, OverflowError):
            blockers.append(_issue("target_contract_exclude_rect_invalid", f"素材 {asset_id} 的 exclude[{index}] 坐标无效。", asset_id=asset_id, annotation_id=annotation_id))
            continue
        overlap = rect_intersection_area(target, rect)
        if not rect_inside(rect, width, height) or overlap:
            blockers.append(
                _issue(
                    "target_contract_exclude_overlap",
                    f"素材 {asset_id} 的标注 {annotation_id} 误包含排除目标。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                    exclude_id=str(item.get("id", index)),
                    overlap_area=overlap,
                )
            )


def _validate_geometry(
    asset: dict[str, Any],
    image_size: tuple[int, int],
    blockers: list[dict[str, Any]],
) -> None:
    asset_id = str(asset.get("id", ""))
    width, height = image_size
    protected: list[dict[str, Any]] = []
    raw_protected = asset.get("protected_regions", [])
    if not isinstance(raw_protected, list):
        blockers.append(
            _issue(
                "protected_regions_invalid",
                f"素材 {asset_id} 的 protected_regions 不是数组。",
                asset_id=asset_id,
            )
        )
        raw_protected = []
    for index, item in enumerate(raw_protected, start=1):
        if not isinstance(item, dict):
            blockers.append(
                _issue(
                    "protected_region_invalid",
                    f"素材 {asset_id} 的保护区域 {index} 不是 object。",
                    asset_id=asset_id,
                )
            )
            continue
        try:
            rect = normalize_rect(item.get("rect"), "保护区域 rect")
        except (TypeError, ValueError, OverflowError) as exc:
            blockers.append(
                _issue(
                    "protected_region_rect_invalid",
                    f"素材 {asset_id}：{exc}",
                    asset_id=asset_id,
                )
            )
            continue
        protected.append({"id": str(item.get("id", index)), "rect": rect})
        if not rect_inside(rect, width, height):
            blockers.append(
                _issue(
                    "protected_region_out_of_bounds",
                    f"素材 {asset_id} 的保护区域 {item.get('id', index)} 超出图片边界。",
                    asset_id=asset_id,
                )
            )
    labels: list[dict[str, Any]] = []
    seen_annotation_ids: set[str] = set()
    annotations = asset.get("annotations", [])
    if not isinstance(annotations, list):
        return
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        annotation_id = str(annotation.get("id", ""))
        annotation_key = portable_name_key(annotation_id)
        if not annotation_id or annotation_key in seen_annotation_ids:
            blockers.append(
                _issue(
                    "annotation_id_invalid",
                    f"素材 {asset_id} 的 annotation ID 为空或按文件系统规则重复。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        seen_annotation_ids.add(annotation_key)
        try:
            target = normalize_rect(annotation.get("rect"), "annotation.rect")
        except (TypeError, ValueError, OverflowError) as exc:
            blockers.append(
                _issue(
                    "annotation_rect_invalid",
                    f"素材 {asset_id} 的标注 {annotation_id}：{exc}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
            continue
        if not rect_inside(target, width, height):
            blockers.append(
                _issue(
                    "annotation_rect_out_of_bounds",
                    f"素材 {asset_id} 的标注 {annotation_id} 目标框超出图片边界。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        _validate_target_contract(
            asset_id,
            annotation_id,
            target,
            annotation.get("target_contract"),
            image_size,
            blockers,
        )
        callout = annotation.get("callout")
        if not isinstance(callout, dict):
            continue
        anchor = str(callout.get("anchor", ""))
        resolved_anchor = str(callout.get("resolved_anchor", ""))
        if anchor not in ANCHORS or resolved_anchor not in ANCHORS - {"auto"}:
            blockers.append(
                _issue(
                    "anchor_invalid",
                    f"素材 {asset_id} 的标注 {annotation_id} 锚点无效。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        if anchor != "auto" and resolved_anchor != anchor:
            blockers.append(
                _issue(
                    "explicit_anchor_changed",
                    f"素材 {asset_id} 的标注 {annotation_id} 显式锚点被改写。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        try:
            label = normalize_rect(callout.get("label_rect"), "callout.label_rect")
        except (TypeError, ValueError, OverflowError) as exc:
            blockers.append(
                _issue(
                    "label_rect_invalid",
                    f"素材 {asset_id} 的标注 {annotation_id}：{exc}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
            continue
        if not rect_inside(label, width, height):
            blockers.append(
                _issue(
                    "label_out_of_bounds",
                    f"素材 {asset_id} 的标注 {annotation_id} 标签超出图片边界。",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        asset_style = asset.get("style")
        maximum_distance = (
            asset_style.get("max_manual_label_distance")
            if isinstance(asset_style, dict)
            else None
        )
        if anchor == "manual":
            threshold = _bounded_int(
                maximum_distance,
                code="manual_label_distance_limit_invalid",
                message=(
                    f"素材 {asset_id} 的 max_manual_label_distance 不是正整数。"
                ),
                blockers=blockers,
                minimum=1,
            )
            distance = rect_distance(label, target)
            if threshold is not None and distance > threshold:
                blockers.append(
                    _issue(
                        "manual_label_detached",
                        (
                            f"素材 {asset_id} 的标注 {annotation_id} "
                            "手动标签离目标框过远。"
                        ),
                        asset_id=asset_id,
                        annotation_id=annotation_id,
                        target_distance=distance,
                        maximum_distance=threshold,
                    )
                )
        labels.append({"id": annotation_id, "rect": label, "target": target})
        for region in protected:
            overlap = rect_intersection_area(label, region["rect"])
            if overlap:
                blockers.append(
                    _issue(
                        "protected_region_collision",
                        (
                            f"素材 {asset_id} 的标签 {annotation_id} "
                            f"遮挡保护区域 {region['id']}。"
                        ),
                        asset_id=asset_id,
                        annotation_id=annotation_id,
                        protected_region_id=region["id"],
                        overlap_area=overlap,
                    )
                )
    for first, second in all_pairs(labels):
        overlap = rect_intersection_area(first["rect"], second["rect"])
        if overlap:
            blockers.append(
                _issue(
                    "label_collision",
                    f"素材 {asset_id} 的标签 {first['id']} 与 {second['id']} 相撞。",
                    asset_id=asset_id,
                    annotation_ids=[first["id"], second["id"]],
                    overlap_area=overlap,
                )
            )
    for label in labels:
        for other in labels:
            if label["id"] == other["id"]:
                continue
            overlap = rect_intersection_area(label["rect"], other["target"])
            if overlap:
                blockers.append(
                    _issue(
                        "unrelated_target_collision",
                        (
                            f"素材 {asset_id} 的标签 {label['id']} "
                            f"遮挡了无关目标框 {other['id']}。"
                        ),
                        asset_id=asset_id,
                        annotation_id=label["id"],
                        other_annotation_id=other["id"],
                        overlap_area=overlap,
                    )
                )


def _validate_image_assets(
    version_dir: Path,
    assets: Any,
    base_style: dict[str, Any],
    blockers: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    image_sizes: dict[str, tuple[int, int]] = {}
    source_hashes: dict[str, str] = {}
    if not isinstance(assets, list) or not assets:
        blockers.append(_issue("assets_invalid", "manifest.assets 必须是非空数组。"))
        return image_sizes, source_hashes
    seen_ids: set[str] = set()
    seen_outputs: set[str] = set()
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, dict):
            blockers.append(
                _issue("asset_invalid", f"manifest.assets[{index}] 不是 object。")
            )
            continue
        asset_id = str(asset.get("id", "")).strip()
        asset_key = portable_name_key(asset_id)
        if (
            not asset_id
            or asset_key in seen_ids
            or "/" in asset_id
            or "\\" in asset_id
            or any(ord(character) < 32 for character in asset_id)
        ):
            blockers.append(
                _issue(
                    "asset_id_invalid",
                    f"素材 ID 为空或重复：{asset_id!r}",
                    asset_id=asset_id,
                )
            )
        seen_ids.add(asset_key)
        output_name = str(asset.get("output_name", ""))
        output_key = portable_name_key(output_name)
        if (
            not output_name
            or output_key in seen_outputs
            or Path(output_name).name != output_name
            or "\\" in output_name
            or any(ord(character) < 32 for character in output_name)
            or Path(output_name).suffix.lower() != ".png"
        ):
            blockers.append(
                _issue(
                    "output_name_invalid",
                    f"素材 {asset_id} 的 output_name 不安全、重复或不是 PNG。",
                    asset_id=asset_id,
                )
            )
        seen_outputs.add(output_key)
        source_path, source_error = _inside_version(
            version_dir,
            asset.get("source_archive"),
        )
        if source_error:
            blockers.append(
                _issue(
                    "source_archive_path_unsafe",
                    f"素材 {asset_id}：{source_error}",
                    asset_id=asset_id,
                )
            )
            continue
        assert source_path is not None
        if not source_path.is_file() or source_path.is_symlink():
            blockers.append(
                _issue(
                    "source_archive_missing",
                    f"素材 {asset_id} 的归档副本不存在。",
                    asset_id=asset_id,
                )
            )
            continue
        source_hash = sha256_file(source_path)
        source_hashes[asset_id] = source_hash
        if source_hash.lower() != str(asset.get("source_sha256", "")).lower():
            blockers.append(
                _issue(
                    "source_hash_mismatch",
                    f"素材 {asset_id} 的归档源文件 SHA-256 不一致。",
                    asset_id=asset_id,
                )
            )
        if source_path.stat().st_size != asset.get("source_size_bytes"):
            blockers.append(
                _issue(
                    "source_size_mismatch",
                    f"素材 {asset_id} 的归档源文件大小不一致。",
                    asset_id=asset_id,
                )
            )
        try:
            with Image.open(source_path) as source:
                source_format = str(source.format or "").upper()
                frame_count = int(getattr(source, "n_frames", 1))
                is_animated = bool(getattr(source, "is_animated", False))
                transposed_size = ImageOps.exif_transpose(source).size
        except (OSError, UnidentifiedImageError):
            source_format = ""
            frame_count = 0
            is_animated = False
            transposed_size = None
        renderable = (
            source_format in {"PNG", "JPEG", "WEBP"}
            and frame_count == 1
            and not is_animated
            and transposed_size is not None
        )
        render_status = asset.get("render_status")
        if renderable and render_status != "rendered":
            blockers.append(
                _issue(
                    "renderable_marked_unsupported",
                    f"素材 {asset_id} 可渲染，却被标记为 unsupported。",
                    asset_id=asset_id,
                )
            )
        if not renderable and render_status != "unsupported":
            blockers.append(
                _issue(
                    "unsupported_marked_rendered",
                    f"素材 {asset_id} 不是受支持的单帧栅格，却被标记为 rendered。",
                    asset_id=asset_id,
                )
            )
        if not renderable:
            if asset.get("annotated"):
                blockers.append(
                    _issue(
                        "unsupported_has_annotated",
                        f"不支持的素材 {asset_id} 不得声明 annotated 文件。",
                        asset_id=asset_id,
                    )
                )
            if render_status == "unsupported":
                blockers.append(
                    _issue(
                        "unsupported_asset",
                        (
                            f"素材 {asset_id} 不是受支持的单帧 PNG/JPEG/WebP，"
                            "项目不能自动放行。"
                        ),
                        asset_id=asset_id,
                    )
                )
            continue
        assert transposed_size is not None
        image_sizes[asset_id] = transposed_size
        recorded_size = asset.get("image_size")
        if recorded_size != [transposed_size[0], transposed_size[1]]:
            blockers.append(
                _issue(
                    "source_dimensions_mismatch",
                    f"素材 {asset_id} 的记录尺寸与源图显示尺寸不一致。",
                    asset_id=asset_id,
                )
            )
        annotated_path, annotated_error = _inside_version(
            version_dir,
            asset.get("annotated"),
        )
        if annotated_error:
            blockers.append(
                _issue(
                    "annotated_path_unsafe",
                    f"素材 {asset_id}：{annotated_error}",
                    asset_id=asset_id,
                )
            )
        elif annotated_path is None or not annotated_path.is_file():
            blockers.append(
                _issue(
                    "annotated_missing",
                    f"素材 {asset_id} 缺少标注图。",
                    asset_id=asset_id,
                )
            )
        else:
            if sha256_file(annotated_path).lower() != str(
                asset.get("annotated_sha256", "")
            ).lower():
                blockers.append(
                    _issue(
                        "annotated_hash_mismatch",
                        f"素材 {asset_id} 的标注图 SHA-256 不一致。",
                        asset_id=asset_id,
                    )
                )
            if annotated_path.stat().st_size != asset.get("annotated_size_bytes"):
                blockers.append(
                    _issue(
                        "annotated_size_mismatch",
                        f"素材 {asset_id} 的标注图文件大小不一致。",
                        asset_id=asset_id,
                    )
                )
            try:
                with Image.open(annotated_path) as annotated:
                    if annotated.size != transposed_size:
                        blockers.append(
                            _issue(
                                "annotated_dimensions_mismatch",
                                f"素材 {asset_id} 的标注图尺寸与源图显示尺寸不一致。",
                                asset_id=asset_id,
                            )
                        )
                    metadata = _metadata_keys(annotated)
                    if metadata:
                        blockers.append(
                            _issue(
                                "derived_metadata_present",
                                f"素材 {asset_id} 的标注图仍含元数据：{metadata}",
                                asset_id=asset_id,
                            )
                        )
            except (OSError, UnidentifiedImageError) as exc:
                blockers.append(
                    _issue(
                        "annotated_unreadable",
                        f"素材 {asset_id} 的标注图无法读取：{exc}",
                        asset_id=asset_id,
                    )
                )
            _validate_annotated_pixels(
                source_path,
                annotated_path,
                asset,
                base_style,
                blockers,
            )
        _validate_geometry(asset, transposed_size, blockers)
    return image_sizes, source_hashes


def _validate_flow(
    manifest: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    assets = manifest.get("assets", [])
    asset_ids = {
        str(asset.get("id"))
        for asset in assets
        if isinstance(asset, dict) and asset.get("id") is not None
    }
    annotation_roles = {
        str(asset.get("id")): {
            str(annotation.get("id")): str(annotation.get("semantic_role", ""))
            for annotation in asset.get("annotations", [])
            if isinstance(annotation, dict)
        }
        for asset in assets
        if isinstance(asset, dict)
    }
    flow = manifest.get("flow")
    if not isinstance(flow, dict):
        blockers.append(_issue("flow_invalid", "manifest.flow 必须是 object。"))
        return
    order = flow.get("order")
    if order is not None:
        if not isinstance(order, list) or len(order) != len(set(map(str, order))):
            blockers.append(
                _issue("flow_order_invalid", "flow.order 必须是不重复的素材 ID 数组。")
            )
        elif any(str(value) not in asset_ids for value in order):
            blockers.append(
                _issue("flow_order_missing_asset", "flow.order 引用了不存在的素材。")
            )
        elif set(map(str, order)) != asset_ids:
            blockers.append(
                _issue(
                    "flow_order_incomplete",
                    "flow.order 必须完整包含每个素材且只出现一次。",
                )
            )
    edges = flow.get("edges", [])
    if not isinstance(edges, list):
        blockers.append(_issue("flow_edges_invalid", "flow.edges 必须是数组。"))
        return
    seen_edges: set[tuple[str, str, str, str]] = set()
    callout_targets: dict[tuple[str, str], tuple[str, str]] = {}
    graph: dict[str, set[str]] = {asset_id: set() for asset_id in asset_ids}
    for index, edge in enumerate(edges, start=1):
        if not isinstance(edge, dict):
            blockers.append(
                _issue("flow_edge_invalid", f"flow.edges[{index}] 必须是 object。")
            )
            continue
        from_id = str(edge.get("from", ""))
        to_id = str(edge.get("to", ""))
        kind = str(edge.get("kind", ""))
        callout_raw = edge.get("callout_id")
        callout_id = str(callout_raw).strip() if callout_raw is not None else ""
        if from_id not in asset_ids:
            blockers.append(
                _issue(
                    "flow_from_missing",
                    f"流程边 {index} 的起点素材不存在：{from_id!r}。",
                )
            )
        if to_id not in asset_ids:
            blockers.append(
                _issue(
                    "flow_to_missing",
                    f"流程边 {index} 的终点素材不存在：{to_id!r}。",
                )
            )
        if kind not in {"next", "alternative"}:
            blockers.append(
                _issue("flow_kind_invalid", f"流程边 {index} 的 kind 无效。")
            )
        if not callout_id:
            blockers.append(
                _issue(
                    "flow_callout_required",
                    f"流程边 {index} 必须提供非空 callout_id。",
                )
            )
        elif callout_id not in annotation_roles.get(from_id, {}):
            blockers.append(
                _issue(
                    "flow_callout_missing",
                    f"流程边 {index} 的 callout_id 不存在。",
                )
            )
        else:
            role = annotation_roles[from_id][callout_id]
            compatible = (
                kind == "next" and role in {"single", "sequence"}
            ) or (kind == "alternative" and role == "alternative")
            if kind in {"next", "alternative"} and not compatible:
                blockers.append(
                    _issue(
                        "flow_kind_role_mismatch",
                        (
                            f"流程边 {index} 的 kind={kind} 与标注 "
                            f"{from_id}/{callout_id} 的 semantic_role={role} 不兼容。"
                        ),
                    )
                )

        edge_key = (from_id, to_id, kind, callout_id)
        if edge_key in seen_edges:
            blockers.append(
                _issue("flow_edge_duplicate", f"流程边 {index} 与前面的边完全重复。")
            )
        else:
            seen_edges.add(edge_key)
        if callout_id:
            callout_key = (from_id, callout_id)
            target = (to_id, kind)
            previous = callout_targets.get(callout_key)
            if previous is not None and previous != target:
                blockers.append(
                    _issue(
                        "flow_callout_fanout",
                        f"标注 {from_id}/{callout_id} 不能同时连接多个流程目标。",
                    )
                )
            else:
                callout_targets[callout_key] = target
        if from_id == to_id and from_id in asset_ids:
            blockers.append(
                _issue(
                    "flow_self_loop",
                    f"流程边 {index} 不能从素材 {from_id} 指回自身。",
                )
            )
        elif from_id in asset_ids and to_id in asset_ids:
            graph[from_id].add(to_id)

    indegree = {asset_id: 0 for asset_id in asset_ids}
    for targets in graph.values():
        for target in targets:
            indegree[target] += 1
    pending = [asset_id for asset_id, degree in indegree.items() if degree == 0]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        for target in graph[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(asset_ids):
        blockers.append(
            _issue(
                "flow_cycle",
                "流程图必须是 DAG，检测到环路："
                + "、".join(
                    sorted(asset_id for asset_id, degree in indegree.items() if degree > 0)
                ),
            )
        )


def _validate_overview(
    version_dir: Path,
    manifest: dict[str, Any],
    image_sizes: dict[str, tuple[int, int]],
    assets: Any,
    blockers: list[dict[str, Any]],
) -> None:
    overview = manifest.get("overview")
    if not isinstance(overview, dict):
        blockers.append(_issue("overview_invalid", "manifest.overview 必须是 object。"))
        return
    mode = overview.get("resolved_mode")
    if mode not in {"vertical", "grid"}:
        blockers.append(_issue("overview_mode_invalid", "overview.resolved_mode 无效。"))
    columns = overview.get("columns")
    if mode == "grid" and (not isinstance(columns, int) or not 4 <= columns <= 7):
        blockers.append(
            _issue("overview_columns_invalid", "grid overview.columns 必须在 4 到 7。")
        )
    maximum_pixels = _bounded_int(
        overview.get("max_pixels"),
        code="overview_max_pixels_invalid",
        message="overview.max_pixels 必须是不小于 250000 的整数。",
        blockers=blockers,
        minimum=250_000,
    )
    maximum_height = _bounded_int(
        overview.get("max_page_height"),
        code="overview_max_page_height_invalid",
        message="overview.max_page_height 必须是不小于 200 的整数。",
        blockers=blockers,
        minimum=200,
    )
    try:
        minimum_label_font = float(overview.get("min_label_font_px"))
    except (TypeError, ValueError, OverflowError):
        minimum_label_font = None
        blockers.append(
            _issue(
                "overview_min_label_font_invalid",
                "overview.min_label_font_px 必须在 8 到 96 之间。",
            )
        )
    else:
        if not 8 <= minimum_label_font <= 96:
            blockers.append(
                _issue(
                    "overview_min_label_font_invalid",
                    "overview.min_label_font_px 必须在 8 到 96 之间。",
                )
            )
            minimum_label_font = None
    pages = overview.get("pages")
    if not isinstance(pages, list) or not pages:
        blockers.append(_issue("overview_pages_missing", "overview.pages 必须非空。"))
        return
    annotated_paths: dict[str, Path] = {}
    label_fonts: dict[str, int] = {}
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("render_status") != "rendered":
                continue
            path, error = _inside_version(version_dir, asset.get("annotated"))
            if error is None and path is not None and path.is_file():
                asset_id = str(asset.get("id", ""))
                annotated_paths[asset_id] = path
                asset_style = asset.get("style")
                if isinstance(asset_style, dict):
                    try:
                        label_fonts[asset_id] = int(
                            asset_style.get("callout_font_size")
                        )
                    except (TypeError, ValueError, OverflowError):
                        blockers.append(
                            _issue(
                                "overview_source_label_font_invalid",
                                f"素材 {asset_id} 的源标签字号无效。",
                                asset_id=asset_id,
                            )
                        )
    placed: list[str] = []
    for page_index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            blockers.append(
                _issue("overview_page_invalid", f"overview 第 {page_index} 页不是 object。")
            )
            continue
        page_path, error = _inside_version(version_dir, page.get("path"))
        if error or page_path is None or not page_path.is_file():
            blockers.append(
                _issue(
                    "overview_page_missing",
                    f"overview 第 {page_index} 页不存在或路径不安全。",
                )
            )
            continue
        try:
            with Image.open(page_path) as image:
                size = image.size
                metadata = _metadata_keys(image)
                page_pixels = image.convert("RGB")
        except (OSError, UnidentifiedImageError) as exc:
            blockers.append(
                _issue(
                    "overview_page_unreadable",
                    f"overview 第 {page_index} 页无法读取：{exc}",
                )
            )
            continue
        if page.get("size") != [size[0], size[1]]:
            blockers.append(
                _issue(
                    "overview_size_mismatch",
                    f"overview 第 {page_index} 页记录尺寸不一致。",
                )
            )
        pixels = size[0] * size[1]
        if page.get("pixel_count") != pixels:
            blockers.append(
                _issue(
                    "overview_pixel_count_mismatch",
                    f"overview 第 {page_index} 页像素数记录不一致。",
                )
            )
        if maximum_pixels is not None and pixels > maximum_pixels:
            blockers.append(
                _issue(
                    "overview_pixel_budget_exceeded",
                    f"overview 第 {page_index} 页超过像素预算。",
                    pixel_count=pixels,
                    max_pixels=maximum_pixels,
                )
            )
        if maximum_height is not None and size[1] > maximum_height:
            blockers.append(
                _issue(
                    "overview_height_budget_exceeded",
                    f"overview 第 {page_index} 页超过高度预算。",
                )
            )
        if metadata:
            blockers.append(
                _issue(
                    "derived_metadata_present",
                    f"overview 第 {page_index} 页仍含元数据：{metadata}",
                )
            )
        page_placements = page.get("placements", [])
        if not isinstance(page_placements, list):
            blockers.append(
                _issue(
                    "overview_placements_invalid",
                    f"overview 第 {page_index} 页 placements 不是数组。",
                )
            )
            continue
        normalized_placements = []
        for placement in page_placements:
            if not isinstance(placement, dict):
                blockers.append(
                    _issue(
                        "overview_placement_invalid",
                        f"overview 第 {page_index} 页含无效 placement。",
                    )
                )
                continue
            asset_id = str(placement.get("asset_id", ""))
            if asset_id not in image_sizes:
                blockers.append(
                    _issue(
                        "overview_unknown_asset",
                        f"overview 引用了不存在或不可渲染的素材：{asset_id!r}。",
                    )
                )
            try:
                rect = normalize_rect(placement.get("rect"), "overview placement.rect")
            except (TypeError, ValueError, OverflowError) as exc:
                blockers.append(_issue("overview_rect_invalid", str(exc)))
                continue
            if not rect_inside(rect, size[0], size[1]):
                blockers.append(
                    _issue(
                        "overview_placement_out_of_bounds",
                        f"overview 中素材 {asset_id} 的 placement 超出页面。",
                    )
                )
            if placement.get("rendered_size") != [rect[2] - rect[0], rect[3] - rect[1]]:
                blockers.append(
                    _issue(
                        "overview_rendered_size_mismatch",
                        f"overview 中素材 {asset_id} 的 rendered_size 不一致。",
                    )
                )
            expected_source = image_sizes.get(asset_id)
            if expected_source and placement.get("source_size") != list(expected_source):
                blockers.append(
                    _issue(
                        "overview_source_size_mismatch",
                        f"overview 中素材 {asset_id} 的 source_size 不一致。",
                    )
                )
            if expected_source:
                rendered_width = rect[2] - rect[0]
                rendered_height = rect[3] - rect[1]
                expected_scale = round(
                    min(
                        rendered_width / max(1, expected_source[0]),
                        rendered_height / max(1, expected_source[1]),
                    ),
                    6,
                )
                try:
                    recorded_scale = float(placement.get("scale"))
                except (TypeError, ValueError, OverflowError):
                    recorded_scale = None
                if recorded_scale is None or abs(recorded_scale - expected_scale) > 0.000001:
                    blockers.append(
                        _issue(
                            "overview_scale_mismatch",
                            f"overview 中素材 {asset_id} 的 scale 记录不一致。",
                            asset_id=asset_id,
                        )
                    )
                source_font = label_fonts.get(asset_id)
                if source_font is not None:
                    if placement.get("source_label_font_px") != source_font:
                        blockers.append(
                            _issue(
                                "overview_source_label_font_mismatch",
                                f"overview 中素材 {asset_id} 的源标签字号不一致。",
                                asset_id=asset_id,
                            )
                        )
                    effective_font = round(source_font * expected_scale, 3)
                    try:
                        recorded_effective = float(
                            placement.get("effective_label_font_px")
                        )
                    except (TypeError, ValueError, OverflowError):
                        recorded_effective = None
                    if (
                        recorded_effective is None
                        or abs(recorded_effective - effective_font) > 0.001
                    ):
                        blockers.append(
                            _issue(
                                "overview_effective_label_font_mismatch",
                                f"overview 中素材 {asset_id} 的有效标签字号记录不一致。",
                                asset_id=asset_id,
                            )
                        )
                    if (
                        minimum_label_font is not None
                        and effective_font < minimum_label_font
                    ):
                        blockers.append(
                            _issue(
                                "overview_label_unreadable",
                                (
                                    f"素材 {asset_id} 在总览中的有效标签字号 "
                                    f"{effective_font}px 低于 {minimum_label_font}px。"
                                ),
                                asset_id=asset_id,
                                effective_label_font_px=effective_font,
                                minimum_label_font_px=minimum_label_font,
                            )
                        )
            annotated_path = annotated_paths.get(asset_id)
            rendered_size = placement.get("rendered_size")
            if (
                annotated_path is not None
                and isinstance(rendered_size, list)
                and len(rendered_size) == 2
            ):
                try:
                    expected_width = int(rendered_size[0])
                    expected_height = int(rendered_size[1])
                    with Image.open(annotated_path) as annotated_opened:
                        expected_pixels = annotated_opened.convert("RGB")
                        expected_pixels.thumbnail(
                            (expected_width, expected_height),
                            lanczos_resample_filter(),
                        )
                    actual_pixels = page_pixels.crop(rect)
                    if (
                        expected_pixels.size != (expected_width, expected_height)
                        or actual_pixels.size != (expected_width, expected_height)
                        or ImageChops.difference(
                            expected_pixels,
                            actual_pixels,
                        ).getbbox()
                        is not None
                    ):
                        blockers.append(
                            _issue(
                                "overview_content_mismatch",
                                (
                                    f"overview 中素材 {asset_id} 的页内像素与标注图缩放结果不一致。"
                                ),
                                asset_id=asset_id,
                                page=page_index,
                            )
                        )
                except (
                    OSError,
                    UnidentifiedImageError,
                    TypeError,
                    ValueError,
                    OverflowError,
                ) as exc:
                    blockers.append(
                        _issue(
                            "overview_content_unverifiable",
                            f"overview 中素材 {asset_id} 无法执行内容校验：{exc}",
                            asset_id=asset_id,
                            page=page_index,
                        )
                    )
            placed.append(asset_id)
            normalized_placements.append({"id": asset_id, "rect": rect})
        for first, second in all_pairs(normalized_placements):
            if rect_intersection_area(first["rect"], second["rect"]):
                blockers.append(
                    _issue(
                        "overview_placement_collision",
                        (
                            f"overview 第 {page_index} 页素材 {first['id']} 与 "
                            f"{second['id']} 的 placement 相撞。"
                        ),
                    )
                )
    expected = set(image_sizes)
    if set(placed) != expected or len(placed) != len(expected):
        blockers.append(
            _issue(
                "overview_asset_coverage_mismatch",
                "overview 必须且只能放置每个已渲染素材一次。",
            )
        )
    expected_order = [
        asset_id
        for asset_id in _effective_manifest_flow_order(manifest)
        if asset_id in image_sizes
    ]
    if placed != expected_order:
        blockers.append(
            _issue(
                "overview_flow_order_mismatch",
                "overview placements 顺序必须与 flow.order 一致。",
            )
        )


def _validate_annotation_crops(
    version_dir: Path,
    manifest: dict[str, Any],
    qa: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    config = qa.get("annotation_crops")
    if not isinstance(config, dict):
        blockers.append(
            _issue("annotation_crops_missing", "qa.annotation_crops 必须是 object。")
        )
        return
    padding = _bounded_int(
        config.get("padding"),
        code="annotation_crop_padding_invalid",
        message="qa.annotation_crops.padding 必须在 0 到 2000 之间。",
        blockers=blockers,
        minimum=0,
        maximum=2000,
    )
    files = config.get("files")
    if not isinstance(files, list):
        blockers.append(
            _issue("annotation_crop_files_invalid", "annotation_crops.files 必须是数组。")
        )
        return

    expected: dict[tuple[str, str], dict[str, Any]] = {}
    embedded: dict[tuple[str, str], dict[str, Any]] = {}
    assets = manifest.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("render_status") != "rendered":
                continue
            asset_id = str(asset.get("id", ""))
            annotated_path, path_error = _inside_version(
                version_dir,
                asset.get("annotated"),
            )
            if (
                path_error
                or annotated_path is None
                or not annotated_path.is_file()
            ):
                continue
            annotations = asset.get("annotations")
            if isinstance(annotations, list):
                for annotation in annotations:
                    if not isinstance(annotation, dict):
                        continue
                    annotation_id = str(annotation.get("id", ""))
                    try:
                        target = normalize_rect(
                            annotation.get("rect"),
                            "annotation crop target",
                        )
                        callout = annotation.get("callout")
                        if not isinstance(callout, dict):
                            raise ValueError("callout 必须是 object")
                        label = normalize_rect(
                            callout.get("label_rect"),
                            "annotation crop label",
                        )
                    except (TypeError, ValueError, OverflowError):
                        continue
                    try:
                        with Image.open(annotated_path) as opened:
                            width, height = opened.size
                    except (OSError, UnidentifiedImageError):
                        continue
                    if padding is None:
                        continue
                    expected[(asset_id, annotation_id)] = {
                        "annotated_path": annotated_path,
                        "crop_rect": (
                            max(0, min(target[0], label[0]) - padding),
                            max(0, min(target[1], label[1]) - padding),
                            min(width, max(target[2], label[2]) + padding),
                            min(height, max(target[3], label[3]) + padding),
                        ),
                    }
            asset_records = asset.get("annotation_crops")
            if not isinstance(asset_records, list):
                blockers.append(
                    _issue(
                        "asset_annotation_crops_invalid",
                        f"素材 {asset_id} 的 annotation_crops 必须是数组。",
                        asset_id=asset_id,
                    )
                )
                continue
            for record in asset_records:
                if not isinstance(record, dict):
                    continue
                pair = (
                    str(record.get("asset_id", "")),
                    str(record.get("annotation_id", "")),
                )
                if pair in embedded:
                    blockers.append(
                        _issue(
                            "asset_annotation_crop_duplicate",
                            f"素材 {asset_id} 的局部裁切记录重复。",
                            asset_id=asset_id,
                        )
                    )
                embedded[pair] = record

    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for record in files:
        if not isinstance(record, dict):
            blockers.append(
                _issue("annotation_crop_record_invalid", "局部裁切记录必须是 object。")
            )
            continue
        pair = (
            str(record.get("asset_id", "")),
            str(record.get("annotation_id", "")),
        )
        if pair in actual:
            blockers.append(
                _issue(
                    "annotation_crop_duplicate",
                    f"局部裁切记录重复：{pair[0]}/{pair[1]}。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
            continue
        actual[pair] = record
        expected_item = expected.get(pair)
        if expected_item is None:
            blockers.append(
                _issue(
                    "annotation_crop_unknown",
                    f"局部裁切引用未知标注：{pair[0]}/{pair[1]}。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
            continue
        try:
            crop_rect = normalize_rect(record.get("crop_rect"), "crop_rect")
        except (TypeError, ValueError, OverflowError) as exc:
            blockers.append(
                _issue(
                    "annotation_crop_rect_invalid",
                    f"局部裁切 {pair[0]}/{pair[1]}：{exc}",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
            continue
        if crop_rect != expected_item["crop_rect"]:
            blockers.append(
                _issue(
                    "annotation_crop_rect_mismatch",
                    f"局部裁切 {pair[0]}/{pair[1]} 的范围不一致。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
        path, path_error = _inside_version(version_dir, record.get("path"))
        if path_error or path is None or not path.is_file() or path.is_symlink():
            blockers.append(
                _issue(
                    "annotation_crop_missing",
                    f"局部裁切 {pair[0]}/{pair[1]} 不存在或路径不安全。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
            continue
        if sha256_file(path).lower() != str(record.get("sha256", "")).lower():
            blockers.append(
                _issue(
                    "annotation_crop_hash_mismatch",
                    f"局部裁切 {pair[0]}/{pair[1]} 的哈希不一致。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
        try:
            with Image.open(path) as crop_opened:
                actual_pixels = crop_opened.convert("RGBA")
                actual_size = crop_opened.size
                metadata = _metadata_keys(crop_opened)
            with Image.open(expected_item["annotated_path"]) as annotated_opened:
                expected_pixels = annotated_opened.convert("RGBA").crop(
                    expected_item["crop_rect"]
                )
        except (OSError, UnidentifiedImageError) as exc:
            blockers.append(
                _issue(
                    "annotation_crop_unreadable",
                    f"局部裁切 {pair[0]}/{pair[1]} 无法读取：{exc}",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
            continue
        if record.get("size") != [actual_size[0], actual_size[1]]:
            blockers.append(
                _issue(
                    "annotation_crop_size_mismatch",
                    f"局部裁切 {pair[0]}/{pair[1]} 的尺寸记录不一致。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
        if (
            actual_pixels.size != expected_pixels.size
            or ImageChops.difference(actual_pixels, expected_pixels).getbbox()
            is not None
        ):
            blockers.append(
                _issue(
                    "annotation_crop_content_mismatch",
                    f"局部裁切 {pair[0]}/{pair[1]} 与标注图像素不一致。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )
        if metadata:
            blockers.append(
                _issue(
                    "derived_metadata_present",
                    f"局部裁切 {pair[0]}/{pair[1]} 仍含元数据：{metadata}",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )

    if set(actual) != set(expected):
        blockers.append(
            _issue(
                "annotation_crop_coverage_mismatch",
                "每个已渲染标注必须且只能有一张局部裁切图。",
            )
        )
    if set(embedded) != set(expected):
        blockers.append(
            _issue(
                "asset_annotation_crop_coverage_mismatch",
                "资产级 annotation_crops 必须完整覆盖全部标注。",
            )
        )
    for pair in set(actual) & set(embedded):
        if actual[pair] != embedded[pair]:
            blockers.append(
                _issue(
                    "annotation_crop_record_mismatch",
                    f"局部裁切 {pair[0]}/{pair[1]} 的双向记录不一致。",
                    asset_id=pair[0],
                    annotation_id=pair[1],
                )
            )


def _validate_qa_images(
    version_dir: Path,
    manifest: dict[str, Any],
    image_sizes: dict[str, tuple[int, int]],
    blockers: list[dict[str, Any]],
) -> None:
    qa = manifest.get("qa")
    if not isinstance(qa, dict):
        blockers.append(_issue("qa_invalid", "manifest.qa 必须是 object。"))
        return
    _validate_annotation_crops(version_dir, manifest, qa, blockers)
    contact = qa.get("full_contact_sheet")
    if not isinstance(contact, dict):
        blockers.append(
            _issue("contact_sheet_record_missing", "缺少 full_contact_sheet 记录。")
        )
    else:
        contact_path, error = _inside_version(version_dir, contact.get("path"))
        if error or contact_path is None or not contact_path.is_file():
            blockers.append(
                _issue("contact_sheet_missing", "qa/full-contact-sheet.png 不存在。")
            )
        else:
            try:
                with Image.open(contact_path) as image:
                    size = image.size
                    metadata = _metadata_keys(image)
            except (OSError, UnidentifiedImageError) as exc:
                blockers.append(
                    _issue("contact_sheet_unreadable", f"full contact sheet 无法读取：{exc}")
                )
            else:
                if contact.get("size") != [size[0], size[1]]:
                    blockers.append(
                        _issue(
                            "contact_sheet_size_mismatch",
                            "full contact sheet 记录尺寸不一致。",
                        )
                    )
                if contact.get("pixel_count") != size[0] * size[1]:
                    blockers.append(
                        _issue(
                            "contact_sheet_pixel_count_mismatch",
                            "full contact sheet 像素数记录不一致。",
                        )
                    )
                if metadata:
                    blockers.append(
                        _issue(
                            "derived_metadata_present",
                            f"full contact sheet 仍含元数据：{metadata}",
                        )
                    )
    grid_config = qa.get("coordinate_grids", {})
    if not isinstance(grid_config, dict):
        blockers.append(
            _issue("coordinate_grids_invalid", "qa.coordinate_grids 必须是 object。")
        )
        return
    files = grid_config.get("files", [])
    if not isinstance(files, list):
        blockers.append(
            _issue("coordinate_grid_files_invalid", "coordinate_grids.files 必须是数组。")
        )
        return
    requested = grid_config.get("assets", [])
    if grid_config.get("enabled") and requested == "all":
        expected_ids = set(image_sizes)
    elif grid_config.get("enabled") and isinstance(requested, list):
        expected_ids = set(map(str, requested)) & set(image_sizes)
    else:
        expected_ids = set()
    actual_ids: set[str] = set()
    for record in files:
        if not isinstance(record, dict):
            blockers.append(
                _issue("coordinate_grid_record_invalid", "坐标网格记录必须是 object。")
            )
            continue
        asset_id = str(record.get("asset_id", ""))
        actual_ids.add(asset_id)
        path, error = _inside_version(version_dir, record.get("path"))
        if error or path is None or not path.is_file():
            blockers.append(
                _issue(
                    "coordinate_grid_missing",
                    f"素材 {asset_id} 的坐标网格不存在。",
                    asset_id=asset_id,
                )
            )
            continue
        try:
            with Image.open(path) as image:
                size = image.size
                metadata = _metadata_keys(image)
        except (OSError, UnidentifiedImageError) as exc:
            blockers.append(
                _issue(
                    "coordinate_grid_unreadable",
                    f"素材 {asset_id} 的坐标网格无法读取：{exc}",
                    asset_id=asset_id,
                )
            )
            continue
        if record.get("size") != [size[0], size[1]] or image_sizes.get(asset_id) != size:
            blockers.append(
                _issue(
                    "coordinate_grid_size_mismatch",
                    f"素材 {asset_id} 的坐标网格尺寸不一致。",
                    asset_id=asset_id,
                )
            )
        if sha256_file(path).lower() != str(record.get("sha256", "")).lower():
            blockers.append(
                _issue(
                    "coordinate_grid_hash_mismatch",
                    f"素材 {asset_id} 的坐标网格哈希不一致。",
                    asset_id=asset_id,
                )
            )
        _bounded_int(
            record.get("step"),
            code="coordinate_grid_step_invalid",
            message=f"素材 {asset_id} 的坐标网格 step 无效。",
            blockers=blockers,
            minimum=10,
            maximum=2000,
        )
        if metadata:
            blockers.append(
                _issue(
                    "derived_metadata_present",
                    f"素材 {asset_id} 的坐标网格仍含元数据：{metadata}",
                    asset_id=asset_id,
                )
            )
    if actual_ids != expected_ids:
        blockers.append(
            _issue(
                "coordinate_grid_coverage_mismatch",
                "坐标网格文件集合与 qa.coordinate_grids 选择不一致。",
            )
        )


def _audit_passed(payload: dict[str, Any]) -> bool:
    if payload.get("passed") is True or payload.get("deterministic_passed") is True:
        return True
    status = str(payload.get("status", payload.get("result", ""))).strip().lower()
    return status in {"passed", "pass", "approved", "ok", "通过", "已通过"}


def _normalized_sha256(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return None


def _watermark_indicates_removed(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("removed") is True:
            return True
        return any(
            _watermark_indicates_removed(value.get(key))
            for key in ("status", "action", "result", "treatment")
            if key in value
        )
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in {
        "removed",
        "watermark_removed",
        "authorized_removed",
        "deterministically_removed",
        "remove",
        "已移除",
        "已去除",
        "已删除",
    } or (
        "removed" in normalized
        and normalized not in {"not_removed", "unremoved", "preserve_not_removed"}
    )


def _watermark_requires_preservation(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("unknown") is True:
            return True
        return any(
            _watermark_requires_preservation(value.get(key))
            for key in ("status", "ownership", "copyright", "policy")
            if key in value
        )
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return (
        normalized in {
            "preserve_unknown",
            "unknown",
            "unknown_copyright",
            "third_party_unlicensed",
            "preserve_third_party",
            "未知",
            "未知版权",
        }
        or "unknown" in normalized
        or "未知" in normalized
        or "unlicensed" in normalized
    )


def _validate_summary_allowlist(
    summary: dict[str, Any],
    *,
    kind: str,
    code_prefix: str,
    report_fields: set[str],
    boolean_check_fields: set[str],
    blockers: list[dict[str, Any]],
) -> bool:
    valid = True
    top_fields = {
        "schema_version",
        "kind",
        "required",
        "passed",
        "load_complete",
        "reports",
    }
    if set(summary) - top_fields:
        blockers.append(
            _issue(
                f"{code_prefix}_summary_unsafe_field",
                "审计摘要包含非白名单顶层字段。",
            )
        )
        valid = False
    if summary.get("schema_version") != "2.0-summary" or summary.get("kind") != kind:
        blockers.append(
            _issue(
                f"{code_prefix}_summary_schema_invalid",
                "审计摘要 schema_version 或 kind 无效。",
            )
        )
        valid = False
    for key in ("required", "passed", "load_complete"):
        if not isinstance(summary.get(key), bool):
            blockers.append(
                _issue(
                    f"{code_prefix}_summary_type_invalid",
                    f"审计摘要 {key} 必须是 boolean。",
                )
            )
            valid = False
    reports = summary.get("reports")
    if not isinstance(reports, dict):
        blockers.append(
            _issue(
                f"{code_prefix}_summary_reports_invalid",
                "审计摘要 reports 必须是素材映射。",
            )
        )
        return False
    for asset_id, report in reports.items():
        if not isinstance(report, dict):
            blockers.append(
                _issue(
                    f"{code_prefix}_summary_report_invalid",
                    f"素材 {asset_id} 的审计摘要必须是 object。",
                    asset_id=str(asset_id),
                )
            )
            valid = False
            continue
        if set(report) - report_fields:
            blockers.append(
                _issue(
                    f"{code_prefix}_summary_unsafe_field",
                    f"素材 {asset_id} 的审计摘要包含非白名单字段。",
                    asset_id=str(asset_id),
                )
            )
            valid = False
        if report.get("status") not in {"passed", "failed", "unknown", "missing"}:
            blockers.append(
                _issue(
                    f"{code_prefix}_summary_status_invalid",
                    f"素材 {asset_id} 的审计状态不是允许的枚举。",
                    asset_id=str(asset_id),
                )
            )
            valid = False
        checks = report.get("checks")
        if checks is not None:
            if (
                not isinstance(checks, dict)
                or set(checks) - boolean_check_fields
                or any(not isinstance(value, bool) for value in checks.values())
            ):
                blockers.append(
                    _issue(
                        f"{code_prefix}_summary_checks_invalid",
                        f"素材 {asset_id} 的 checks 只能包含白名单 boolean。",
                        asset_id=str(asset_id),
                    )
                )
                valid = False
        for key, value in report.items():
            if key.endswith("sha256") and _normalized_sha256(value) is None:
                blockers.append(
                    _issue(
                        f"{code_prefix}_summary_hash_invalid",
                        f"素材 {asset_id} 的 {key} 不是有效 SHA-256。",
                        asset_id=str(asset_id),
                    )
                )
                valid = False
    return valid


def _read_audit_summary(
    version_dir: Path,
    pointer: dict[str, Any],
    *,
    code_prefix: str,
    label: str,
    blockers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    audit_path, error = _inside_version(version_dir, pointer.get("path"))
    if error or audit_path is None or not audit_path.is_file():
        blockers.append(
            _issue(f"{code_prefix}_missing", f"{label}归档不存在。")
        )
        return None
    if sha256_file(audit_path).lower() != str(pointer.get("sha256", "")).lower():
        blockers.append(
            _issue(f"{code_prefix}_file_hash_mismatch", f"{label}文件哈希不一致。")
        )
    return _read_optional_json(audit_path, label, blockers)


def _validate_privacy_upstream(
    version_dir: Path,
    privacy: dict[str, Any],
    source_hashes: dict[str, str],
    blockers: list[dict[str, Any]],
) -> bool:
    valid = True
    required = bool(privacy.get("required", False))
    privacy_status = str(privacy.get("status", "")).strip().lower()
    if required and privacy_status not in {
        "passed",
        "pass",
        "approved",
        "ok",
        "通过",
        "已通过",
    }:
        blockers.append(
            _issue(
                "privacy_status_not_passed",
                "privacy.required=true，但 privacy.status 未明确为 passed。",
            )
        )
        valid = False
    declared_source = privacy.get("source_sha256")
    if declared_source is not None:
        if len(source_hashes) != 1:
            blockers.append(
                _issue(
                    "privacy_source_hash_ambiguous",
                    "privacy.source_sha256 仅兼容单素材。",
                )
            )
            valid = False
        elif (
            _normalized_sha256(declared_source)
            != next(iter(source_hashes.values())).lower()
        ):
            blockers.append(
                _issue(
                    "privacy_source_hash_mismatch",
                    "privacy.source_sha256 与当前待标注素材不一致。",
                )
            )
            valid = False
    upstream = privacy.get("upstream_audit")
    if not isinstance(upstream, dict):
        if required:
            blockers.append(
                _issue("privacy_audit_missing", "required=true 但缺少 upstream_audit。")
            )
            return False
        return valid
    declared = upstream.get("declared") is True
    if not declared:
        if required:
            blockers.append(
                _issue("privacy_audit_missing", "required=true 但上游隐私审计未声明。")
            )
            valid = False
        return valid
    summary = _read_audit_summary(
        version_dir,
        upstream,
        code_prefix="privacy_audit",
        label="上游隐私审计摘要",
        blockers=blockers,
    )
    if summary is None:
        return False
    if not _validate_summary_allowlist(
        summary,
        kind="privacy_redaction",
        code_prefix="privacy_audit",
        report_fields={
            "status",
            "passed",
            "source_sha256",
            "output_sha256",
            "output_hash_matched",
            "checks",
            "original_audit_sha256",
        },
        boolean_check_fields={
            "sensitive_content_absent",
            "watermark_policy_satisfied",
            "metadata_removed",
            "source_hash_matched",
        },
        blockers=blockers,
    ):
        valid = False
    if summary.get("required") is not required or upstream.get("required") is not required:
        blockers.append(
            _issue(
                "privacy_audit_contract_mismatch",
                "隐私审计摘要的 required 与 manifest 不一致。",
            )
        )
        valid = False
    if summary.get("load_complete") is not True or upstream.get("load_complete") is not True:
        blockers.append(
            _issue("privacy_audit_missing", "上游隐私审计没有完整加载。")
        )
        valid = False
    if summary.get("passed") is not True or upstream.get("passed") is not True:
        blockers.append(
            _issue("privacy_audit_not_passed", "上游隐私审计摘要未通过。")
        )
        valid = False
    reports = summary.get("reports")
    if not isinstance(reports, dict):
        return False
    if set(reports) != set(source_hashes):
        blockers.append(
            _issue(
                "privacy_audit_asset_coverage_mismatch",
                "上游隐私审计摘要必须逐一覆盖当前素材。",
            )
        )
        valid = False
    for asset_id, actual in source_hashes.items():
        report = reports.get(asset_id)
        if not isinstance(report, dict):
            blockers.append(
                _issue(
                    "privacy_audit_asset_missing",
                    f"上游隐私审计缺少素材 {asset_id} 的报告。",
                    asset_id=asset_id,
                )
            )
            valid = False
            continue
        if report.get("passed") is not True:
            blockers.append(
                _issue(
                    "privacy_audit_not_passed",
                    f"素材 {asset_id} 的上游隐私审计未通过。",
                    asset_id=asset_id,
                )
            )
            valid = False
        raw_output_hash = report.get("output_sha256")
        output_hash = _normalized_sha256(raw_output_hash)
        if raw_output_hash is None:
            blockers.append(
                _issue(
                    "privacy_audit_output_hash_missing",
                    f"素材 {asset_id} 的上游隐私审计缺少 output_sha256。",
                    asset_id=asset_id,
                )
            )
            valid = False
        elif output_hash is None:
            blockers.append(
                _issue(
                    "privacy_audit_output_hash_invalid",
                    f"素材 {asset_id} 的 output_sha256 无效。",
                    asset_id=asset_id,
                )
            )
            valid = False
        elif output_hash != actual.lower():
            blockers.append(
                _issue(
                    "privacy_audit_hash_mismatch",
                    f"素材 {asset_id} 必须绑定隐私审计的 output_sha256。",
                    asset_id=asset_id,
                )
            )
            valid = False
        expected_binding = output_hash is not None and output_hash == actual.lower()
        if report.get("output_hash_matched") is not expected_binding:
            blockers.append(
                _issue(
                    "privacy_audit_binding_state_invalid",
                    f"素材 {asset_id} 的 output_hash_matched 与实际哈希不一致。",
                    asset_id=asset_id,
                )
            )
            valid = False
    return valid


def _validate_watermark_audit(
    version_dir: Path,
    privacy: dict[str, Any],
    source_hashes: dict[str, str],
    blockers: list[dict[str, Any]],
) -> bool:
    valid = True
    watermark_policy = privacy.get("watermark")
    removal_requested = (
        privacy.get("authorized_watermark_removal") is True
        or _watermark_indicates_removed(watermark_policy)
    )
    if removal_requested and _watermark_requires_preservation(watermark_policy):
        blockers.append(
            _issue(
                "watermark_unknown_removal_forbidden",
                "未知版权水印必须保留，不能声明为已删除。",
            )
        )
        valid = False
    pointer = privacy.get("watermark_audit")
    if not isinstance(pointer, dict) or pointer.get("declared") is not True:
        if removal_requested:
            blockers.append(
                _issue(
                    "watermark_audit_missing",
                    "水印已声明删除，但缺少 watermark_audit。",
                )
            )
            return False
        return valid
    summary = _read_audit_summary(
        version_dir,
        pointer,
        code_prefix="watermark_audit",
        label="水印处理审计摘要",
        blockers=blockers,
    )
    if summary is None:
        return False
    if not _validate_summary_allowlist(
        summary,
        kind="watermark_removal",
        code_prefix="watermark_audit",
        report_fields={
            "status",
            "passed",
            "authorized",
            "deterministic",
            "outside_changed_pixels_zero",
            "source_sha256",
            "output_sha256",
            "output_hash_matched",
            "original_audit_sha256",
        },
        boolean_check_fields=set(),
        blockers=blockers,
    ):
        valid = False
    if (
        summary.get("required") is not removal_requested
        or pointer.get("required") is not removal_requested
    ):
        blockers.append(
            _issue(
                "watermark_audit_contract_mismatch",
                "水印审计摘要的 required 与删除声明不一致。",
            )
        )
        valid = False
    if summary.get("load_complete") is not True or pointer.get("load_complete") is not True:
        blockers.append(
            _issue("watermark_audit_missing", "水印处理审计没有完整加载。")
        )
        valid = False
    if summary.get("passed") is not True or pointer.get("passed") is not True:
        blockers.append(
            _issue("watermark_audit_not_passed", "水印处理审计摘要未通过。")
        )
        valid = False
    reports = summary.get("reports")
    if not isinstance(reports, dict):
        return False
    if set(reports) != set(source_hashes):
        blockers.append(
            _issue(
                "watermark_audit_asset_coverage_mismatch",
                "水印处理审计摘要必须逐一覆盖当前素材。",
            )
        )
        valid = False
    for asset_id, actual in source_hashes.items():
        report = reports.get(asset_id)
        if not isinstance(report, dict):
            blockers.append(
                _issue(
                    "watermark_audit_asset_missing",
                    f"水印处理审计缺少素材 {asset_id} 的报告。",
                    asset_id=asset_id,
                )
            )
            valid = False
            continue
        for field, code, message in (
            ("passed", "watermark_audit_not_passed", "审计未通过"),
            (
                "authorized",
                "watermark_audit_authorization_missing",
                "未确认 authorized=true",
            ),
            (
                "deterministic",
                "watermark_audit_not_deterministic",
                "未确认 deterministic=true",
            ),
            (
                "outside_changed_pixels_zero",
                "watermark_audit_outside_pixels_changed",
                "未证明水印区域外像素变化为 0",
            ),
        ):
            if report.get(field) is not True:
                blockers.append(
                    _issue(
                        code,
                        f"素材 {asset_id} 的水印处理{message}。",
                        asset_id=asset_id,
                    )
                )
                valid = False
        raw_output_hash = report.get("output_sha256")
        output_hash = _normalized_sha256(raw_output_hash)
        if raw_output_hash is None:
            blockers.append(
                _issue(
                    "watermark_audit_output_hash_missing",
                    f"素材 {asset_id} 的水印审计缺少 output_sha256。",
                    asset_id=asset_id,
                )
            )
            valid = False
        elif output_hash is None:
            blockers.append(
                _issue(
                    "watermark_audit_output_hash_invalid",
                    f"素材 {asset_id} 的水印 output_sha256 无效。",
                    asset_id=asset_id,
                )
            )
            valid = False
        elif output_hash != actual.lower():
            blockers.append(
                _issue(
                    "watermark_audit_hash_mismatch",
                    f"素材 {asset_id} 必须绑定水印审计的 output_sha256。",
                    asset_id=asset_id,
                )
            )
            valid = False
        expected_binding = output_hash is not None and output_hash == actual.lower()
        if report.get("output_hash_matched") is not expected_binding:
            blockers.append(
                _issue(
                    "watermark_audit_binding_state_invalid",
                    f"素材 {asset_id} 的水印 output_hash_matched 与实际哈希不一致。",
                    asset_id=asset_id,
                )
            )
            valid = False
    return valid


def _validate_privacy(
    version_dir: Path,
    manifest: dict[str, Any],
    source_hashes: dict[str, str],
    blockers: list[dict[str, Any]],
) -> bool:
    privacy = manifest.get("privacy")
    if not isinstance(privacy, dict):
        blockers.append(_issue("privacy_invalid", "manifest.privacy 必须是 object。"))
        return False
    upstream_valid = _validate_privacy_upstream(
        version_dir,
        privacy,
        source_hashes,
        blockers,
    )
    watermark_valid = _validate_watermark_audit(
        version_dir,
        privacy,
        source_hashes,
        blockers,
    )
    return upstream_valid and watermark_valid


def _validate_manual_gate(
    version_dir: Path,
    manifest: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    qa = manifest.get("qa", {})
    auto_path, auto_error = _inside_version(version_dir, qa.get("auto_summary"))
    manual_path, manual_error = _inside_version(
        version_dir,
        qa.get("manual_review_template"),
    )
    if auto_error or auto_path is None or not auto_path.is_file():
        blockers.append(_issue("auto_summary_missing", "缺少 qa/auto-summary.json。"))
    else:
        auto = _read_optional_json(auto_path, "自动 QA 摘要", blockers)
        if auto is not None:
            if auto.get("status") not in ALLOWED_AUTO_STATUSES:
                blockers.append(
                    _issue(
                        "auto_status_invalid",
                        "自动 QA 状态只能是 blocked 或 awaiting_manual_review。",
                    )
                )
            if auto.get("manual_review_required") is not True:
                blockers.append(
                    _issue(
                        "manual_gate_missing",
                        "自动 QA 必须明确 manual_review_required=true。",
                    )
                )
            for key in ("schema_version", "job_id", "version", "status", "deterministic_passed"):
                if auto.get(key) != manifest.get(key):
                    blockers.append(
                        _issue(
                            "auto_summary_identity_mismatch",
                            f"auto-summary 的 {key} 与 manifest 不一致。",
                        )
                    )
            if auto.get("blockers") != manifest.get("blockers"):
                blockers.append(
                    _issue(
                        "auto_summary_blockers_mismatch",
                        "auto-summary 的 blockers 与 manifest 不一致。",
                    )
                )
            if auto.get("manual_review_status") != manifest.get(
                "manual_review_status"
            ):
                blockers.append(
                    _issue(
                        "auto_summary_manual_status_mismatch",
                        "auto-summary 的 manual_review_status 与 manifest 不一致。",
                    )
                )
    if manual_error or manual_path is None or not manual_path.is_file():
        blockers.append(
            _issue("manual_review_template_missing", "缺少人工复核模板。")
        )
        return
    manual = _read_optional_json(manual_path, "人工复核模板", blockers)
    if manual is None:
        return
    expected = {
        "review_index": 1,
        "status": (
            "not_ready" if manifest.get("blockers") else "awaiting_manual_review"
        ),
        "reviewer": None,
        "reviewed_at": None,
        "decision": "unchecked",
        "notes": "unchecked",
    }
    for key, value in expected.items():
        if manual.get(key) != value:
            blockers.append(
                _issue(
                    "manual_review_template_state_invalid",
                    f"人工复核模板 {key} 必须为 {value!r}。",
                )
            )
    if manual.get("manual_review_required") is not True:
        blockers.append(
            _issue(
                "manual_review_required_missing",
                "人工复核模板必须 manual_review_required=true。",
            )
        )
    if manual.get("deterministic_ready") is not (not manifest.get("blockers")):
        blockers.append(
            _issue(
                "manual_review_template_deterministic_state_invalid",
                "人工复核模板 deterministic_ready 与 manifest.blockers 不一致。",
            )
        )
    if not isinstance(manual.get("blockers"), list):
        blockers.append(
            _issue("manual_review_blockers_invalid", "人工复核模板 blockers 必须是数组。")
        )
    for asset in manual.get("assets", []):
        if not isinstance(asset, dict):
            continue
        for field in ("decision", "notes"):
            if asset.get(field) != "unchecked":
                blockers.append(
                    _issue(
                        "manual_asset_state_invalid",
                        f"人工复核模板素材字段 {field} 必须为 unchecked。",
                    )
                )
        for annotation in asset.get("annotations", []):
            if not isinstance(annotation, dict):
                continue
            for field in ("alignment", "readability", "non_obstruction", "decision", "notes"):
                if annotation.get(field) != "unchecked":
                    blockers.append(
                        _issue(
                            "manual_annotation_state_invalid",
                            f"人工复核模板标注字段 {field} 必须为 unchecked。",
                        )
                    )


def _timezone_aware_iso8601(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_review_payload(
    review: dict[str, Any],
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    deterministic_ready: bool,
    review_number: int,
    review_name: str,
    blockers: list[dict[str, Any]],
) -> str:
    if review.get("review_index") != review_number:
        blockers.append(
            _issue(
                "review_index_mismatch",
                f"{review_name}.review_index 必须等于文件编号 {review_number}。",
            )
        )
    for key in ("schema_version", "job_id", "version"):
        if review.get(key) != manifest.get(key):
            blockers.append(
                _issue(
                    "review_identity_mismatch",
                    f"{review_name} 的 {key} 与 manifest 不一致。",
                )
            )
    if review.get("manifest_sha256") != manifest_sha256:
        blockers.append(
            _issue(
                "review_manifest_mismatch",
                f"{review_name} 没有绑定当前最终 manifest.json。",
            )
        )
    if review.get("manual_review_required") is not True:
        blockers.append(
            _issue(
                "review_manual_gate_missing",
                f"{review_name} 必须 manual_review_required=true。",
            )
        )
    if review.get("deterministic_ready") is not deterministic_ready:
        blockers.append(
            _issue(
                "review_deterministic_state_mismatch",
                f"{review_name} 的 deterministic_ready 与确定性检查不一致。",
            )
        )

    status = review.get("status")
    decision = review.get("decision")
    review_blockers = review.get("blockers")
    if not isinstance(review_blockers, list):
        blockers.append(
            _issue("review_blockers_invalid", f"{review_name}.blockers 必须是数组。")
        )
        review_blockers = []
    else:
        for index, item in enumerate(review_blockers, start=1):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("code"), str)
                or not item["code"].strip()
            ):
                blockers.append(
                    _issue(
                        "review_blocker_invalid",
                        f"{review_name}.blockers[{index}] 必须是含非空 code 的 object。",
                    )
                )
    allowed_states = {
        "awaiting_manual_review": "unchecked",
        "not_ready": "unchecked",
        "passed": "passed",
        "blocked": "blocked",
    }
    if status not in allowed_states or decision != allowed_states.get(status):
        blockers.append(
            _issue(
                "review_decision_status_invalid",
                f"{review_name} 的 status 与 decision 组合无效。",
            )
        )

    signed = status in {"passed", "blocked"}
    if signed:
        reviewer = review.get("reviewer")
        reviewer_value = reviewer.strip() if isinstance(reviewer, str) else ""
        if (
            not reviewer_value
            or reviewer_value.casefold()
            in {"unchecked", "unknown", "none", "null", "待确认", "未知"}
        ):
            blockers.append(
                _issue("reviewer_invalid", f"{review_name} 缺少有效 reviewer。")
            )
        if not _timezone_aware_iso8601(review.get("reviewed_at")):
            blockers.append(
                _issue(
                    "review_time_invalid",
                    f"{review_name}.reviewed_at 必须是带时区的 ISO 时间。",
                )
            )
        if not isinstance(review.get("notes"), str):
            blockers.append(
                _issue("review_notes_invalid", f"{review_name}.notes 必须是字符串。")
            )
    else:
        if review.get("reviewer") is not None or review.get("reviewed_at") is not None:
            blockers.append(
                _issue(
                    "review_unsigned_identity_invalid",
                    f"{review_name} 未签署时不得填写 reviewer 或 reviewed_at。",
                )
            )
        if review.get("notes") != "unchecked":
            blockers.append(
                _issue(
                    "review_unsigned_notes_invalid",
                    f"{review_name} 未签署时 notes 必须为 unchecked。",
                )
            )
    if status == "passed" and review_blockers:
        blockers.append(
            _issue(
                "review_passed_with_blockers",
                f"{review_name} 标记 passed 时 blockers 必须为空。",
            )
        )
    if status in {"awaiting_manual_review", "not_ready"} and review_blockers:
        blockers.append(
            _issue(
                "review_unsigned_with_blockers",
                f"{review_name} 未签署时 blockers 必须为空。",
            )
        )
    if status == "blocked":
        if not review_blockers:
            blockers.append(
                _issue(
                    "review_blocked_without_blocker",
                    f"{review_name} 标记 blocked 时必须记录 blocker。",
                )
            )
        blockers.append(
            _issue("manual_review_blocked", f"{review_name} 的人工复核结论为 blocked。")
        )
    if status == "awaiting_manual_review" and not deterministic_ready:
        blockers.append(
            _issue(
                "review_status_invalid",
                f"{review_name} 在确定性检查未通过时必须为 not_ready。",
            )
        )
    if status == "not_ready" and deterministic_ready:
        blockers.append(
            _issue(
                "review_status_invalid",
                f"{review_name} 在确定性检查通过后不能为 not_ready。",
            )
        )
    if status == "passed" and not deterministic_ready:
        blockers.append(
            _issue(
                "review_cannot_override_deterministic_blockers",
                f"{review_name} 不能覆盖确定性 blocker。",
            )
        )

    expected_assets: dict[str, tuple[str, set[str]]] = {}
    for asset in manifest.get("assets", []):
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("id"))
        expected_assets[asset_id] = (
            str(asset.get("render_status")),
            {
                str(annotation.get("id"))
                for annotation in asset.get("annotations", [])
                if isinstance(annotation, dict)
            },
        )
    review_assets = review.get("assets")
    seen_assets: set[str] = set()
    coverage_invalid = not isinstance(review_assets, list)
    if isinstance(review_assets, list):
        for asset in review_assets:
            if not isinstance(asset, dict):
                coverage_invalid = True
                continue
            asset_id = str(asset.get("id"))
            if asset_id in seen_assets or asset_id not in expected_assets:
                coverage_invalid = True
            seen_assets.add(asset_id)
            expected = expected_assets.get(asset_id)
            if expected is None:
                continue
            render_status, expected_annotations = expected
            if str(asset.get("render_status")) != render_status:
                coverage_invalid = True
            expected_asset_decision = "passed" if status == "passed" else "unchecked"
            if asset.get("decision") != expected_asset_decision:
                blockers.append(
                    _issue(
                        "review_asset_state_invalid",
                        (
                            f"{review_name} 的素材 {asset_id} decision "
                            f"必须为 {expected_asset_decision}。"
                        ),
                    )
                )
            if status == "passed":
                if not isinstance(asset.get("notes"), str):
                    blockers.append(
                        _issue(
                            "review_asset_notes_invalid",
                            f"{review_name} 已通过素材的 notes 必须是字符串。",
                        )
                    )
            elif asset.get("notes") != "unchecked":
                blockers.append(
                    _issue(
                        "review_asset_state_invalid",
                        f"{review_name} 未通过素材的 notes 必须为 unchecked。",
                    )
                )
            annotations = asset.get("annotations")
            if not isinstance(annotations, list):
                coverage_invalid = True
                continue
            seen_annotations: set[str] = set()
            for annotation in annotations:
                if not isinstance(annotation, dict):
                    coverage_invalid = True
                    continue
                annotation_id = str(annotation.get("id"))
                if (
                    annotation_id in seen_annotations
                    or annotation_id not in expected_annotations
                ):
                    coverage_invalid = True
                seen_annotations.add(annotation_id)
                expected_value = "passed" if status == "passed" else "unchecked"
                for field in (
                    "alignment",
                    "readability",
                    "non_obstruction",
                    "decision",
                ):
                    if annotation.get(field) != expected_value:
                        blockers.append(
                            _issue(
                                "review_annotation_state_invalid",
                                (
                                    f"{review_name} 的标注 {asset_id}/{annotation_id} "
                                    f"字段 {field} 必须为 {expected_value}。"
                                ),
                            )
                        )
                if status == "passed":
                    if not isinstance(annotation.get("notes"), str):
                        blockers.append(
                            _issue(
                                "review_annotation_notes_invalid",
                                f"{review_name} 已通过标注的 notes 必须是字符串。",
                            )
                        )
                elif annotation.get("notes") != "unchecked":
                    blockers.append(
                        _issue(
                            "review_annotation_state_invalid",
                            f"{review_name} 未通过标注的 notes 必须为 unchecked。",
                        )
                    )
            if seen_annotations != expected_annotations:
                coverage_invalid = True
    if seen_assets != set(expected_assets):
        coverage_invalid = True
    if coverage_invalid:
        blockers.append(
            _issue(
                "review_annotation_coverage_mismatch",
                f"{review_name} 必须完整且无重复地覆盖所有素材与标注。",
            )
        )
    return str(status)


def _validate_review_workspace(
    version_dir: Path,
    manifest: dict[str, Any],
    deterministic_ready: bool,
    blockers: list[dict[str, Any]],
) -> str:
    expected_pointer = f"reviews/{version_dir.name}"
    files = manifest.get("files")
    pointer = files.get("review_workspace") if isinstance(files, dict) else None
    if pointer != expected_pointer:
        blockers.append(
            _issue(
                "review_workspace_path_invalid",
                f"review_workspace 必须精确为 {expected_pointer!r}。",
            )
        )
    if version_dir.parent.name != "versions":
        blockers.append(
            _issue(
                "review_workspace_path_invalid",
                "版本目录必须位于项目的 versions 目录下。",
            )
        )
        return "not_ready"
    project_root = version_dir.parent.parent
    reviews_root = project_root / "reviews"
    workspace = reviews_root / version_dir.name
    if reviews_root.is_symlink() or workspace.is_symlink():
        blockers.append(
            _issue("review_workspace_symlink", "人工复核目录不得是符号链接。")
        )
        return "not_ready"
    if not workspace.is_dir():
        blockers.append(
            _issue("review_workspace_missing", f"人工复核目录不存在：{workspace}")
        )
        return "not_ready"
    try:
        common = os.path.commonpath((str(project_root.resolve()), str(workspace.resolve())))
    except ValueError:
        common = ""
    if common != str(project_root.resolve()):
        blockers.append(
            _issue("review_workspace_path_invalid", "人工复核目录越出项目范围。")
        )
        return "not_ready"

    numbered: dict[int, Path] = {}
    for path in workspace.iterdir():
        if path.is_symlink():
            blockers.append(
                _issue("review_symlink", f"人工复核文件不得是符号链接：{path.name}")
            )
            continue
        match = REVIEW_FILE_PATTERN.fullmatch(path.name)
        if not path.is_file() or match is None:
            blockers.append(
                _issue(
                    "review_workspace_unexpected_entry",
                    f"人工复核目录含非 review-NNN.json 项：{path.name}",
                )
            )
            continue
        numbered[int(match.group(1))] = path
    if not numbered:
        blockers.append(_issue("review_missing", "缺少 review-001.json。"))
        return "not_ready"
    expected_numbers = set(range(1, max(numbered) + 1))
    if set(numbered) != expected_numbers:
        blockers.append(
            _issue(
                "review_sequence_gap",
                "人工复核文件必须从 review-001.json 开始连续编号。",
            )
        )

    manifest_digest = sha256_file(version_dir / "manifest.json")
    latest_status = "not_ready"
    for number in sorted(numbered):
        path = numbered[number]
        review = _read_optional_json(path, path.name, blockers)
        if review is None:
            continue
        latest_status = _validate_review_payload(
            review,
            manifest=manifest,
            manifest_sha256=manifest_digest,
            deterministic_ready=deterministic_ready,
            review_number=number,
            review_name=path.name,
            blockers=blockers,
        )
    return latest_status


def validate_project(version_dir: str | Path) -> dict[str, Any]:
    """重新读取版本目录，返回不写盘的确定性校验摘要。"""

    root = Path(version_dir).expanduser().resolve()
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not root.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "passed": False,
            "deterministic_passed": False,
            "manual_review_required": True,
            "manual_review_status": "not_ready",
            "privacy_audit_passed": False,
            "blockers": [
                _issue("version_directory_missing", f"版本目录不存在：{root}")
            ],
            "warnings": [],
            "checks": {},
        }
    if not VERSION_PATTERN.fullmatch(root.name):
        blockers.append(
            _issue(
                "version_directory_name_invalid",
                "版本目录名必须是 v001 到 v999。",
            )
        )
    manifest = _read_optional_json(root / "manifest.json", "版本 manifest", blockers)
    resolved = _read_optional_json(
        root / "plans" / "resolved-plan.json",
        "resolved plan",
        blockers,
    )
    if manifest is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "blocked",
            "passed": False,
            "deterministic_passed": False,
            "manual_review_required": True,
            "manual_review_status": "not_ready",
            "privacy_audit_passed": False,
            "blockers": blockers,
            "warnings": warnings,
            "checks": {},
        }
    if manifest.get("schema_version") != SCHEMA_VERSION:
        blockers.append(
            _issue("schema_version_invalid", "manifest.schema_version 必须是 '2.0'。")
        )
    if manifest.get("version") != root.name:
        blockers.append(
            _issue("version_mismatch", "manifest.version 与版本目录名不一致。")
        )
    if manifest.get("status") not in ALLOWED_AUTO_STATUSES:
        blockers.append(
            _issue(
                "manifest_status_invalid",
                "manifest.status 只能是 blocked 或 awaiting_manual_review。",
            )
        )
    recorded_blockers = manifest.get("blockers")
    if not isinstance(recorded_blockers, list):
        blockers.append(
            _issue("manifest_blockers_invalid", "manifest.blockers 必须是数组。")
        )
        recorded_blockers = []
    else:
        for index, recorded in enumerate(recorded_blockers, start=1):
            if (
                isinstance(recorded, dict)
                and isinstance(recorded.get("code"), str)
                and recorded["code"].strip()
            ):
                if recorded not in blockers:
                    blockers.append(dict(recorded))
            else:
                blockers.append(
                    _issue(
                        "manifest_recorded_blocker_invalid",
                        f"manifest.blockers[{index}] 必须是含非空 code 的 object。",
                    )
                )
    expected_recorded_status = (
        "blocked" if recorded_blockers else "awaiting_manual_review"
    )
    if manifest.get("status") != expected_recorded_status:
        blockers.append(
            _issue(
                "manifest_status_inconsistent",
                "manifest.status 与记录的 blockers 不一致。",
            )
        )
    if manifest.get("deterministic_passed") is not (not recorded_blockers):
        blockers.append(
            _issue(
                "manifest_deterministic_state_inconsistent",
                "manifest.deterministic_passed 与 blockers 不一致。",
            )
        )
    if manifest.get("manual_review_required") is not True:
        blockers.append(
            _issue(
                "manifest_manual_gate_missing",
                "manifest 必须 manual_review_required=true。",
            )
        )
    expected_manual_status = (
        "not_ready" if recorded_blockers else "awaiting_manual_review"
    )
    if manifest.get("manual_review_status") != expected_manual_status:
        blockers.append(
            _issue(
                "manifest_manual_status_inconsistent",
                "manifest.manual_review_status 与 blockers 不一致。",
            )
        )
    style = manifest.get("style")
    if not isinstance(style, dict) or style.get("preset") not in V2_STYLE_PRESETS:
        blockers.append(
            _issue(
                "style_preset_invalid",
                "v2 manifest.style.preset 必须是 action-orange-v2 或 action-red-v2。",
            )
        )
    if resolved is not None:
        for key in (
            "schema_version",
            "job_id",
            "version",
            "status",
            "style",
            "privacy",
            "flow",
            "overview",
            "manual_review_status",
        ):
            if resolved.get(key) != manifest.get(key):
                blockers.append(
                    _issue(
                        "resolved_plan_identity_mismatch",
                        f"resolved plan 的 {key} 与 manifest 不一致。",
                    )
                )
        if resolved.get("assets") != manifest.get("assets"):
            blockers.append(
                _issue(
                    "resolved_plan_assets_mismatch",
                    "resolved plan 与 manifest 的 assets 不一致。",
                )
            )
    inventory = _validate_inventory(root, manifest, blockers)
    image_sizes, source_hashes = _validate_image_assets(
        root,
        manifest.get("assets"),
        style if isinstance(style, dict) else {},
        blockers,
        warnings,
    )
    _validate_job_markers_and_copy(manifest, blockers, warnings)
    _validate_flow(manifest, blockers)
    _validate_overview(
        root,
        manifest,
        image_sizes,
        manifest.get("assets"),
        blockers,
    )
    _validate_qa_images(root, manifest, image_sizes, blockers)
    privacy_passed = _validate_privacy(root, manifest, source_hashes, blockers)
    _validate_manual_gate(root, manifest, blockers)
    deterministic_ready = not blockers
    review_blocker_start = len(blockers)
    reviewed_status = _validate_review_workspace(
        root,
        manifest,
        deterministic_ready,
        blockers,
    )
    review_has_blockers = len(blockers) > review_blocker_start

    if not deterministic_ready:
        status = "blocked"
        manual_review_status = "not_ready"
    elif review_has_blockers or reviewed_status == "blocked":
        status = "blocked"
        manual_review_status = "blocked"
    elif reviewed_status == "passed":
        status = "passed"
        manual_review_status = "passed"
    else:
        status = "awaiting_manual_review"
        manual_review_status = "awaiting_manual_review"
    checks = {
        "file_collection_and_hashes": not any(
            issue["code"].startswith("file_")
            or issue["code"] in {"expected_file_missing", "unexpected_file"}
            for issue in blockers
        ),
        "images_dimensions_and_metadata": not any(
            "dimensions" in issue["code"]
            or "metadata" in issue["code"]
            or "unreadable" in issue["code"]
            for issue in blockers
        ),
        "geometry_and_collisions": not any(
            "rect" in issue["code"]
            or "collision" in issue["code"]
            or "anchor" in issue["code"]
            or issue["code"].startswith("target_contract_")
            for issue in blockers
        ),
        "marker_and_copy": not any(
            "marker" in issue["code"] or "copy" in issue["code"]
            for issue in blockers
        ),
        "overview": not any(issue["code"].startswith("overview_") for issue in blockers),
        "privacy_upstream_audit": privacy_passed,
        "manual_review_required": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": manifest.get("job_id"),
        "version": manifest.get("version"),
        "status": status,
        "passed": status == "passed",
        "deterministic_passed": deterministic_ready,
        "manual_review_required": True,
        "manual_review_status": manual_review_status,
        "privacy_audit_passed": privacy_passed,
        "checked_file_count": len(inventory) + 1,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
    }
