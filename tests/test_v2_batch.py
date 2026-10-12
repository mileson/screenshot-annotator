from __future__ import annotations

import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image

from tests.support import (
    load_script_module,
    make_animated_gif,
    make_synthetic_screenshot,
    read_json,
    sha256,
    write_json,
)


def _resolved_plan_path(version_dir: Path) -> Path:
    path = version_dir / "plans" / "resolved-plan.json"
    if not path.is_file():
        raise AssertionError(f"缺少 v2 契约产物：{path}")
    return path


def _blocker_codes(summary: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for blocker in summary.get("blockers", []):
        if isinstance(blocker, str):
            values.append(blocker)
        elif isinstance(blocker, dict):
            code = blocker.get("code") or blocker.get("type") or blocker.get("kind")
            if code is not None:
                values.append(str(code))
    for asset in summary.get("assets", []):
        if isinstance(asset, dict):
            values.extend(_blocker_codes(asset))
    return values


def _asset_map(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = document.get("assets")
    if not isinstance(assets, list):
        raise AssertionError("产物缺少 assets 数组")
    return {
        str(asset["id"]): asset
        for asset in assets
        if isinstance(asset, dict) and "id" in asset
    }


def _mutate_rgb_pixel(path: Path, point: tuple[int, int]) -> None:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    old = image.getpixel(point)
    replacement = tuple((int(channel) + 127) % 256 for channel in old)
    if replacement == old:
        raise AssertionError("测试像素必须实际发生变化")
    image.putpixel(point, replacement)
    image.save(path, format="PNG")


def _refresh_manifest_integrity_record(
    version_dir: Path,
    relative_path: str,
) -> None:
    """更新普通哈希字段，确保测试命中语义防篡改而非基础哈希检查。"""
    manifest_path = version_dir / "manifest.json"
    manifest = read_json(manifest_path)
    target = version_dir / relative_path
    digest = sha256(target)
    size = target.stat().st_size

    for record in manifest.get("files", {}).get("inventory", []):
        if record.get("path") == relative_path:
            record["sha256"] = digest
            record["size_bytes"] = size
            break
    else:
        raise AssertionError(f"manifest inventory 缺少 {relative_path}")

    for asset in manifest.get("assets", []):
        if asset.get("annotated") == relative_path:
            asset["annotated_sha256"] = digest
            asset["annotated_size_bytes"] = size

    for page in manifest.get("overview", {}).get("pages", []):
        if page.get("path") == relative_path:
            page["sha256"] = digest

    write_json(manifest_path, manifest)


def _mutate_manifest_and_resolved_plan(
    version_dir: Path,
    mutate: Any,
) -> None:
    """Apply one tamper to both identity-bound documents and refresh the file hash."""

    manifest_path = version_dir / "manifest.json"
    resolved_path = _resolved_plan_path(version_dir)
    manifest = read_json(manifest_path)
    resolved = read_json(resolved_path)
    mutate(manifest)
    mutate(resolved)
    write_json(resolved_path, resolved)

    relative = resolved_path.relative_to(version_dir).as_posix()
    for record in manifest.get("files", {}).get("inventory", []):
        if record.get("path") == relative:
            record["sha256"] = sha256(resolved_path)
            record["size_bytes"] = resolved_path.stat().st_size
            break
    else:
        raise AssertionError(f"manifest inventory 缺少 {relative}")
    write_json(manifest_path, manifest)


class V2BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.batch_module = load_script_module(
            "annotate_batch.py",
            "screenshot_annotator_v2_batch_for_tests",
        )
        cls.validator_module = load_script_module(
            "validate_annotation_project.py",
            "screenshot_annotator_v2_validator_for_tests",
        )
        cls.rendering_module = importlib.import_module(
            "screenshot_annotator_v2.rendering"
        )
        if not callable(getattr(cls.batch_module, "run_batch", None)):
            raise AssertionError("annotate_batch.py 必须公开 run_batch()")
        if not callable(getattr(cls.validator_module, "validate_project", None)):
            raise AssertionError(
                "validate_annotation_project.py 必须公开 validate_project()"
            )

    def _manifest(
        self,
        *,
        job_id: str,
        assets: list[dict[str, Any]],
        overview: dict[str, Any] | None = None,
        privacy: dict[str, Any] | None = None,
        style: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "job_id": job_id,
            "style": style or {"preset": "action-orange-v2"},
            "privacy": privacy
            or {
                "source_handling": "read_only_copy",
                "sensitive_data": "not_present_synthetic_fixture",
                "watermark": "preserve_unknown",
                "authorized_watermark_removal": False,
            },
            "flow": {
                "type": "linear",
                "sort": "declared_order",
                "order": [str(asset["id"]) for asset in assets],
                "edges": [],
            },
            "overview": overview
            or {
                "mode": "grid",
                "columns": 4,
                "max_pixels": 40_000_000,
            },
            "assets": assets,
        }

    def _run(
        self,
        root: Path,
        manifest: dict[str, Any],
        *,
        version: str | None = None,
    ) -> Path:
        manifest_path = write_json(root / f"{manifest['job_id']}-manifest.json", manifest)
        version_dir = self.batch_module.run_batch(
            manifest_path,
            root / "output",
            version=version,
        )
        version_path = Path(version_dir)
        self.assertTrue(version_path.is_dir())
        return version_path

    def test_single_sequence_alternative_and_action_orange_resolution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-semantics-") as temp:
            root = Path(temp)
            sources = [
                make_synthetic_screenshot(root / f"source-{index}.png")
                for index in range(1, 4)
            ]
            assets = [
                {
                    "id": "single",
                    "source": str(sources[0]),
                    "output_name": "01-single.png",
                    "kind": "sequential_action",
                    "protected_regions": [],
                    "annotations": [
                        {
                            "id": "single-action",
                            "rect": [115, 260, 535, 415],
                            "semantic_role": "single",
                            "callout": {
                                "text": "点击演示入口",
                                "marker": "none",
                                "anchor": "auto",
                            },
                        }
                    ],
                },
                {
                    "id": "sequence",
                    "source": str(sources[1]),
                    "output_name": "02-sequence.png",
                    "kind": "sequential_action",
                    "protected_regions": [],
                    "annotations": [
                        {
                            "id": f"sequence-{index}",
                            "rect": rect,
                            "semantic_role": "sequence",
                            "callout": {
                                "text": text,
                                "marker": "auto",
                                "anchor": "auto",
                            },
                        }
                        for index, (rect, text) in enumerate(
                            [
                                ([115, 260, 535, 415], "先选择演示入口"),
                                ([615, 260, 1035, 415], "再核对演示信息"),
                                ([115, 480, 535, 635], "最后完成演示"),
                            ],
                            start=1,
                        )
                    ],
                },
                {
                    "id": "alternative",
                    "source": str(sources[2]),
                    "output_name": "03-alternative.png",
                    "kind": "alternative",
                    "protected_regions": [],
                    "annotations": [
                        {
                            "id": "alternative-a",
                            "rect": [115, 260, 535, 415],
                            "semantic_role": "alternative",
                            "callout": {
                                "text": "选择入口 A",
                                "marker": "auto",
                                "anchor": "auto",
                            },
                        },
                        {
                            "id": "alternative-b",
                            "rect": [115, 480, 535, 635],
                            "semantic_role": "alternative",
                            "callout": {
                                "text": "选择入口 B",
                                "marker": "auto",
                                "anchor": "auto",
                            },
                        },
                    ],
                },
            ]
            version_dir = self._run(
                root,
                self._manifest(job_id="semantic-markers", assets=assets),
            )
            resolved = read_json(_resolved_plan_path(version_dir))
            resolved_assets = _asset_map(resolved)

            single_annotation = resolved_assets["single"]["annotations"][0]
            self.assertNotIn("marker", single_annotation)
            self.assertNotIn("anchor", single_annotation)
            self.assertNotIn("label_rect", single_annotation)
            self.assertIsNone(
                single_annotation["callout"]["resolved_marker"],
                "single + marker=none 不得渲染冗余编号",
            )
            sequence_markers = [
                str(item["callout"]["resolved_marker"])
                for item in resolved_assets["sequence"]["annotations"]
            ]
            self.assertEqual(sequence_markers, ["1", "2", "3"])
            alternative_markers = [
                str(item["callout"]["resolved_marker"])
                for item in resolved_assets["alternative"]["annotations"]
            ]
            self.assertEqual(alternative_markers, ["A", "B"])

            self.assertEqual(
                resolved.get("style", {}).get("preset"),
                "action-orange-v2",
            )
            for asset_id in ("single", "sequence", "alternative"):
                annotated_path = version_dir / "annotated" / assets[
                    ["single", "sequence", "alternative"].index(asset_id)
                ]["output_name"]
                self.assertTrue(annotated_path.is_file())
                with Image.open(annotated_path) as image:
                    self.assertEqual(image.size, (1200, 900))
                    self.assertFalse(
                        {
                            "exif",
                            "gps",
                            "xmp",
                            "XML:com.adobe.xmp",
                            "Author",
                            "Comment",
                        }
                        & set(image.info),
                        "派生标注图不得保留敏感元数据",
                    )

    def test_marker_request_cannot_override_semantic_role(self) -> None:
        invalid_combinations = [
            ("single", "number"),
            ("single", "自定义"),
            ("sequence", "letter"),
            ("alternative", "none"),
            ("alternative", "number"),
        ]
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-marker-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "marker-source.png")
            for index, (role, marker) in enumerate(invalid_combinations, start=1):
                with self.subTest(role=role, marker=marker):
                    asset = {
                        "id": f"invalid-marker-{index}",
                        "source": str(source),
                        "output_name": f"invalid-marker-{index}.png",
                        "kind": "alternative" if role == "alternative" else "sequential_action",
                        "protected_regions": [],
                        "annotations": [
                            {
                                "id": "action",
                                "rect": [115, 260, 535, 415],
                                "semantic_role": role,
                                "callout": {
                                    "text": (
                                        "选择演示入口"
                                        if role == "alternative"
                                        else "点击演示入口"
                                    ),
                                    "marker": marker,
                                    "anchor": "auto",
                                },
                            }
                        ],
                    }
                    with self.assertRaisesRegex(ValueError, "不兼容"):
                        self._run(
                            root / f"invalid-{index}",
                            self._manifest(
                                job_id=f"invalid-marker-{index}",
                                assets=[asset],
                            ),
                        )

            valid_asset = {
                "id": "valid-single",
                "source": str(source),
                "output_name": "valid-single.png",
                "kind": "sequential_action",
                "protected_regions": [],
                "annotations": [
                    {
                        "id": "action",
                        "rect": [115, 260, 535, 415],
                        "semantic_role": "single",
                        "callout": {
                            "text": "点击演示入口",
                            "marker": "none",
                            "anchor": "auto",
                        },
                    }
                ],
            }
            version_dir = self._run(
                root / "validator",
                self._manifest(
                    job_id="validator-marker-role",
                    assets=[valid_asset],
                ),
            )
            manifest_path = version_dir / "manifest.json"
            manifest = read_json(manifest_path)
            callout = manifest["assets"][0]["annotations"][0]["callout"]
            callout["requested_marker"] = "number"
            write_json(manifest_path, manifest)
            validation = self.validator_module.validate_project(version_dir)
            self.assertIn("marker_request_invalid", _blocker_codes(validation))

    def test_large_callout_near_edge_stays_inside_canvas(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-edge-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "edge-source.png")
            asset = {
                "id": "edge",
                "source": str(source),
                "output_name": "edge.png",
                "kind": "sequential_action",
                "protected_regions": [],
                "annotations": [
                    {
                        "id": "edge-action",
                        "rect": [820, 650, 1185, 885],
                        "semantic_role": "single",
                        "callout": {
                            "text": "点击并确认演示结果",
                            "marker": "none",
                            "anchor": "auto",
                        },
                    }
                ],
            }
            version_dir = self._run(
                root,
                self._manifest(
                    job_id="edge-layout",
                    assets=[asset],
                    style={"preset": "action-orange-v2"},
                ),
            )
            resolved = read_json(_resolved_plan_path(version_dir))
            edge_asset = _asset_map(resolved)["edge"]
            annotation = edge_asset["annotations"][0]
            self.assertNotIn("label_rect", annotation)
            label_rect = annotation["callout"].get("label_rect")
            self.assertIsNotNone(label_rect, "resolved plan 必须记录实际标签框")
            assert label_rect is not None
            x1, y1, x2, y2 = label_rect
            self.assertGreaterEqual(x1, 0)
            self.assertGreaterEqual(y1, 0)
            self.assertLessEqual(x2, 1200)
            self.assertLessEqual(y2, 900)
            font_size = (
                annotation.get("font_size")
                or annotation.get("callout", {}).get("font_size")
                or resolved.get("style", {}).get("font_size")
                or resolved.get("style", {}).get("callout", {}).get("font_size")
                or edge_asset.get("style", {}).get("callout_font_size")
                or resolved.get("style", {}).get("callout_font_size")
            )
            self.assertIsNotNone(font_size, "resolved plan 必须记录实际字号")
            self.assertGreaterEqual(int(font_size), 44)
            resolved_style = edge_asset.get("style") or resolved.get("style", {})
            line_width = resolved_style.get("line_width") or resolved_style.get(
                "rect", {}
            ).get("line_width")
            rect_radius = resolved_style.get("rect_radius") or resolved_style.get(
                "rect", {}
            ).get("radius")
            self.assertEqual(int(line_width), 8)
            self.assertEqual(int(rect_radius), 26)
            with Image.open(version_dir / "annotated" / "edge.png") as opened:
                rgb = opened.convert("RGB")
                orange_pixels = sum(
                    1
                    for pixel in rgb.getdata()
                    if pixel == (249, 115, 22)
                )
            self.assertGreater(
                orange_pixels,
                500,
                "action-orange-v2 应实际使用 #F97316 绘制框与标签",
            )

    def test_action_red_v2_renders_and_classic_red_v1_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-red-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "red-source.png")
            asset = {
                "id": "red-action",
                "source": str(source),
                "output_name": "red-action.png",
                "kind": "operation",
                "protected_regions": [],
                "annotations": [
                    {
                        "id": "click-red-action",
                        "rect": [115, 260, 535, 415],
                        "semantic_role": "single",
                        "callout": {
                            "text": "点击演示入口",
                            "marker": "none",
                            "anchor": "auto",
                        },
                    }
                ],
            }
            red_manifest = self._manifest(
                job_id="action-red",
                assets=[asset],
                style={"preset": "action-red-v2"},
            )
            version_dir = self._run(root, red_manifest)
            resolved = read_json(_resolved_plan_path(version_dir))
            self.assertEqual(resolved["style"]["preset"], "action-red-v2")
            with Image.open(version_dir / "annotated" / "red-action.png") as opened:
                rgb = opened.convert("RGB")
                red_pixels = sum(
                    1
                    for pixel in rgb.getdata()
                    if pixel == (230, 35, 35)
                )
            self.assertGreater(
                red_pixels,
                500,
                "action-red-v2 应实际使用 #E62323 绘制框与标签",
            )
            validation = self.validator_module.validate_project(version_dir)
            self.assertEqual(validation.get("status"), "awaiting_manual_review", validation)

            classic_manifest = copy.deepcopy(red_manifest)
            classic_manifest["job_id"] = "classic-red-rejected"
            classic_manifest["style"] = {"preset": "classic-red-v1"}
            with self.assertRaisesRegex(
                ValueError,
                r"classic-red-v1.*annotate_screenshot\.py.*action-red-v2",
            ):
                self._run(root / "classic", classic_manifest)

    def test_protected_region_and_label_collision_create_blockers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-collision-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "collision-source.png")
            asset = {
                "id": "collision",
                "source": str(source),
                "output_name": "collision.png",
                "kind": "sequential_action",
                "protected_regions": [
                    {
                        "id": "must-remain-readable",
                        "rect": [60, 90, 1140, 840],
                        "reason": "synthetic protected content",
                    }
                ],
                "annotations": [
                    {
                        "id": "protected-overlap",
                        "rect": [115, 260, 535, 415],
                        "semantic_role": "sequence",
                        "callout": {
                            "text": "执行第一项演示",
                            "marker": "auto",
                            "anchor": "manual",
                            "label_rect": [180, 300, 540, 380],
                        },
                    },
                    {
                        "id": "label-overlap",
                        "rect": [615, 260, 1035, 415],
                        "semantic_role": "sequence",
                        "callout": {
                            "text": "执行第二项演示",
                            "marker": "auto",
                            "anchor": "manual",
                            "label_rect": [180, 300, 540, 380],
                        },
                    },
                ],
            }
            version_dir = self._run(
                root,
                self._manifest(job_id="collision-blockers", assets=[asset]),
            )
            summary = read_json(version_dir / "qa" / "auto-summary.json")
            codes = _blocker_codes(summary)
            self.assertIn("protected_region_collision", codes)
            self.assertIn("label_collision", codes)
            self.assertNotEqual(summary.get("status"), "passed")

    def test_grid_accepts_four_to_seven_columns_and_vertical_splits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-overview-") as temp:
            root = Path(temp)
            sources = [
                make_synthetic_screenshot(
                    root / f"overview-{index}.png",
                    size=(360, 280),
                )
                for index in range(1, 9)
            ]
            assets = [
                {
                    "id": f"overview-{index}",
                    "source": str(source),
                    "output_name": f"{index:02d}-overview.png",
                    "kind": "information",
                    "protected_regions": [],
                    "annotations": [],
                }
                for index, source in enumerate(sources, start=1)
            ]

            for columns in (4, 5, 6, 7):
                with self.subTest(columns=columns):
                    manifest = self._manifest(
                        job_id=f"grid-{columns}",
                        assets=copy.deepcopy(assets),
                        overview={
                            "mode": "grid",
                            "columns": columns,
                            "max_pixels": 40_000_000,
                        },
                    )
                    version_dir = self._run(root / f"grid-{columns}", manifest)
                    resolved = read_json(_resolved_plan_path(version_dir))
                    overview = resolved.get("overview", {})
                    self.assertEqual(overview.get("resolved_mode"), "grid")
                    self.assertEqual(int(overview.get("columns")), columns)
                    placements = [
                        placement
                        for page in overview.get("pages", [])
                        for placement in page.get("placements", [])
                    ]
                    self.assertEqual(len(overview.get("pages", [])), 1)
                    self.assertEqual(len(placements), len(assets))
                    x_positions = {
                        int(item["rect"][0])
                        for item in placements
                    }
                    self.assertEqual(len(x_positions), columns)
                    self.assertTrue(list((version_dir / "overview").glob("*.png")))

            for columns in (3, 8):
                with self.subTest(rejected_columns=columns):
                    invalid_manifest = self._manifest(
                        job_id=f"grid-invalid-{columns}",
                        assets=copy.deepcopy(assets[:1]),
                        overview={
                            "mode": "grid",
                            "columns": columns,
                            "max_pixels": 40_000_000,
                        },
                    )
                    with self.assertRaises(ValueError):
                        self._run(
                            root / f"grid-invalid-{columns}",
                            invalid_manifest,
                        )

            vertical_manifest = self._manifest(
                job_id="vertical-split",
                assets=copy.deepcopy(assets),
                overview={
                    "mode": "vertical",
                    "max_pixels": 750_000,
                },
            )
            vertical_dir = self._run(root / "vertical", vertical_manifest)
            vertical_resolved = read_json(_resolved_plan_path(vertical_dir))
            overview = vertical_resolved.get("overview", {})
            parts = overview.get("pages") or []
            overview_images = list((vertical_dir / "overview").glob("*.png"))
            self.assertGreaterEqual(
                len(parts),
                2,
                "vertical 超出像素预算时必须分片",
            )
            self.assertEqual(len(overview_images), len(parts))
            for part in parts:
                self.assertLessEqual(
                    int(part["pixel_count"]),
                    int(vertical_manifest["overview"]["max_pixels"]),
                )

    def test_flow_order_controls_cross_asset_markers_and_overview_pages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-flow-") as temp:
            root = Path(temp)
            asset_ids = ["a", "b", "c", "d", "e", "f"]
            flow_order = ["f", "c", "a", "e", "b", "d"]
            sources = {
                asset_id: make_synthetic_screenshot(root / f"{asset_id}.png")
                for asset_id in asset_ids
            }
            assets = [
                {
                    "id": asset_id,
                    "source": str(sources[asset_id]),
                    "output_name": f"{asset_id}.png",
                    "kind": "operation",
                    "protected_regions": [],
                    "annotations": [
                        {
                            "id": f"step-{asset_id}",
                            "rect": [115, 260, 535, 415],
                            "semantic_role": "sequence",
                            "callout": {
                                "text": "点击演示入口",
                                "marker": "auto",
                                "anchor": "auto",
                            },
                        }
                    ],
                }
                for asset_id in asset_ids
            ]
            manifest = self._manifest(
                job_id="flow-order",
                assets=assets,
                overview={
                    "mode": "grid",
                    "columns": 4,
                    "max_pixels": 900_000,
                },
            )
            manifest["flow"] = {
                "type": "linear",
                "sort": "declared_order",
                "order": flow_order,
                "edges": [
                    {
                        "from": current,
                        "callout_id": f"step-{current}",
                        "to": following,
                        "kind": "next",
                    }
                    for current, following in zip(flow_order, flow_order[1:])
                ],
            }

            version_dir = self._run(root, manifest)
            resolved = read_json(_resolved_plan_path(version_dir))
            self.assertEqual(resolved["flow"]["order"], flow_order)
            resolved_assets = _asset_map(resolved)
            marker_by_asset = {
                asset_id: resolved_assets[asset_id]["annotations"][0]["callout"][
                    "resolved_marker"
                ]
                for asset_id in asset_ids
            }
            self.assertEqual(
                marker_by_asset,
                {
                    asset_id: str(index)
                    for index, asset_id in enumerate(flow_order, start=1)
                },
            )

            pages = resolved["overview"]["pages"]
            self.assertEqual(len(pages), 2, "低像素预算应让声明顺序跨两页")
            placement_order = [
                placement["asset_id"]
                for page in pages
                for placement in page["placements"]
            ]
            self.assertEqual(placement_order, flow_order)
            validation = self.validator_module.validate_project(version_dir)
            self.assertEqual(validation.get("status"), "awaiting_manual_review", validation)
            self.assertTrue(validation.get("deterministic_passed"), validation)

    def test_section_numbering_restarts_numbers_for_each_section(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-sections-") as temp:
            root = Path(temp)
            asset_ids = ["a1", "a2", "b1", "b2"]
            assets = []
            for asset_id in asset_ids:
                source = make_synthetic_screenshot(root / f"{asset_id}.png")
                assets.append(
                    {
                        "id": asset_id,
                        "section_id": "section-a" if asset_id.startswith("a") else "section-b",
                        "source": str(source),
                        "output_name": f"{asset_id}.png",
                        "kind": "operation",
                        "protected_regions": [],
                        "annotations": [
                            {
                                "id": f"step-{asset_id}",
                                "rect": [115, 260, 535, 415],
                                "semantic_role": "sequence",
                                "callout": {
                                    "text": "点击演示入口",
                                    "marker": "auto",
                                    "anchor": "auto",
                                },
                            }
                        ],
                    }
                )
            manifest = self._manifest(job_id="section-numbering", assets=assets)
            manifest["flow"]["numbering_scope"] = "section"

            version_dir = self._run(root, manifest)
            resolved = read_json(_resolved_plan_path(version_dir))
            resolved_assets = _asset_map(resolved)
            self.assertEqual(
                {
                    asset_id: resolved_assets[asset_id]["annotations"][0]["callout"]["resolved_marker"]
                    for asset_id in asset_ids
                },
                {"a1": "1", "a2": "2", "b1": "1", "b2": "2"},
            )
            validation = self.validator_module.validate_project(version_dir)
            self.assertTrue(validation.get("deterministic_passed"), validation)

    def test_section_numbering_requires_section_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-section-required-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            manifest = self._manifest(
                job_id="section-required",
                assets=[
                    {
                        "id": "missing-section",
                        "source": str(source),
                        "output_name": "missing-section.png",
                        "kind": "operation",
                        "protected_regions": [],
                        "annotations": [
                            {
                                "id": "step",
                                "rect": [115, 260, 535, 415],
                                "semantic_role": "sequence",
                                "callout": {
                                    "text": "点击演示入口",
                                    "marker": "auto",
                                    "anchor": "auto",
                                },
                            }
                        ],
                    }
                ],
            )
            manifest["flow"]["numbering_scope"] = "section"
            version_dir = self._run(root, manifest)
            validation = self.validator_module.validate_project(version_dir)
            self.assertFalse(validation.get("deterministic_passed"), validation)
            blocker_codes = {item.get("code") for item in validation.get("blockers", [])}
            self.assertIn("section_id_required", blocker_codes)

    def test_default_numbering_scope_remains_job_wide(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-job-scope-") as temp:
            root = Path(temp)
            assets = []
            for asset_id, section_id in (("a", "section-a"), ("b", "section-b")):
                source = make_synthetic_screenshot(root / f"{asset_id}.png")
                assets.append(
                    {
                        "id": asset_id,
                        "section_id": section_id,
                        "source": str(source),
                        "output_name": f"{asset_id}.png",
                        "kind": "operation",
                        "protected_regions": [],
                        "annotations": [
                            {
                                "id": f"step-{asset_id}",
                                "rect": [115, 260, 535, 415],
                                "semantic_role": "sequence",
                                "callout": {
                                    "text": "点击演示入口",
                                    "marker": "auto",
                                    "anchor": "auto",
                                },
                            }
                        ],
                    }
                )
            manifest = self._manifest(job_id="job-scope-compatible", assets=assets)
            version_dir = self._run(root, manifest)
            resolved = read_json(_resolved_plan_path(version_dir))
            resolved_assets = _asset_map(resolved)
            self.assertEqual(
                [
                    resolved_assets[asset_id]["annotations"][0]["callout"]["resolved_marker"]
                    for asset_id in ("a", "b")
                ],
                ["1", "2"],
            )

    def test_source_hash_metadata_unsupported_and_version_non_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-integrity-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(
                root / "metadata-source.png",
                with_metadata=True,
            )
            with Image.open(source) as source_image:
                self.assertIn("Author", source_image.info)
                self.assertIn("Comment", source_image.info)
                self.assertIn("exif", source_image.info)
            source_hash = sha256(source)
            unsupported = make_animated_gif(root / "animated.gif")
            manifest = self._manifest(
                job_id="integrity",
                assets=[
                    {
                        "id": "supported",
                        "source": str(source),
                        "output_name": "01-supported.png",
                        "kind": "information",
                        "protected_regions": [],
                        "annotations": [],
                    },
                    {
                        "id": "unsupported",
                        "source": str(unsupported),
                        "output_name": "02-unsupported.png",
                        "kind": "information",
                        "protected_regions": [],
                        "annotations": [],
                    },
                ],
            )

            v001 = self._run(root, manifest)
            first_manifest_hash = sha256(v001 / "manifest.json")
            first_annotated_hash = sha256(v001 / "annotated" / "01-supported.png")
            v002 = self._run(root, manifest)
            with self.assertRaises(FileExistsError):
                self._run(root, manifest, version="v001")

            self.assertEqual(v001.name, "v001")
            self.assertEqual(v002.name, "v002")
            self.assertNotEqual(v001, v002)
            self.assertEqual(sha256(v001 / "manifest.json"), first_manifest_hash)
            self.assertEqual(
                sha256(v001 / "annotated" / "01-supported.png"),
                first_annotated_hash,
            )
            self.assertEqual(sha256(source), source_hash, "输入原图不得被覆盖")

            source_copy = v001 / "sources" / "supported.png"
            self.assertTrue(source_copy.is_file())
            self.assertEqual(sha256(source_copy), source_hash)

            with Image.open(v001 / "annotated" / "01-supported.png") as image:
                self.assertNotIn("Author", image.info)
                self.assertNotIn("Comment", image.info)
                self.assertNotIn("exif", image.info)

            resolved = read_json(_resolved_plan_path(v001))
            supported_asset = _asset_map(resolved)["supported"]
            self.assertEqual(supported_asset.get("source_archive"), "sources/supported.png")
            self.assertEqual(supported_asset.get("source_sha256"), source_hash)
            unsupported_asset = _asset_map(resolved)["unsupported"]
            self.assertEqual(unsupported_asset.get("render_status"), "unsupported")
            self.assertFalse(
                (v001 / "annotated" / "02-unsupported.png").exists(),
                "不支持格式不能冒充已标注",
            )
            unsupported_validation = self.validator_module.validate_project(v001)
            self.assertEqual(unsupported_validation.get("status"), "blocked")
            self.assertIs(
                unsupported_validation.get("deterministic_passed"),
                False,
            )
            self.assertIn(
                "unsupported_asset",
                _blocker_codes(unsupported_validation),
            )

    def test_validator_detects_pixel_change_outside_annotation_mask(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-tamper-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "tamper-source.png")
            manifest = self._manifest(
                job_id="annotated-pixel-tamper",
                assets=[
                    {
                        "id": "tamper",
                        "source": str(source),
                        "output_name": "tamper.png",
                        "kind": "information",
                        "protected_regions": [],
                        "annotations": [
                            {
                                "id": "confirm-result",
                                "rect": [115, 260, 535, 415],
                                "semantic_role": "single",
                                "callout": {
                                    "text": "核对演示结果",
                                    "marker": "none",
                                    "anchor": "auto",
                                },
                            }
                        ],
                    }
                ],
            )
            version_dir = self._run(root, manifest)
            baseline = self.validator_module.validate_project(version_dir)
            self.assertEqual(baseline.get("status"), "awaiting_manual_review", baseline)
            self.assertTrue(baseline.get("deterministic_passed"), baseline)

            relative = "annotated/tamper.png"
            annotated = version_dir / relative
            with Image.open(annotated) as opened:
                point = (opened.width - 2, opened.height - 2)
            _mutate_rgb_pixel(annotated, point)
            _refresh_manifest_integrity_record(version_dir, relative)

            validation = self.validator_module.validate_project(version_dir)
            codes = _blocker_codes(validation)
            self.assertEqual(validation.get("status"), "blocked")
            self.assertIn("annotated_unexpected_pixel_change", codes)
            self.assertNotIn(
                "annotated_hash_mismatch",
                codes,
                "已刷新普通哈希，必须由允许变化蒙版门禁抓到篡改",
            )
            self.assertNotIn("file_hash_mismatch", codes)

    def test_validator_detects_overview_placement_pixel_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-board-tamper-") as temp:
            root = Path(temp)
            sources = [
                make_synthetic_screenshot(
                    root / f"board-source-{index}.png",
                    size=(520, 390),
                )
                for index in range(1, 3)
            ]
            manifest = self._manifest(
                job_id="overview-pixel-tamper",
                overview={
                    "mode": "grid",
                    "columns": 4,
                    "max_pixels": 40_000_000,
                },
                assets=[
                    {
                        "id": f"board-{index}",
                        "source": str(source),
                        "output_name": f"board-{index}.png",
                        "kind": "information",
                        "protected_regions": [],
                        "annotations": [],
                    }
                    for index, source in enumerate(sources, start=1)
                ],
            )
            version_dir = self._run(root, manifest)
            baseline = self.validator_module.validate_project(version_dir)
            self.assertEqual(baseline.get("status"), "awaiting_manual_review", baseline)
            self.assertTrue(baseline.get("deterministic_passed"), baseline)

            output_manifest = read_json(version_dir / "manifest.json")
            page = output_manifest["overview"]["pages"][0]
            placement = page["placements"][0]
            x1, y1, x2, y2 = [int(value) for value in placement["rect"]]
            point = (x1 + (x2 - x1) // 2, y1 + (y2 - y1) // 2)
            relative = str(page["path"])
            overview_path = version_dir / relative
            _mutate_rgb_pixel(overview_path, point)
            _refresh_manifest_integrity_record(version_dir, relative)

            validation = self.validator_module.validate_project(version_dir)
            codes = _blocker_codes(validation)
            self.assertEqual(validation.get("status"), "blocked")
            self.assertIn("overview_content_mismatch", codes)
            self.assertNotIn(
                "file_hash_mismatch",
                codes,
                "已刷新普通哈希，必须由 placement 内容复算抓到篡改",
            )

    def test_validator_blocks_malformed_numeric_fields_without_exceptions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-numeric-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(
                root / "numeric-source.png",
                size=(520, 390),
            )
            version_dir = self._run(
                root,
                self._manifest(
                    job_id="numeric-validation",
                    assets=[
                        {
                            "id": "numeric",
                            "source": str(source),
                            "output_name": "numeric.png",
                            "kind": "information",
                            "protected_regions": [],
                            "annotations": [
                                {
                                    "id": "numeric-callout",
                                    "rect": [80, 145, 440, 305],
                                    "semantic_role": "single",
                                    "callout": {
                                        "text": "核对演示结果",
                                        "marker": "none",
                                        "anchor": "auto",
                                    },
                                }
                            ],
                        }
                    ],
                    overview={
                        "mode": "grid",
                        "columns": 4,
                        "max_pixels": 40_000_000,
                        "max_page_height": 12_000,
                    },
                ),
            )
            baseline = self.validator_module.validate_project(version_dir)
            self.assertEqual(baseline.get("status"), "awaiting_manual_review", baseline)

            original_manifest = read_json(version_dir / "manifest.json")
            original_resolved = read_json(_resolved_plan_path(version_dir))
            cases = [
                (
                    "overview.max_pixels object",
                    lambda document: document["overview"].__setitem__(
                        "max_pixels",
                        {"not": "a number"},
                    ),
                    "overview_max_pixels_invalid",
                ),
                (
                    "overview.max_page_height infinity",
                    lambda document: document["overview"].__setitem__(
                        "max_page_height",
                        float("inf"),
                    ),
                    "overview_max_page_height_invalid",
                ),
                (
                    "overview.columns string",
                    lambda document: document["overview"].__setitem__(
                        "columns",
                        "four",
                    ),
                    "overview_columns_invalid",
                ),
                (
                    "overview placement.rect infinity",
                    lambda document: document["overview"]["pages"][0]["placements"][
                        0
                    ].__setitem__("rect", [0, 0, float("inf"), 10]),
                    "overview_rect_invalid",
                ),
                (
                    "overview placement.rendered_size infinity",
                    lambda document: document["overview"]["pages"][0]["placements"][
                        0
                    ].__setitem__("rendered_size", [float("inf"), 10]),
                    "overview_content_unverifiable",
                ),
                (
                    "resolved style numeric infinity",
                    lambda document: document["style"].__setitem__(
                        "line_width",
                        float("inf"),
                    ),
                    "resolved_style_unverifiable",
                ),
                (
                    "resolved annotation.rect infinity",
                    lambda document: document["assets"][0]["annotations"][0].__setitem__(
                        "rect",
                        [0, 0, float("inf"), 10],
                    ),
                    "annotation_rect_invalid",
                ),
            ]
            for label, mutate, expected_code in cases:
                with self.subTest(field=label):
                    write_json(version_dir / "manifest.json", original_manifest)
                    write_json(_resolved_plan_path(version_dir), original_resolved)
                    _mutate_manifest_and_resolved_plan(version_dir, mutate)

                    validation = self.validator_module.validate_project(version_dir)
                    self.assertIsInstance(validation, dict)
                    self.assertEqual(validation.get("status"), "blocked", validation)
                    self.assertIn(expected_code, _blocker_codes(validation), validation)

    def test_lanczos_resampling_falls_back_without_resampling_enum(self) -> None:
        rendering = self.rendering_module
        with mock.patch.object(rendering.Image, "Resampling", None, create=True):
            selected = rendering.lanczos_resample_filter()
            self.assertEqual(selected, getattr(rendering.Image, "LANCZOS"))
        thumbnail = rendering._thumbnail(
            Image.new("RGB", (64, 32), "#FFFFFF"),
            (16, 16),
        )
        self.assertEqual(thumbnail.size, (16, 8))

    def test_validator_enforces_privacy_upstream_audit_and_manual_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v2-privacy-") as temp:
            root = Path(temp)
            original = make_synthetic_screenshot(root / "privacy-original.png")
            redacted = make_synthetic_screenshot(root / "privacy-redacted.png")
            _mutate_rgb_pixel(redacted, (12, 112))
            self.assertNotEqual(sha256(original), sha256(redacted))
            audit = write_json(
                root / "privacy-audit.json",
                {
                    "schema_version": "1.0",
                    "passed": True,
                    "source_sha256": sha256(original),
                    "output_sha256": sha256(redacted),
                    "checks": {"sensitive_content_absent": True},
                    "reason": "不得复制进安全摘要的测试理由",
                    "source": "/private/input/不得复制.png",
                },
            )
            asset = {
                "id": "privacy-checked",
                "source": str(redacted),
                "output_name": "privacy-checked.png",
                "kind": "information",
                "protected_regions": [],
                "annotations": [
                    {
                        "id": "confirm-synthetic-result",
                        "rect": [115, 260, 535, 415],
                        "semantic_role": "single",
                        "callout": {
                            "text": "核对演示结果",
                            "marker": "none",
                            "anchor": "auto",
                        },
                    }
                ],
            }
            manifest = self._manifest(
                job_id="privacy-audit",
                assets=[asset],
                privacy={
                    "status": "passed",
                    "required": True,
                    "upstream_audit": str(audit),
                },
            )
            version_dir = self._run(root, manifest)
            validate_project = self.validator_module.validate_project
            validation = validate_project(version_dir)
            self.assertIsInstance(validation, dict)
            self.assertEqual(validation.get("status"), "awaiting_manual_review")
            self.assertIs(validation.get("passed"), False)
            self.assertIs(validation.get("deterministic_passed"), True)
            self.assertIs(
                validation.get("privacy_audit_passed"),
                True,
                validation,
            )
            self.assertIs(
                validation.get("checks", {}).get("privacy_upstream_audit"),
                True,
                validation,
            )

            audit_summary = read_json(
                version_dir / "qa" / "privacy-upstream-audit.json"
            )
            privacy_report = audit_summary["reports"]["privacy-checked"]
            self.assertEqual(privacy_report.get("source_sha256"), sha256(original))
            self.assertEqual(privacy_report.get("output_sha256"), sha256(redacted))
            serialized_summary = json.dumps(audit_summary, ensure_ascii=False)
            self.assertNotIn("reason", serialized_summary)
            self.assertNotIn("/private/input", serialized_summary)

            auto_summary = read_json(version_dir / "qa" / "auto-summary.json")
            manual_review = read_json(
                version_dir / "qa" / "manual-review-template.json"
            )
            self.assertNotEqual(auto_summary.get("status"), "passed")
            self.assertEqual(
                manual_review.get("status"),
                "awaiting_manual_review",
            )
            self.assertTrue(manual_review.get("manual_review_required"))
            self.assertNotEqual(manual_review.get("decision"), "passed")
            self.assertIsNone(manual_review.get("reviewer"))
            self.assertIsNone(manual_review.get("reviewed_at"))
            self.assertEqual(manual_review.get("decision"), "unchecked")
            self.assertEqual(manual_review.get("notes"), "unchecked")
            self.assertIsInstance(manual_review.get("blockers"), list)
            manual_annotation = manual_review["assets"][0]["annotations"][0]
            for field in (
                "alignment",
                "readability",
                "non_obstruction",
                "decision",
                "notes",
            ):
                self.assertEqual(manual_annotation.get(field), "unchecked")

            forged_manual_review = copy.deepcopy(manual_review)
            forged_manual_review["status"] = "passed"
            forged_manual_review["decision"] = "passed"
            write_json(
                version_dir / "qa" / "manual-review-template.json",
                forged_manual_review,
            )
            forged_validation = validate_project(version_dir)
            self.assertEqual(forged_validation.get("status"), "blocked")
            self.assertIs(forged_validation.get("passed"), False)
            self.assertIn(
                "manual_review_template_state_invalid",
                _blocker_codes(forged_validation),
                "没有 reviewer/reviewed_at 的伪造 passed 必须被 validator 阻断",
            )

            missing_audit_manifest = copy.deepcopy(manifest)
            missing_audit_manifest["job_id"] = "privacy-audit-missing"
            missing_audit_manifest["privacy"]["upstream_audit"] = str(
                root / "missing-privacy-audit.json"
            )
            missing_dir = self._run(root / "missing", missing_audit_manifest)
            missing_validation = validate_project(missing_dir)
            codes = _blocker_codes(missing_validation)
            self.assertIn("privacy_audit_missing", codes)
            self.assertIs(missing_validation.get("privacy_audit_passed"), False)
            self.assertFalse(
                missing_validation.get("passed", False),
                "缺少上游脱敏审计时 validator 不得通过",
            )

            wrong_audit = write_json(
                root / "wrong-privacy-audit.json",
                {
                    "schema_version": "1.0",
                    "passed": True,
                    "source_sha256": sha256(original),
                    "output_sha256": "0" * 64,
                },
            )
            wrong_hash_manifest = copy.deepcopy(manifest)
            wrong_hash_manifest["job_id"] = "privacy-audit-wrong-hash"
            wrong_hash_manifest["privacy"]["upstream_audit"] = str(wrong_audit)
            wrong_hash_dir = self._run(root / "wrong-hash", wrong_hash_manifest)
            wrong_hash_validation = validate_project(wrong_hash_dir)
            self.assertIn(
                "privacy_audit_hash_mismatch",
                _blocker_codes(wrong_hash_validation),
            )
            self.assertIs(
                wrong_hash_validation.get("privacy_audit_passed"),
                False,
            )

            missing_output_manifest = copy.deepcopy(manifest)
            missing_output_manifest["job_id"] = "privacy-audit-output-missing"
            missing_output_manifest["privacy"]["upstream_audit"] = {
                "passed": True,
                "source_sha256": sha256(original),
            }
            missing_output_dir = self._run(
                root / "output-missing",
                missing_output_manifest,
            )
            missing_output_validation = validate_project(missing_output_dir)
            self.assertIn(
                "privacy_audit_output_hash_missing",
                _blocker_codes(missing_output_validation),
            )

            failed_report_manifest = copy.deepcopy(manifest)
            failed_report_manifest["job_id"] = "privacy-audit-report-failed"
            failed_report_manifest["privacy"]["upstream_audit"] = {
                "passed": False,
                "source_sha256": sha256(original),
                "output_sha256": sha256(redacted),
            }
            failed_report_dir = self._run(
                root / "report-failed",
                failed_report_manifest,
            )
            failed_report_validation = validate_project(failed_report_dir)
            self.assertIn(
                "privacy_audit_not_passed",
                _blocker_codes(failed_report_validation),
            )

    def test_privacy_multi_asset_reports_bind_each_redacted_output(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="screenshot-annotator-v2-privacy-multi-"
        ) as temp:
            root = Path(temp)
            originals = [
                make_synthetic_screenshot(root / f"original-{index}.png")
                for index in (1, 2)
            ]
            redacted = [
                make_synthetic_screenshot(root / f"redacted-{index}.png")
                for index in (1, 2)
            ]
            _mutate_rgb_pixel(redacted[0], (15, 115))
            _mutate_rgb_pixel(redacted[1], (16, 116))
            assets = [
                {
                    "id": f"privacy-{index}",
                    "source": str(redacted[index - 1]),
                    "output_name": f"privacy-{index}.png",
                    "kind": "information",
                    "protected_regions": [],
                    "annotations": [
                        {
                            "id": f"review-{index}",
                            "rect": [115, 260, 535, 415],
                            "semantic_role": "single",
                            "callout": {
                                "text": "核对演示结果",
                                "marker": "none",
                                "anchor": "auto",
                            },
                        }
                    ],
                }
                for index in (1, 2)
            ]
            first_report = write_json(
                root / "privacy-1-audit.json",
                {
                    "passed": True,
                    "source_sha256": sha256(originals[0]),
                    "output_sha256": sha256(redacted[0]),
                    "checks": {"sensitive_content_absent": True},
                    "reason": "不能进入摘要",
                    "output_path": "/private/redacted-1.png",
                },
            )
            second_report = {
                "passed": True,
                "source_sha256": sha256(originals[1]),
                "output_sha256": sha256(redacted[1]),
                "checks": {"sensitive_content_absent": True},
                "reason": "同样不能进入摘要",
            }
            manifest = self._manifest(
                job_id="privacy-multi",
                assets=assets,
                privacy={
                    "status": "passed",
                    "required": True,
                    "watermark": "preserve_unknown",
                    "authorized_watermark_removal": False,
                    "upstream_audits": {
                        "privacy-1": str(first_report),
                        "privacy-2": second_report,
                    },
                },
            )
            version_dir = self._run(root, manifest)
            validation = self.validator_module.validate_project(version_dir)
            self.assertEqual(
                validation.get("status"),
                "awaiting_manual_review",
                validation,
            )
            self.assertIs(validation.get("privacy_audit_passed"), True, validation)
            output_manifest = read_json(version_dir / "manifest.json")
            output_privacy = output_manifest["privacy"]
            self.assertNotIn("upstream_audits", output_privacy)
            self.assertNotIn("source_sha256", output_privacy)
            summary = read_json(version_dir / "qa" / "privacy-upstream-audit.json")
            self.assertEqual(set(summary["reports"]), {"privacy-1", "privacy-2"})
            for index in (1, 2):
                report = summary["reports"][f"privacy-{index}"]
                self.assertEqual(
                    report.get("source_sha256"),
                    sha256(originals[index - 1]),
                )
                self.assertEqual(
                    report.get("output_sha256"),
                    sha256(redacted[index - 1]),
                )
            serialized = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn("reason", serialized)
            self.assertNotIn("/private/", serialized)

    def test_privacy_source_hash_can_never_release_unredacted_asset(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="screenshot-annotator-v2-privacy-original-"
        ) as temp:
            root = Path(temp)
            original = make_synthetic_screenshot(root / "original.png")
            redacted = make_synthetic_screenshot(root / "redacted.png")
            _mutate_rgb_pixel(redacted, (18, 118))
            asset = {
                "id": "must-use-redacted-output",
                "source": str(original),
                "output_name": "must-use-redacted-output.png",
                "kind": "information",
                "protected_regions": [],
                "annotations": [
                    {
                        "id": "review-result",
                        "rect": [115, 260, 535, 415],
                        "semantic_role": "single",
                        "callout": {
                            "text": "核对演示结果",
                            "marker": "none",
                            "anchor": "auto",
                        },
                    }
                ],
            }
            manifest = self._manifest(
                job_id="privacy-original-must-block",
                assets=[asset],
                privacy={
                    "status": "passed",
                    "required": True,
                    "source_sha256": sha256(original),
                    "upstream_audit": {
                        "passed": True,
                        "source_sha256": sha256(original),
                        "output_sha256": sha256(redacted),
                    },
                },
            )
            version_dir = self._run(root, manifest)
            generated = read_json(version_dir / "manifest.json")
            self.assertEqual(generated.get("status"), "blocked", generated)
            self.assertIn(
                "privacy_audit_hash_mismatch",
                _blocker_codes(generated),
                "source_sha256 即使绑定当前原图，也不能替代 output_sha256 放行",
            )
            validation = self.validator_module.validate_project(version_dir)
            self.assertEqual(validation.get("status"), "blocked", validation)
            self.assertIn(
                "privacy_audit_hash_mismatch",
                _blocker_codes(validation),
                validation,
            )
            self.assertIs(validation.get("privacy_audit_passed"), False)

    def test_manifest_must_explicitly_declare_privacy_object(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="screenshot-annotator-v2-privacy-required-object-"
        ) as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            asset = {
                "id": "explicit-privacy",
                "source": str(source),
                "output_name": "explicit-privacy.png",
                "kind": "information",
                "protected_regions": [],
                "annotations": [],
            }
            manifest = self._manifest(job_id="privacy-object-missing", assets=[asset])
            manifest.pop("privacy")
            with self.assertRaisesRegex(ValueError, "显式提供 privacy"):
                self._run(root, manifest)

    def test_watermark_removal_requires_bound_deterministic_audit(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="screenshot-annotator-v2-watermark-audit-"
        ) as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "watermark-removed.png")
            asset = {
                "id": "watermark-removed",
                "source": str(source),
                "output_name": "watermark-removed.png",
                "kind": "information",
                "protected_regions": [],
                "annotations": [
                    {
                        "id": "review-watermark-result",
                        "rect": [115, 260, 535, 415],
                        "semantic_role": "single",
                        "callout": {
                            "text": "核对演示结果",
                            "marker": "none",
                            "anchor": "auto",
                        },
                    }
                ],
            }
            missing_evidence_manifest = self._manifest(
                job_id="watermark-missing-evidence",
                assets=[asset],
                privacy={
                    "status": "not_required",
                    "required": False,
                    "watermark": "removed",
                    "authorized_watermark_removal": True,
                },
            )
            missing_dir = self._run(root / "missing", missing_evidence_manifest)
            generated = read_json(missing_dir / "manifest.json")
            self.assertIn("watermark_audit_missing", _blocker_codes(generated))
            validation = self.validator_module.validate_project(missing_dir)
            self.assertEqual(validation.get("status"), "blocked", validation)
            self.assertIn(
                "watermark_audit_missing",
                _blocker_codes(validation),
                validation,
            )

            proved_manifest = copy.deepcopy(missing_evidence_manifest)
            proved_manifest["job_id"] = "watermark-proved"
            proved_manifest["privacy"]["watermark_audit"] = {
                "passed": True,
                "authorized": True,
                "deterministic": True,
                "output_sha256": sha256(source),
                "outside_changed_pixels": 0,
                "reason": "不得进入摘要",
                "output_path": "/private/watermark-removed.png",
            }
            proved_dir = self._run(root / "proved", proved_manifest)
            proved_validation = self.validator_module.validate_project(proved_dir)
            self.assertEqual(
                proved_validation.get("status"),
                "awaiting_manual_review",
                proved_validation,
            )
            watermark_summary = read_json(
                proved_dir / "qa" / "watermark-audit.json"
            )
            serialized = json.dumps(watermark_summary, ensure_ascii=False)
            self.assertNotIn("reason", serialized)
            self.assertNotIn("/private/", serialized)


if __name__ == "__main__":
    unittest.main()
