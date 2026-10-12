"""截图标注 v2 批量作业引擎。"""

from __future__ import annotations

import os
import re
import shutil
import uuid
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .common import (
    ANCHORS,
    SCHEMA_VERSION,
    STYLE_PRESET,
    file_record,
    lint_callout_text,
    lint_copy_for_kind,
    normalize_rect,
    normalize_semantic_role,
    normalize_style,
    read_json,
    rect_inside,
    rect_intersection_area,
    rect_to_list,
    resolve_marker,
    portable_name_key,
    safe_component,
    safe_output_name,
    sha256_file,
    write_json,
)
from .rendering import (
    build_annotation_crops,
    build_contact_sheet,
    build_overview,
    render_annotated_asset,
    render_coordinate_grid,
)


VERSION_PATTERN = re.compile(r"^v(\d{3})$")
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


def _issue(
    code: str,
    message: str,
    *,
    asset_id: str | None = None,
    annotation_id: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if asset_id is not None:
        result["asset_id"] = asset_id
    if annotation_id is not None:
        result["annotation_id"] = annotation_id
    result.update(details)
    return result


def _normalize_version(version: str | int | None, output_root: Path) -> str:
    if version is None:
        highest = 0
        if output_root.exists():
            for child in output_root.iterdir():
                match = VERSION_PATTERN.fullmatch(child.name)
                if match and child.is_dir():
                    highest = max(highest, int(match.group(1)))
        if highest >= 999:
            raise ValueError("版本目录已达到 v999，无法继续自动编号。")
        return f"v{highest + 1:03d}"
    if isinstance(version, int):
        if not 1 <= version <= 999:
            raise ValueError("version 整数必须在 1 到 999 之间。")
        return f"v{version:03d}"
    text = str(version).strip()
    if text.isdigit():
        value = int(text)
        if 1 <= value <= 999:
            return f"v{value:03d}"
    if not VERSION_PATTERN.fullmatch(text):
        raise ValueError("version 必须是 v001 到 v999。")
    return text


def _normalize_overview(raw: Any) -> dict[str, Any]:
    if raw is None:
        result: dict[str, Any] = {"mode": "auto"}
    elif isinstance(raw, str):
        result = {"mode": raw}
    elif isinstance(raw, dict):
        result = dict(raw)
    else:
        raise ValueError("overview 必须是布局字符串或 JSON object。")
    mode = str(result.get("mode", "auto"))
    if mode not in {"auto", "vertical", "grid"}:
        raise ValueError("overview.mode 必须是 auto、vertical 或 grid。")
    result["mode"] = mode
    if "columns" in result and result["columns"] is not None:
        columns = int(result["columns"])
        if not 4 <= columns <= 7:
            raise ValueError("overview.columns 必须在 4 到 7 之间。")
        result["columns"] = columns
    if mode == "grid" and "columns" not in result:
        result["columns"] = 4
    maximum_pixels = int(result.get("max_pixels", 40_000_000))
    if maximum_pixels < 250_000:
        raise ValueError("overview.max_pixels 不得小于 250000。")
    result["max_pixels"] = maximum_pixels
    maximum_height = int(result.get("max_page_height", 12_000))
    if maximum_height < 200:
        raise ValueError("overview.max_page_height 不得小于 200。")
    result["max_page_height"] = maximum_height
    minimum_label_font = float(result.get("min_label_font_px", 14))
    if not 8 <= minimum_label_font <= 96:
        raise ValueError("overview.min_label_font_px 必须在 8 到 96 之间。")
    result["min_label_font_px"] = minimum_label_font
    return result


def _normalize_qa(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {
            "coordinate_grids": {"enabled": False, "assets": [], "step": 100},
            "crop_padding": 90,
        }
    if not isinstance(raw, dict):
        raise ValueError("qa 必须是 JSON object。")
    result = dict(raw)
    grids = result.get("coordinate_grids", False)
    if isinstance(grids, bool):
        grid_config = {
            "enabled": grids,
            "assets": "all" if grids else [],
            "step": 100,
        }
    elif isinstance(grids, list):
        grid_config = {
            "enabled": True,
            "assets": [str(value) for value in grids],
            "step": 100,
        }
    elif isinstance(grids, dict):
        grid_config = dict(grids)
        grid_config["enabled"] = bool(grid_config.get("enabled", True))
        assets = grid_config.get("assets", "all")
        if assets != "all" and not isinstance(assets, list):
            raise ValueError("qa.coordinate_grids.assets 必须是 'all' 或素材 ID 数组。")
        if isinstance(assets, list):
            assets = [str(value) for value in assets]
        grid_config["assets"] = assets
        grid_config["step"] = int(grid_config.get("step", 100))
    else:
        raise ValueError(
            "qa.coordinate_grids 必须是 boolean、素材 ID 数组或 JSON object。"
        )
    if not 10 <= int(grid_config["step"]) <= 2000:
        raise ValueError("qa.coordinate_grids.step 必须在 10 到 2000 之间。")
    result["coordinate_grids"] = grid_config
    crop_padding = int(result.get("crop_padding", 90))
    if not 0 <= crop_padding <= 2000:
        raise ValueError("qa.crop_padding 必须在 0 到 2000 之间。")
    result["crop_padding"] = crop_padding
    return result


def _normalize_protected_regions(raw: Any, asset_id: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"素材 {asset_id} 的 protected_regions 必须是数组。")
    result = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            region_id = str(item.get("id", f"protected-{index}"))
            rect_raw = item.get("rect")
            reason = item.get("reason")
        else:
            region_id = f"protected-{index}"
            rect_raw = item
            reason = None
        region_key = portable_name_key(region_id)
        if region_key in seen:
            raise ValueError(f"素材 {asset_id} 的保护区域 ID 重复：{region_id}")
        seen.add(region_key)
        rect = normalize_rect(
            rect_raw,
            f"素材 {asset_id} 的保护区域 {region_id}.rect",
        )
        result.append(
            {
                "id": region_id,
                "rect": rect,
                "reason": str(reason) if reason is not None else None,
            }
        )
    return result


def _normalize_annotations(
    raw: Any,
    asset_id: str,
    asset_kind: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError(f"素材 {asset_id} 的 annotations 必须是数组。")
    result: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    number_index = 1
    letter_index = 1
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"素材 {asset_id} 的第 {index} 个 annotation 必须是 object。")
        annotation_id = safe_component(
            item.get("id", index),
            f"素材 {asset_id} 的第 {index} 个 annotation.id",
        )
        annotation_key = portable_name_key(annotation_id)
        if annotation_key in seen:
            raise ValueError(f"素材 {asset_id} 的 annotation ID 重复：{annotation_id}")
        seen.add(annotation_key)
        rect = normalize_rect(
            item.get("rect"),
            f"素材 {asset_id} 的标注 {annotation_id}.rect",
        )
        role = normalize_semantic_role(item.get("semantic_role"))
        raw_callout = item.get("callout")
        if not isinstance(raw_callout, dict):
            raise ValueError(
                f"素材 {asset_id} 的标注 {annotation_id}.callout 必须是 object。"
            )
        text, text_blockers, text_warnings = lint_callout_text(raw_callout.get("text"))
        for issue in text_blockers:
            blockers.append(
                _issue(
                    issue["code"],
                    f"素材 {asset_id} 的标注 {annotation_id}：{issue['message']}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        for issue in text_warnings:
            warnings.append(
                _issue(
                    issue["code"],
                    f"素材 {asset_id} 的标注 {annotation_id}：{issue['message']}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        kind_blockers, kind_warnings = lint_copy_for_kind(text, asset_kind)
        for issue in kind_blockers:
            blockers.append(
                _issue(
                    issue["code"],
                    f"素材 {asset_id} 的标注 {annotation_id}：{issue['message']}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        for issue in kind_warnings:
            warnings.append(
                _issue(
                    issue["code"],
                    f"素材 {asset_id} 的标注 {annotation_id}：{issue['message']}",
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        marker_requested = raw_callout.get("marker", item.get("marker", "auto"))
        resolved_marker, number_index, letter_index = resolve_marker(
            marker_requested,
            role,
            number_index,
            letter_index,
        )
        anchor = str(raw_callout.get("anchor", item.get("anchor", "auto")))
        if anchor not in ANCHORS:
            raise ValueError(
                f"素材 {asset_id} 的标注 {annotation_id}.callout.anchor 不受支持：{anchor}"
            )
        label_rect_raw = raw_callout.get("label_rect", item.get("label_rect"))
        if anchor == "manual":
            label_rect = normalize_rect(
                label_rect_raw,
                f"素材 {asset_id} 的标注 {annotation_id}.callout.label_rect",
            )
        elif label_rect_raw is not None:
            label_rect = normalize_rect(
                label_rect_raw,
                f"素材 {asset_id} 的标注 {annotation_id}.callout.label_rect",
            )
            warnings.append(
                _issue(
                    "unused_label_rect",
                    (
                        f"素材 {asset_id} 的标注 {annotation_id} 不是 manual 锚点，"
                        "输入 label_rect 不参与自动布局。"
                    ),
                    asset_id=asset_id,
                    annotation_id=annotation_id,
                )
            )
        else:
            label_rect = None
        target_contract = _normalize_target_contract(
            item.get("target_contract"), asset_id, annotation_id
        )
        result.append(
            {
                "id": annotation_id,
                "asset_id": asset_id,
                "rect": rect,
                "semantic_role": role,
                "callout": {
                    "text": text,
                    "marker": resolved_marker,
                    "requested_marker": str(marker_requested),
                    "resolved_marker": resolved_marker,
                    "anchor": anchor,
                    "label_rect": label_rect,
                },
                "target_contract": target_contract,
            }
        )
    return result, blockers, warnings


def _normalize_target_contract(raw: Any, asset_id: str, annotation_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"素材 {asset_id} 的标注 {annotation_id}.target_contract 必须是 object。"
        )
    target_kind = str(raw.get("target_kind", "custom")).strip().lower()
    if target_kind not in TARGET_KINDS:
        raise ValueError(
            f"素材 {asset_id} 的标注 {annotation_id}.target_kind 不受支持：{target_kind}"
        )
    tolerance = raw.get("boundary_tolerance_px", 0)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 100:
        raise ValueError(
            f"素材 {asset_id} 的标注 {annotation_id}.boundary_tolerance_px 必须是 0 到 100 的整数。"
        )

    def normalize_items(value: Any, field: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(
                f"素材 {asset_id} 的标注 {annotation_id}.target_contract.{field} 必须是数组。"
            )
        result = []
        seen: set[str] = set()
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise ValueError(
                    f"素材 {asset_id} 的标注 {annotation_id}.{field}[{index}] 必须是 object。"
                )
            item_id = safe_component(
                item.get("id", f"{field}-{index}"),
                f"素材 {asset_id} 的标注 {annotation_id}.{field}[{index}].id",
            )
            key = portable_name_key(item_id)
            if key in seen:
                raise ValueError(
                    f"素材 {asset_id} 的标注 {annotation_id}.{field} ID 重复：{item_id}"
                )
            seen.add(key)
            result.append(
                {
                    "id": item_id,
                    "role": str(item.get("role", "unspecified")).strip().lower(),
                    "rect": normalize_rect(
                        item.get("rect", item.get("box")),
                        f"素材 {asset_id} 的标注 {annotation_id}.{field}/{item_id}.rect",
                    ),
                }
            )
        return result

    include = normalize_items(raw.get("must_include"), "must_include")
    exclude = normalize_items(raw.get("must_exclude"), "must_exclude")
    required = TARGET_KIND_REQUIRED_ROLES.get(target_kind, set())
    present = {item["role"] for item in include}
    missing = required - present
    if missing:
        raise ValueError(
            f"素材 {asset_id} 的标注 {annotation_id} 场景 {target_kind} 缺少 must_include 角色："
            + "、".join(sorted(missing))
        )
    return {
        "target_kind": target_kind,
        "boundary_tolerance_px": tolerance,
        "must_include": include,
        "must_exclude": exclude,
    }


def _evaluate_target_contract(
    asset_id: str,
    annotation: dict[str, Any],
    width: int,
    height: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    contract = annotation.get("target_contract")
    if not isinstance(contract, dict):
        return None, []
    blockers: list[dict[str, Any]] = []
    target = tuple(annotation["rect"])
    tolerance = int(contract.get("boundary_tolerance_px", 0))
    expanded = (
        max(0, target[0] - tolerance),
        max(0, target[1] - tolerance),
        min(width, target[2] + tolerance),
        min(height, target[3] + tolerance),
    )
    include_results = []
    for item in contract.get("must_include", []):
        rect = tuple(item["rect"])
        area = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
        covered = rect_intersection_area(expanded, rect)
        passed = rect_inside(rect, width, height) and covered == area
        include_results.append(
            {
                "id": item["id"],
                "role": item["role"],
                "covered_area": covered,
                "required_area": area,
                "coverage_ratio": round(covered / area, 6),
                "passed": passed,
            }
        )
        if not passed:
            blockers.append(
                _issue(
                    "target_contract_include_incomplete",
                    f"素材 {asset_id} 的标注 {annotation['id']} 未完整覆盖必要目标 {item['id']}。",
                    asset_id=asset_id,
                    annotation_id=annotation["id"],
                    target_kind=contract["target_kind"],
                    include_id=item["id"],
                    coverage_ratio=round(covered / area, 6),
                )
            )
    exclude_results = []
    for item in contract.get("must_exclude", []):
        rect = tuple(item["rect"])
        overlap = rect_intersection_area(target, rect)
        passed = rect_inside(rect, width, height) and overlap == 0
        exclude_results.append(
            {
                "id": item["id"],
                "role": item["role"],
                "overlap_area": overlap,
                "passed": passed,
            }
        )
        if not passed:
            blockers.append(
                _issue(
                    "target_contract_exclude_overlap",
                    f"素材 {asset_id} 的标注 {annotation['id']} 误包含排除目标 {item['id']}。",
                    asset_id=asset_id,
                    annotation_id=annotation["id"],
                    target_kind=contract["target_kind"],
                    exclude_id=item["id"],
                    overlap_area=overlap,
                )
            )
    result = {
        **contract,
        "must_include": [
            {**item, "rect": rect_to_list(tuple(item["rect"]))}
            for item in contract.get("must_include", [])
        ],
        "must_exclude": [
            {**item, "rect": rect_to_list(tuple(item["rect"]))}
            for item in contract.get("must_exclude", [])
        ],
        "checks": {
            "must_include": include_results,
            "must_exclude": exclude_results,
            "passed": not blockers,
        },
    }
    return result, blockers


def _normalize_flow(
    raw: Any,
    declared_asset_order: list[str],
    annotation_roles: dict[str, dict[str, str]],
    asset_sections: dict[str, str | None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    asset_ids = set(declared_asset_order)
    asset_sections = asset_sections or {}
    if raw is None:
        flow: dict[str, Any] = {
            "type": "linear",
            "sort": "sequence",
            "numbering_scope": "job",
            "order": list(declared_asset_order),
            "edges": [],
        }
    elif isinstance(raw, dict):
        flow = dict(raw)
    else:
        raise ValueError("flow 必须是 JSON object。")
    raw_edges = flow.get("edges", [])
    if not isinstance(raw_edges, list):
        raise ValueError("flow.edges 必须是数组。")
    edges = []
    blockers = []
    numbering_scope = str(flow.get("numbering_scope", "job")).strip().lower()
    if numbering_scope not in {"job", "section"}:
        blockers.append(
            _issue(
                "flow_numbering_scope_invalid",
                "flow.numbering_scope 必须是 job 或 section。",
            )
        )
        numbering_scope = "job"
    if numbering_scope == "section":
        for asset_id in declared_asset_order:
            if not asset_sections.get(asset_id):
                blockers.append(
                    _issue(
                        "section_id_required",
                        f"按章节编号时，素材 {asset_id} 必须提供非空 section_id。",
                        asset_id=asset_id,
                    )
                )
    flow["numbering_scope"] = numbering_scope
    raw_order = flow.get("order", declared_asset_order)
    if not isinstance(raw_order, list):
        blockers.append(
            _issue("flow_order_invalid", "flow.order 必须是素材 ID 数组。")
        )
        normalized_order = list(declared_asset_order)
    else:
        normalized_order = [str(value) for value in raw_order]
        if len(normalized_order) != len(set(normalized_order)):
            blockers.append(
                _issue("flow_order_duplicate", "flow.order 不能包含重复素材 ID。")
            )
        unknown = set(normalized_order) - asset_ids
        missing = asset_ids - set(normalized_order)
        if unknown:
            blockers.append(
                _issue(
                    "flow_order_unknown_asset",
                    "flow.order 引用了不存在的素材：" + "、".join(sorted(unknown)),
                )
            )
        if missing:
            blockers.append(
                _issue(
                    "flow_order_missing_asset",
                    "flow.order 缺少素材：" + "、".join(sorted(missing)),
                )
            )
    flow["order"] = normalized_order
    seen_edges: set[tuple[str, str, str, str]] = set()
    callout_targets: dict[tuple[str, str], tuple[str, str]] = {}
    graph: dict[str, set[str]] = {asset_id: set() for asset_id in asset_ids}
    for index, item in enumerate(raw_edges, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"flow.edges[{index}] 必须是 object。")
        from_id = str(item.get("from", item.get("from_asset_id", ""))).strip()
        to_id = str(item.get("to", item.get("to_asset_id", ""))).strip()
        kind = str(item.get("kind", "next"))
        callout_id_raw = item.get("callout_id")
        callout_id = (
            str(callout_id_raw).strip()
            if callout_id_raw is not None
            else ""
        )
        edge = {
            "from": from_id,
            "to": to_id,
            "kind": kind,
            "callout_id": callout_id,
        }
        edges.append(edge)
        if from_id not in asset_ids:
            blockers.append(
                _issue(
                    "flow_from_missing",
                    f"流程边 {index} 引用不存在的起点素材：{from_id!r}。",
                )
            )
        if to_id not in asset_ids:
            blockers.append(
                _issue(
                    "flow_to_missing",
                    f"流程边 {index} 引用不存在的终点素材：{to_id!r}。",
                )
            )
        if kind not in {"next", "alternative"}:
            blockers.append(
                _issue(
                    "flow_kind_invalid",
                    f"流程边 {index} 的 kind 必须是 next 或 alternative。",
                )
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
                    f"流程边 {index} 引用不存在的标注：{from_id}/{callout_id}。",
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
                _issue(
                    "flow_edge_duplicate",
                    f"流程边 {index} 与前面的边完全重复。",
                )
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
                        (
                            f"标注 {from_id}/{callout_id} 不能同时连接多个流程目标。"
                        ),
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
        cyclic_assets = sorted(
            asset_id for asset_id, degree in indegree.items() if degree > 0
        )
        blockers.append(
            _issue(
                "flow_cycle",
                "流程图必须是 DAG，检测到环路：" + "、".join(cyclic_assets),
            )
        )
    flow["edges"] = edges
    return flow, blockers


def _effective_flow_order(
    flow: dict[str, Any],
    declared_asset_order: list[str],
) -> list[str]:
    declared = set(declared_asset_order)
    raw_order = flow.get("order")
    result: list[str] = []
    if isinstance(raw_order, list):
        for value in raw_order:
            asset_id = str(value)
            if asset_id in declared and asset_id not in result:
                result.append(asset_id)
    result.extend(asset_id for asset_id in declared_asset_order if asset_id not in result)
    return result


def _apply_flow_marker_semantics(
    prepared_assets: list[dict[str, Any]],
    flow: dict[str, Any],
) -> None:
    asset_map = {asset["id"]: asset for asset in prepared_assets}
    order = _effective_flow_order(flow, [asset["id"] for asset in prepared_assets])
    numbering_scope = str(flow.get("numbering_scope", "job"))
    counters: dict[str, tuple[int, int]] = {"__job__": (1, 1)}
    for asset_id in order:
        asset = asset_map[asset_id]
        counter_key = (
            str(asset.get("section_id") or "")
            if numbering_scope == "section"
            else "__job__"
        )
        number_index, letter_index = counters.setdefault(counter_key, (1, 1))
        for annotation in asset["annotations"]:
            callout = annotation["callout"]
            resolved, number_index, letter_index = resolve_marker(
                callout["requested_marker"],
                annotation["semantic_role"],
                number_index,
                letter_index,
            )
            callout["marker"] = resolved
            callout["resolved_marker"] = resolved
        counters[counter_key] = (number_index, letter_index)


def _audit_passed(payload: dict[str, Any]) -> bool:
    if payload.get("passed") is True or payload.get("deterministic_passed") is True:
        return True
    status = str(payload.get("status", payload.get("result", ""))).strip().lower()
    return status in {"passed", "pass", "approved", "ok", "通过", "已通过"}


def _audit_status(payload: dict[str, Any]) -> str:
    if _audit_passed(payload):
        return "passed"
    if payload.get("passed") is False or payload.get("deterministic_passed") is False:
        return "failed"
    status = str(payload.get("status", payload.get("result", ""))).strip().lower()
    if status in {
        "failed",
        "fail",
        "blocked",
        "rejected",
        "denied",
        "失败",
        "未通过",
        "阻断",
    }:
        return "failed"
    return "unknown"


def _normalized_sha256(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return None


def _canonical_payload_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_audit_declaration(
    declaration: Any,
    plan_dir: Path,
    *,
    label: str,
    code_prefix: str,
    asset_id: str | None,
    blockers: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    external_path: Path | None = None
    if isinstance(declaration, str):
        external_path = Path(declaration)
        payload: dict[str, Any] | None = None
    elif isinstance(declaration, dict):
        path_value = declaration.get("path", declaration.get("file"))
        external_path = Path(str(path_value)) if path_value else None
        payload = dict(declaration) if external_path is None else None
    else:
        blockers.append(
            _issue(
                f"{code_prefix}_invalid",
                f"{label}必须是 JSON object 或审计文件路径。",
                asset_id=asset_id,
            )
        )
        return None, None
    if external_path is not None:
        if not external_path.is_absolute():
            external_path = (plan_dir / external_path).resolve()
        if not external_path.is_file():
            blockers.append(
                _issue(
                    f"{code_prefix}_missing",
                    f"{label}文件不存在。",
                    asset_id=asset_id,
                )
            )
            return None, None
        try:
            payload = read_json(external_path, label)
        except ValueError:
            blockers.append(
                _issue(
                    f"{code_prefix}_unreadable",
                    f"{label}文件无法读取。",
                    asset_id=asset_id,
                )
            )
            return None, None
        return payload, sha256_file(external_path)
    assert payload is not None
    return payload, _canonical_payload_sha256(payload)


def _collect_audit_declarations(
    privacy: dict[str, Any],
    *,
    singular_key: str,
    plural_key: str,
    asset_ids: set[str],
    code_prefix: str,
    blockers: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any], Any | None]:
    singular_present = (
        singular_key in privacy and privacy.get(singular_key) is not None
    )
    plural_present = plural_key in privacy and privacy.get(plural_key) is not None
    if singular_present and plural_present:
        blockers.append(
            _issue(
                f"{code_prefix}_declaration_conflict",
                f"privacy.{singular_key} 与 privacy.{plural_key} 不能同时声明。",
            )
        )
    if plural_present:
        raw_reports = privacy.get(plural_key)
    elif (
        singular_present
        and isinstance(privacy.get(singular_key), dict)
        and "reports" in privacy[singular_key]
    ):
        raw_reports = privacy[singular_key].get("reports")
    else:
        return singular_present, {}, privacy.get(singular_key)
    if not isinstance(raw_reports, dict) or not raw_reports:
        blockers.append(
            _issue(
                f"{code_prefix}_invalid",
                f"privacy.{plural_key}（或 {singular_key}.reports）必须是非空素材映射。",
            )
        )
        return True, {}, None
    reports = {str(key): value for key, value in raw_reports.items()}
    for unknown_id in sorted(set(reports) - asset_ids):
        blockers.append(
            _issue(
                f"{code_prefix}_unknown_asset",
                f"审计声明引用了未知素材 {unknown_id}。",
                asset_id=unknown_id,
            )
        )
    return True, reports, None


def _asset_report_payload(
    payload: dict[str, Any],
    asset_id: str,
    *,
    combined: bool,
) -> tuple[dict[str, Any], bool]:
    if combined:
        reports = payload.get("reports")
        if isinstance(reports, dict) and isinstance(reports.get(asset_id), dict):
            return reports[asset_id], True
    return payload, not combined


def _mapped_sha256(
    payload: dict[str, Any],
    asset_id: str,
    *,
    mapping_keys: tuple[str, ...],
    digest_keys: tuple[str, ...],
) -> Any:
    for mapping_key in mapping_keys:
        mapping = payload.get(mapping_key)
        if not isinstance(mapping, dict) or asset_id not in mapping:
            continue
        value = mapping[asset_id]
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for digest_key in digest_keys:
                if value.get(digest_key) is not None:
                    return value[digest_key]
    assets = payload.get("assets")
    if isinstance(assets, list):
        for item in assets:
            if not isinstance(item, dict):
                continue
            key = item.get("id", item.get("asset_id"))
            if str(key) != asset_id:
                continue
            for digest_key in digest_keys:
                if item.get(digest_key) is not None:
                    return item[digest_key]
    return None


def _audit_output_sha256(
    payload: dict[str, Any],
    asset_id: str,
    *,
    allow_top_level: bool,
) -> Any:
    mapped = _mapped_sha256(
        payload,
        asset_id,
        mapping_keys=("output_hashes", "outputs"),
        digest_keys=("output_sha256", "sha256"),
    )
    if mapped is not None:
        return mapped
    if allow_top_level:
        if payload.get("output_sha256") is not None:
            return payload.get("output_sha256")
        output = payload.get("output")
        if isinstance(output, dict):
            return output.get("output_sha256", output.get("sha256"))
    return None


def _audit_source_lineage_sha256(
    payload: dict[str, Any],
    asset_id: str,
    *,
    allow_top_level: bool,
) -> Any:
    mapped = _mapped_sha256(
        payload,
        asset_id,
        mapping_keys=("source_hashes", "sources"),
        digest_keys=("source_sha256", "sha256"),
    )
    if mapped is not None:
        return mapped
    if allow_top_level:
        return payload.get("source_sha256")
    return None


def _safe_boolean_checks(
    payload: dict[str, Any],
    allowed: tuple[str, ...],
) -> dict[str, bool]:
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, dict):
        return {}
    return {
        key: raw_checks[key]
        for key in allowed
        if isinstance(raw_checks.get(key), bool)
    }


def _process_privacy_audit(
    privacy: dict[str, Any],
    plan_dir: Path,
    stage: Path,
    assets: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = {
        key: privacy[key]
        for key in (
            "status",
            "required",
            "source_handling",
            "sensitive_data",
            "watermark",
            "authorized_watermark_removal",
            "source_sha256",
        )
        if key in privacy
    }
    blockers: list[dict[str, Any]] = []
    required = bool(privacy.get("required", False))
    declared_source_hash = privacy.get("source_sha256")
    if declared_source_hash is not None:
        if len(assets) != 1:
            blockers.append(
                _issue(
                    "privacy_source_hash_ambiguous",
                    "privacy.source_sha256 仅兼容单素材，并且只能表示当前待标注素材。",
                )
            )
        elif (
            _normalized_sha256(declared_source_hash)
            != assets[0]["source_sha256"].lower()
        ):
            blockers.append(
                _issue(
                    "privacy_source_hash_mismatch",
                    "privacy.source_sha256 与当前待标注素材不一致。",
                    asset_id=assets[0]["id"],
                )
            )
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
    declaration_blocker_count = len(blockers)
    declared, per_asset, combined = _collect_audit_declarations(
        privacy,
        singular_key="upstream_audit",
        plural_key="upstream_audits",
        asset_ids={asset["id"] for asset in assets},
        code_prefix="privacy_audit",
        blockers=blockers,
    )
    declaration_valid = len(blockers) == declaration_blocker_count
    if not declared:
        result["upstream_audit"] = {"declared": False}
        if required:
            blockers.append(
                _issue(
                    "privacy_audit_missing",
                    "privacy.required=true，但未声明上游隐私审计。",
                )
            )
        return result, blockers

    combined_loaded: tuple[dict[str, Any] | None, str | None] | None = None
    if combined is not None:
        combined_loaded = _load_audit_declaration(
            combined,
            plan_dir,
            label="上游隐私审计",
            code_prefix="privacy_audit",
            asset_id=None,
            blockers=blockers,
        )
    safe_reports: dict[str, dict[str, Any]] = {}
    audit_ok = declaration_valid
    load_complete = True
    for asset in assets:
        asset_id = asset["id"]
        is_combined = combined is not None
        if is_combined:
            assert combined_loaded is not None
            payload, original_audit_sha256 = combined_loaded
        elif asset_id in per_asset:
            payload, original_audit_sha256 = _load_audit_declaration(
                per_asset[asset_id],
                plan_dir,
                label=f"素材 {asset_id} 的上游隐私审计",
                code_prefix="privacy_audit",
                asset_id=asset_id,
                blockers=blockers,
            )
        else:
            payload, original_audit_sha256 = None, None
            blockers.append(
                _issue(
                    "privacy_audit_asset_missing",
                    f"上游隐私审计缺少素材 {asset_id} 的报告。",
                    asset_id=asset_id,
                )
            )
        if payload is None or original_audit_sha256 is None:
            load_complete = False
            audit_ok = False
            safe_reports[asset_id] = {
                "status": "missing",
                "passed": False,
                "output_hash_matched": False,
            }
            continue
        evidence, evidence_is_asset_specific = _asset_report_payload(
            payload,
            asset_id,
            combined=is_combined,
        )
        report_passed = _audit_passed(evidence)
        if not report_passed:
            blockers.append(
                _issue(
                    "privacy_audit_not_passed",
                    f"素材 {asset_id} 的上游隐私审计未明确通过。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        allow_top_level = evidence_is_asset_specific or len(assets) == 1
        raw_output_hash = _audit_output_sha256(
            evidence,
            asset_id,
            allow_top_level=allow_top_level,
        )
        if raw_output_hash is None and evidence is not payload:
            raw_output_hash = _audit_output_sha256(
                payload,
                asset_id,
                allow_top_level=False,
            )
        output_hash = _normalized_sha256(raw_output_hash)
        output_matches = output_hash == asset["source_sha256"].lower()
        if raw_output_hash is None:
            blockers.append(
                _issue(
                    "privacy_audit_output_hash_missing",
                    f"素材 {asset_id} 的上游隐私审计缺少 output_sha256。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        elif output_hash is None:
            blockers.append(
                _issue(
                    "privacy_audit_output_hash_invalid",
                    f"素材 {asset_id} 的 output_sha256 不是有效 SHA-256。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        elif not output_matches:
            blockers.append(
                _issue(
                    "privacy_audit_hash_mismatch",
                    f"素材 {asset_id} 必须绑定隐私审计的 output_sha256。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        raw_source_hash = _audit_source_lineage_sha256(
            evidence,
            asset_id,
            allow_top_level=allow_top_level,
        )
        if raw_source_hash is None and evidence is not payload:
            raw_source_hash = _audit_source_lineage_sha256(
                payload,
                asset_id,
                allow_top_level=False,
            )
        source_hash = _normalized_sha256(raw_source_hash)
        report_summary: dict[str, Any] = {
            "status": _audit_status(evidence),
            "passed": report_passed,
            "output_hash_matched": output_matches,
            "checks": _safe_boolean_checks(
                evidence,
                (
                    "sensitive_content_absent",
                    "watermark_policy_satisfied",
                    "metadata_removed",
                    "source_hash_matched",
                ),
            ),
            "original_audit_sha256": original_audit_sha256,
        }
        if source_hash is not None:
            report_summary["source_sha256"] = source_hash
        if output_hash is not None:
            report_summary["output_sha256"] = output_hash
        safe_reports[asset_id] = report_summary
    audit_summary: dict[str, Any] = {
        "schema_version": "2.0-summary",
        "kind": "privacy_redaction",
        "required": required,
        "passed": audit_ok,
        "load_complete": load_complete,
        "reports": safe_reports,
    }
    audit_output = stage / "qa" / "privacy-upstream-audit.json"
    write_json(audit_output, audit_summary)
    result["upstream_audit"] = {
        "declared": True,
        "required": required,
        "passed": audit_ok,
        "load_complete": load_complete,
        "path": "qa/privacy-upstream-audit.json",
        "sha256": sha256_file(audit_output),
    }
    return result, blockers


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


def _process_watermark_audit(
    privacy: dict[str, Any],
    plan_dir: Path,
    stage: Path,
    assets: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
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
    declaration_blocker_count = len(blockers)
    declared, per_asset, combined = _collect_audit_declarations(
        privacy,
        singular_key="watermark_audit",
        plural_key="watermark_audits",
        asset_ids={asset["id"] for asset in assets},
        code_prefix="watermark_audit",
        blockers=blockers,
    )
    declaration_valid = len(blockers) == declaration_blocker_count
    if not declared:
        if removal_requested:
            blockers.append(
                _issue(
                    "watermark_audit_missing",
                    "水印已声明删除，但未提供 watermark_audit。",
                )
            )
        return {
            "declared": False,
            "required": removal_requested,
            "passed": not removal_requested,
        }, blockers

    combined_loaded: tuple[dict[str, Any] | None, str | None] | None = None
    if combined is not None:
        combined_loaded = _load_audit_declaration(
            combined,
            plan_dir,
            label="水印处理审计",
            code_prefix="watermark_audit",
            asset_id=None,
            blockers=blockers,
        )
    safe_reports: dict[str, dict[str, Any]] = {}
    audit_ok = declaration_valid and not (
        removal_requested and _watermark_requires_preservation(watermark_policy)
    )
    load_complete = True
    for asset in assets:
        asset_id = asset["id"]
        is_combined = combined is not None
        if is_combined:
            assert combined_loaded is not None
            payload, original_audit_sha256 = combined_loaded
        elif asset_id in per_asset:
            payload, original_audit_sha256 = _load_audit_declaration(
                per_asset[asset_id],
                plan_dir,
                label=f"素材 {asset_id} 的水印处理审计",
                code_prefix="watermark_audit",
                asset_id=asset_id,
                blockers=blockers,
            )
        else:
            payload, original_audit_sha256 = None, None
            blockers.append(
                _issue(
                    "watermark_audit_asset_missing",
                    f"水印处理审计缺少素材 {asset_id} 的报告。",
                    asset_id=asset_id,
                )
            )
        if payload is None or original_audit_sha256 is None:
            load_complete = False
            audit_ok = False
            safe_reports[asset_id] = {
                "status": "missing",
                "passed": False,
                "authorized": False,
                "deterministic": False,
                "outside_changed_pixels_zero": False,
                "output_hash_matched": False,
            }
            continue
        evidence, evidence_is_asset_specific = _asset_report_payload(
            payload,
            asset_id,
            combined=is_combined,
        )
        checks = evidence.get("checks")
        checks = checks if isinstance(checks, dict) else {}
        report_passed = _audit_passed(evidence)
        authorized = evidence.get("authorized", checks.get("authorized")) is True
        deterministic = (
            evidence.get("deterministic", checks.get("deterministic")) is True
        )
        outside_changed_pixels = evidence.get(
            "outside_changed_pixels",
            checks.get("outside_changed_pixels"),
        )
        outside_unchanged = (
            not isinstance(outside_changed_pixels, bool)
            and isinstance(outside_changed_pixels, (int, float))
            and outside_changed_pixels == 0
        )
        allow_top_level = evidence_is_asset_specific or len(assets) == 1
        raw_output_hash = _audit_output_sha256(
            evidence,
            asset_id,
            allow_top_level=allow_top_level,
        )
        if raw_output_hash is None and evidence is not payload:
            raw_output_hash = _audit_output_sha256(
                payload,
                asset_id,
                allow_top_level=False,
            )
        output_hash = _normalized_sha256(raw_output_hash)
        output_matches = output_hash == asset["source_sha256"].lower()
        if not report_passed:
            blockers.append(
                _issue(
                    "watermark_audit_not_passed",
                    f"素材 {asset_id} 的水印处理审计未明确通过。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        if not authorized:
            blockers.append(
                _issue(
                    "watermark_audit_authorization_missing",
                    f"素材 {asset_id} 的水印处理审计未确认 authorized=true。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        if not deterministic:
            blockers.append(
                _issue(
                    "watermark_audit_not_deterministic",
                    f"素材 {asset_id} 的水印处理审计未确认 deterministic=true。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        if raw_output_hash is None:
            blockers.append(
                _issue(
                    "watermark_audit_output_hash_missing",
                    f"素材 {asset_id} 的水印处理审计缺少 output_sha256。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        elif output_hash is None:
            blockers.append(
                _issue(
                    "watermark_audit_output_hash_invalid",
                    f"素材 {asset_id} 的水印 output_sha256 不是有效 SHA-256。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        elif not output_matches:
            blockers.append(
                _issue(
                    "watermark_audit_hash_mismatch",
                    f"素材 {asset_id} 必须绑定水印审计的 output_sha256。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        if not outside_unchanged:
            blockers.append(
                _issue(
                    "watermark_audit_outside_pixels_changed",
                    f"素材 {asset_id} 未证明水印区域外像素变化为 0。",
                    asset_id=asset_id,
                )
            )
            audit_ok = False
        raw_source_hash = _audit_source_lineage_sha256(
            evidence,
            asset_id,
            allow_top_level=allow_top_level,
        )
        source_hash = _normalized_sha256(raw_source_hash)
        report_summary: dict[str, Any] = {
            "status": _audit_status(evidence),
            "passed": report_passed,
            "authorized": authorized,
            "deterministic": deterministic,
            "outside_changed_pixels_zero": outside_unchanged,
            "output_hash_matched": output_matches,
            "original_audit_sha256": original_audit_sha256,
        }
        if source_hash is not None:
            report_summary["source_sha256"] = source_hash
        if output_hash is not None:
            report_summary["output_sha256"] = output_hash
        safe_reports[asset_id] = report_summary
    summary = {
        "schema_version": "2.0-summary",
        "kind": "watermark_removal",
        "required": removal_requested,
        "passed": audit_ok,
        "load_complete": load_complete,
        "reports": safe_reports,
    }
    audit_output = stage / "qa" / "watermark-audit.json"
    write_json(audit_output, summary)
    return {
        "declared": True,
        "required": removal_requested,
        "passed": audit_ok,
        "load_complete": load_complete,
        "path": "qa/watermark-audit.json",
        "sha256": sha256_file(audit_output),
    }, blockers


def _manual_review_template(
    job_id: str,
    version: str,
    assets: list[dict[str, Any]],
    deterministic_ready: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "version": version,
        "review_index": 1,
        "status": (
            "awaiting_manual_review" if deterministic_ready else "not_ready"
        ),
        "deterministic_ready": deterministic_ready,
        "manual_review_required": True,
        "reviewer": None,
        "reviewed_at": None,
        "decision": "unchecked",
        "notes": "unchecked",
        "blockers": [],
        "assets": [
            {
                "id": asset["id"],
                "render_status": asset["render_status"],
                "decision": "unchecked",
                "notes": "unchecked",
                "annotations": [
                    {
                        "id": annotation["id"],
                        "alignment": "unchecked",
                        "readability": "unchecked",
                        "non_obstruction": "unchecked",
                        "decision": "unchecked",
                        "notes": "unchecked",
                    }
                    for annotation in asset.get("annotations", [])
                ],
            }
            for asset in assets
        ],
        "blocking_rules": [
            "标注框没有覆盖正确且完整的视觉区域。",
            "动作标签遮挡保护区域或用户必须阅读、点击的内容。",
            "标签互撞、贴线、越界或文字不可读。",
            "没有打开完整标注图、总览图和 full-contact-sheet.png 人工复核。",
            "声明的上游隐私审计不存在、未通过或源文件哈希不一致。",
        ],
    }


def _inventory(stage: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(stage).as_posix()
        if relative.startswith("sources/"):
            role = "source_archive"
        elif relative.startswith("annotated/"):
            role = "annotated"
        elif relative.startswith("overview/"):
            role = "overview"
        elif relative.startswith("plans/"):
            role = "resolved_plan"
        elif relative == "qa/full-contact-sheet.png":
            role = "full_contact_sheet"
        elif relative.startswith("qa/grids/"):
            role = "coordinate_grid"
        elif relative.startswith("qa/crops/"):
            role = "annotation_crop"
        elif relative == "qa/auto-summary.json":
            role = "auto_summary"
        elif relative == "qa/manual-review-template.json":
            role = "manual_review_template"
        elif relative == "qa/privacy-upstream-audit.json":
            role = "privacy_upstream_audit"
        elif relative == "qa/watermark-audit.json":
            role = "watermark_audit"
        else:
            role = "other"
        records.append(file_record(path, stage, role))
    return records


def run_batch(
    manifest_path: str | Path,
    output_root: str | Path,
    version: str | int | None = None,
) -> Path:
    """运行批量标注，并原子发布一个新的 ``vNNN`` 版本目录。"""

    input_path = Path(manifest_path).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    plan = read_json(input_path, "v2 作业 manifest")
    if str(plan.get("schema_version")) != SCHEMA_VERSION:
        raise ValueError("manifest.schema_version 必须是字符串 '2.0'。")
    job_id = str(plan.get("job_id", "")).strip()
    if not job_id:
        raise ValueError("manifest.job_id 不能为空。")
    safe_component(job_id, "manifest.job_id")
    style = normalize_style(plan.get("style"), plan.get("style_preset"))
    if "privacy" not in plan:
        raise ValueError("manifest 必须显式提供 privacy object。")
    privacy_raw = plan["privacy"]
    if not isinstance(privacy_raw, dict):
        raise ValueError("manifest.privacy 必须是 JSON object。")
    overview_config = _normalize_overview(plan.get("overview"))
    qa_config = _normalize_qa(plan.get("qa"))
    raw_assets = plan.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValueError("manifest.assets 必须是非空数组。")

    prepared: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_outputs: set[str] = set()
    annotation_roles: dict[str, dict[str, str]] = {}
    for index, raw_asset in enumerate(raw_assets, start=1):
        if not isinstance(raw_asset, dict):
            raise ValueError(f"assets[{index}] 必须是 object。")
        asset_id = safe_component(raw_asset.get("id", ""), f"assets[{index}].id")
        asset_key = portable_name_key(asset_id)
        if asset_key in seen_ids:
            raise ValueError(f"素材 ID 重复：{asset_id}")
        seen_ids.add(asset_key)
        source_raw = raw_asset.get("source")
        if not isinstance(source_raw, str) or not source_raw.strip():
            raise ValueError(f"素材 {asset_id} 缺少 source。")
        source_path = Path(source_raw).expanduser()
        if not source_path.is_absolute():
            source_path = (input_path.parent / source_path).resolve()
        else:
            source_path = source_path.resolve()
        if not source_path.is_file():
            raise ValueError(f"素材 {asset_id} 的源文件不存在：{source_path}")
        output_name = safe_output_name(
            raw_asset.get("output_name"),
            f"{asset_id}-annotated.png",
        )
        output_key = portable_name_key(output_name)
        if output_key in seen_outputs:
            raise ValueError(f"annotated output_name 重复：{output_name}")
        seen_outputs.add(output_key)
        title = str(raw_asset.get("title", asset_id)).strip() or asset_id
        kind = str(raw_asset.get("kind", "screenshot")).strip() or "screenshot"
        section_raw = raw_asset.get("section_id")
        section_id = (
            str(section_raw).strip() if section_raw is not None else None
        )
        if section_raw is not None and not section_id:
            raise ValueError(f"素材 {asset_id} 的 section_id 不能为空字符串。")
        protected = _normalize_protected_regions(
            raw_asset.get("protected_regions"),
            asset_id,
        )
        annotations, asset_blockers, asset_warnings = _normalize_annotations(
            raw_asset.get("annotations"),
            asset_id,
            kind,
        )
        blockers.extend(asset_blockers)
        warnings.extend(asset_warnings)
        annotation_roles[asset_id] = {
            item["id"]: item["semantic_role"] for item in annotations
        }
        prepared.append(
            {
                "id": asset_id,
                "source_path": source_path,
                "output_name": output_name,
                "title": title,
                "kind": kind,
                "section_id": section_id,
                "protected_regions": protected,
                "annotations": annotations,
            }
        )
    declared_asset_order = [asset["id"] for asset in prepared]
    flow, flow_blockers = _normalize_flow(
        plan.get("flow"),
        declared_asset_order,
        annotation_roles,
        {asset["id"]: asset["section_id"] for asset in prepared},
    )
    blockers.extend(flow_blockers)
    _apply_flow_marker_semantics(prepared, flow)

    project_root = root / job_id
    versions_root = project_root / "versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    version_name = _normalize_version(version, versions_root)
    final_dir = versions_root / version_name
    review_dir = project_root / "reviews" / version_name
    if final_dir.exists():
        raise FileExistsError(f"版本目录已存在，拒绝覆盖：{final_dir}")
    if review_dir.exists():
        raise FileExistsError(f"人工复核目录已存在，拒绝覆盖：{review_dir}")
    stage = versions_root / f".{version_name}.{uuid.uuid4().hex}.tmp"
    stage.mkdir(parents=False, exist_ok=False)
    try:
        for directory in ("sources", "annotated", "overview", "plans", "qa"):
            (stage / directory).mkdir(parents=True, exist_ok=False)
        output_assets: list[dict[str, Any]] = []
        for asset in prepared:
            suffix = asset["source_path"].suffix.lower()
            if not suffix or len(suffix) > 12 or not re.fullmatch(r"\.[a-z0-9]+", suffix):
                suffix = ".bin"
            archive = stage / "sources" / f"{asset['id']}{suffix}"
            shutil.copyfile(asset["source_path"], archive)
            digest = sha256_file(archive)
            if digest != sha256_file(asset["source_path"]):
                raise OSError(f"素材 {asset['id']} 的归档副本哈希不一致。")
            output_assets.append(
                {
                    "id": asset["id"],
                    "title": asset["title"],
                    "kind": asset["kind"],
                    "section_id": asset["section_id"],
                    "source_archive": archive.relative_to(stage).as_posix(),
                    "source_sha256": digest,
                    "source_size_bytes": archive.stat().st_size,
                    "output_name": asset["output_name"],
                    "protected_regions": [
                        {
                            **region,
                            "rect": rect_to_list(region["rect"]),
                        }
                        for region in asset["protected_regions"]
                    ],
                    "_protected": asset["protected_regions"],
                    "_annotations": asset["annotations"],
                    "_archive_path": archive,
                }
            )

        privacy, privacy_blockers = _process_privacy_audit(
            privacy_raw,
            input_path.parent,
            stage,
            output_assets,
        )
        blockers.extend(privacy_blockers)
        watermark_audit, watermark_blockers = _process_watermark_audit(
            privacy_raw,
            input_path.parent,
            stage,
            output_assets,
        )
        privacy["watermark_audit"] = watermark_audit
        blockers.extend(watermark_blockers)

        overview_items = []
        crop_records: list[dict[str, Any]] = []
        for asset in output_assets:
            annotated = stage / "annotated" / asset["output_name"]
            try:
                render_result = render_annotated_asset(
                    asset["_archive_path"],
                    annotated,
                    asset["_annotations"],
                    asset["_protected"],
                    style,
                )
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                render_result = {
                    "render_status": "unsupported",
                    "unsupported_reason": f"无法作为单帧栅格渲染：{exc}",
                    "detected_format": None,
                    "frame_count": None,
                }
            asset.update(render_result)
            blockers.extend(render_result.get("blockers", []))
            warnings.extend(render_result.get("warnings", []))
            if asset["render_status"] == "unsupported":
                blockers.append(
                    _issue(
                        "unsupported_asset",
                        (
                            f"素材 {asset['id']} 不是受支持的单帧 PNG/JPEG/WebP，"
                            "已仅归档源文件，项目不能自动放行。"
                        ),
                        asset_id=asset["id"],
                    )
                )
            if asset["render_status"] == "rendered":
                width, height = asset["image_size"]
                for annotation in asset["annotations"]:
                    if not rect_inside(tuple(annotation["rect"]), width, height):
                        blockers.append(
                            _issue(
                                "annotation_rect_out_of_bounds",
                                (
                                    f"素材 {asset['id']} 的标注 {annotation['id']} "
                                    "目标框超出图片边界。"
                                ),
                                asset_id=asset["id"],
                                annotation_id=annotation["id"],
                            )
                        )
                    target_contract, contract_blockers = _evaluate_target_contract(
                        asset["id"], annotation, width, height
                    )
                    if target_contract is not None:
                        annotation["target_contract"] = target_contract
                    blockers.extend(contract_blockers)
                for region in asset["protected_regions"]:
                    if not rect_inside(tuple(region["rect"]), width, height):
                        blockers.append(
                            _issue(
                                "protected_region_out_of_bounds",
                                (
                                    f"素材 {asset['id']} 的保护区域 {region['id']} "
                                    "超出图片边界。"
                                ),
                                asset_id=asset["id"],
                            )
                        )
                asset["annotated"] = annotated.relative_to(stage).as_posix()
                asset["annotated_sha256"] = sha256_file(annotated)
                asset["annotated_size_bytes"] = annotated.stat().st_size
                crop_dir = stage / "qa" / "crops" / asset["id"]
                raw_crop_records = build_annotation_crops(
                    annotated,
                    asset["id"],
                    asset["annotations"],
                    crop_dir,
                    padding=int(qa_config["crop_padding"]),
                )
                asset_crops: list[dict[str, Any]] = []
                for crop in raw_crop_records:
                    crop_path = crop_dir / crop.pop("name")
                    record = {
                        **crop,
                        "path": crop_path.relative_to(stage).as_posix(),
                    }
                    asset_crops.append(record)
                    crop_records.append(record)
                asset["annotation_crops"] = asset_crops
                overview_items.append(
                    {
                        "asset_id": asset["id"],
                        "title": asset["title"],
                        "path": annotated,
                        "source_size": asset["image_size"],
                        "label_font_size": int(
                            asset["style"]["callout_font_size"]
                        ),
                    }
                )
            else:
                asset["annotated"] = None
                asset["annotation_crops"] = []
                asset["annotations"] = [
                    {
                        **annotation,
                        "rect": rect_to_list(annotation["rect"]),
                        "callout": {
                            **annotation["callout"],
                            "label_rect": (
                                rect_to_list(annotation["callout"]["label_rect"])
                                if annotation["callout"]["label_rect"] is not None
                                else None
                            ),
                        },
                    }
                    for annotation in asset["_annotations"]
                ]

        grid_config = qa_config["coordinate_grids"]
        requested_grid_assets: set[str] = set()
        if grid_config["enabled"]:
            if grid_config["assets"] == "all":
                requested_grid_assets = {asset["id"] for asset in output_assets}
            else:
                requested_grid_assets = set(grid_config["assets"])
                unknown = requested_grid_assets - {asset["id"] for asset in output_assets}
                if unknown:
                    raise ValueError(
                        "qa.coordinate_grids 引用了不存在的素材："
                        + "、".join(sorted(unknown))
                    )
            (stage / "qa" / "grids").mkdir(parents=True, exist_ok=True)
        grid_records = []
        for asset in output_assets:
            if asset["id"] not in requested_grid_assets:
                asset["coordinate_grid"] = None
                continue
            if asset["render_status"] != "rendered":
                warnings.append(
                    _issue(
                        "coordinate_grid_unsupported",
                        f"素材 {asset['id']} 不可渲染，未生成坐标网格。",
                        asset_id=asset["id"],
                    )
                )
                asset["coordinate_grid"] = None
                continue
            grid_path = stage / "qa" / "grids" / f"{asset['id']}-grid.png"
            grid_result = render_coordinate_grid(
                asset["_archive_path"],
                grid_path,
                int(grid_config["step"]),
            )
            record = {
                "asset_id": asset["id"],
                "path": grid_path.relative_to(stage).as_posix(),
                "step": grid_result["step"],
                "size": grid_result["size"],
                "sha256": sha256_file(grid_path),
            }
            asset["coordinate_grid"] = record
            grid_records.append(record)

        flow_order = _effective_flow_order(
            flow,
            [asset["id"] for asset in output_assets],
        )
        flow_order_index = {
            asset_id: index for index, asset_id in enumerate(flow_order)
        }
        overview_items.sort(
            key=lambda item: flow_order_index.get(item["asset_id"], len(flow_order))
        )
        overview = build_overview(
            overview_items,
            stage / "overview",
            overview_config,
        )
        for issue in overview.get("readability_issues", []):
            blockers.append(
                _issue(
                    issue["code"],
                    (
                        f"素材 {issue['asset_id']} 在总览中的有效标签字号 "
                        f"{issue['effective_label_font_px']}px 低于 "
                        f"{issue['minimum_label_font_px']}px，必须调整布局或分片。"
                    ),
                    asset_id=issue["asset_id"],
                    **{
                        key: value
                        for key, value in issue.items()
                        if key not in {"code", "asset_id"}
                    },
                )
            )
        for page in overview["pages"]:
            page_path = stage / "overview" / page["path"]
            page["path"] = page_path.relative_to(stage).as_posix()
            page["sha256"] = sha256_file(page_path)
        contact_path = stage / "qa" / "full-contact-sheet.png"
        contact = build_contact_sheet(overview_items, contact_path)
        contact["path"] = contact_path.relative_to(stage).as_posix()
        contact["sha256"] = sha256_file(contact_path)

        for asset in output_assets:
            for private_key in ("_protected", "_annotations", "_archive_path"):
                asset.pop(private_key, None)
        status = "blocked" if blockers else "awaiting_manual_review"
        manual_review_status = (
            "not_ready" if blockers else "awaiting_manual_review"
        )
        resolved_plan = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "version": version_name,
            "status": status,
            "style": style,
            "privacy": privacy,
            "flow": flow,
            "overview": overview,
            "qa": {
                "coordinate_grids": {
                    **grid_config,
                    "files": grid_records,
                },
                "annotation_crops": {
                    "padding": qa_config["crop_padding"],
                    "files": crop_records,
                },
                "full_contact_sheet": contact,
            },
            "assets": output_assets,
            "blockers": blockers,
            "warnings": warnings,
            "manual_review_required": True,
            "manual_review_status": manual_review_status,
        }
        resolved_path = stage / "plans" / "resolved-plan.json"
        write_json(resolved_path, resolved_plan)

        manual_review = _manual_review_template(
            job_id,
            version_name,
            output_assets,
            not blockers,
        )
        manual_path = stage / "qa" / "manual-review-template.json"
        write_json(manual_path, manual_review)
        auto_summary = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "version": version_name,
            "status": status,
            "deterministic_passed": not blockers,
            "manual_review_required": True,
            "manual_review_status": manual_review_status,
            "rendered_asset_count": sum(
                asset["render_status"] == "rendered" for asset in output_assets
            ),
            "unsupported_asset_count": sum(
                asset["render_status"] == "unsupported" for asset in output_assets
            ),
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "blockers": blockers,
            "warnings": warnings,
            "resolved_plan": "plans/resolved-plan.json",
            "manual_review_template": "qa/manual-review-template.json",
            "full_contact_sheet": "qa/full-contact-sheet.png",
        }
        auto_path = stage / "qa" / "auto-summary.json"
        write_json(auto_path, auto_summary)

        inventory = _inventory(stage)
        output_manifest = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "version": version_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "deterministic_passed": not blockers,
            "manual_review_required": True,
            "manual_review_status": manual_review_status,
            "style": style,
            "privacy": privacy,
            "flow": flow,
            "overview": overview,
            "qa": {
                "coordinate_grids": {
                    **grid_config,
                    "files": grid_records,
                },
                "annotation_crops": {
                    "padding": qa_config["crop_padding"],
                    "files": crop_records,
                },
                "full_contact_sheet": contact,
                "auto_summary": "qa/auto-summary.json",
                "manual_review_template": "qa/manual-review-template.json",
            },
            "assets": output_assets,
            "blockers": blockers,
            "warnings": warnings,
            "files": {
                "resolved_plan": "plans/resolved-plan.json",
                "review_workspace": f"reviews/{version_name}",
                "inventory": inventory,
            },
            "input_manifest": {
                "sha256": sha256_file(input_path),
            },
        }
        manifest_path = stage / "manifest.json"
        write_json(manifest_path, output_manifest)
        review_record = {
            **manual_review,
            "manifest_sha256": sha256_file(manifest_path),
        }
        if final_dir.exists():
            raise FileExistsError(f"版本目录在发布时已出现，拒绝覆盖：{final_dir}")
        reviews_root = project_root / "reviews"
        reviews_root.mkdir(parents=True, exist_ok=True)
        review_stage = reviews_root / f".{version_name}.{uuid.uuid4().hex}.tmp"
        review_stage.mkdir(parents=True, exist_ok=False)
        try:
            write_json(review_stage / "review-001.json", review_record)
            os.rename(stage, final_dir)
            try:
                os.rename(review_stage, review_dir)
            except Exception:
                # final_dir 是本次刚刚由唯一 stage 发布的精确版本目录，可安全回滚。
                if final_dir.exists():
                    shutil.rmtree(final_dir)
                raise
        except Exception:
            if review_stage.exists():
                shutil.rmtree(review_stage)
            raise
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return final_dir
