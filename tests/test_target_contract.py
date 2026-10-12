from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from screenshot_annotator_v2.engine import run_batch
from screenshot_annotator_v2.validator import validate_project


class TargetContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.image = self.root / "screen.png"
        canvas = Image.new("RGB", (420, 320), "white")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((20, 210, 400, 305), radius=30, fill="#F5F7FA", outline="#D8DCE3")
        for left in (30, 125, 220, 315):
            draw.ellipse((left + 25, 225, left + 55, 255), fill="#3B82F6")
            draw.rectangle((left + 20, 267, left + 62, 280), fill="#1F2937")
        canvas.save(self.image)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def plan(self, *, target_rect, exclude_rect=(220, 210, 315, 305)) -> dict:
        return {
            "schema_version": "2.0",
            "job_id": "target-contract-fixture",
            "style": {"preset": "action-orange-v2", "scale_with_image": False},
            "privacy": {
                "source_handling": "read_only_copy",
                "sensitive_data": "not_present_synthetic_fixture",
                "watermark": "preserve_unknown",
                "authorized_watermark_removal": False,
            },
            "flow": {"type": "linear", "sort": "declared_order", "order": ["nav"], "edges": []},
            "overview": {"mode": "grid", "columns": 4, "max_pixels": 40_000_000},
            "assets": [
                {
                    "id": "nav",
                    "source": str(self.image),
                    "output_name": "nav.png",
                    "title": "选择底部入口",
                    "kind": "sequential_action",
                    "protected_regions": [],
                    "annotations": [
                        {
                            "id": "tap-nav",
                            "rect": list(target_rect),
                            "semantic_role": "single",
                            "target_contract": {
                                "target_kind": "bottom_nav_item",
                                "boundary_tolerance_px": 0,
                                "must_include": [
                                    {"id": "container", "role": "container", "rect": [315, 210, 400, 305]},
                                    {"id": "icon", "role": "icon", "rect": [340, 225, 370, 255]},
                                    {"id": "label", "role": "label", "rect": [335, 267, 377, 280]},
                                ],
                                "must_exclude": [
                                    {"id": "adjacent", "role": "adjacent_tab", "rect": list(exclude_rect)}
                                ],
                            },
                            "callout": {"text": "点击我的", "marker": "none", "anchor": "manual", "label_rect": [170, 105, 410, 195]},
                        }
                    ],
                }
            ],
        }

    def run_plan(self, payload: dict, version: str) -> tuple[Path, dict]:
        plan_path = self.root / f"{version}.json"
        plan_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        version_dir = run_batch(plan_path, self.root / "output", version)
        return version_dir, validate_project(version_dir)

    def test_complete_visual_unit_passes(self) -> None:
        version_dir, validation = self.run_plan(self.plan(target_rect=(315, 210, 400, 305)), "v001")
        self.assertTrue(validation["deterministic_passed"], validation["blockers"])
        resolved = json.loads((version_dir / "plans/resolved-plan.json").read_text(encoding="utf-8"))
        contract = resolved["assets"][0]["annotations"][0]["target_contract"]
        self.assertTrue(contract["checks"]["passed"])

    def test_missing_container_edge_is_blocked(self) -> None:
        _, validation = self.run_plan(self.plan(target_rect=(340, 220, 400, 295)), "v002")
        codes = {item["code"] for item in validation["blockers"]}
        self.assertIn("target_contract_include_incomplete", codes)

    def test_adjacent_control_overlap_is_blocked(self) -> None:
        payload = self.plan(target_rect=(300, 210, 400, 305), exclude_rect=(290, 210, 320, 305))
        _, validation = self.run_plan(payload, "v003")
        codes = {item["code"] for item in validation["blockers"]}
        self.assertIn("target_contract_exclude_overlap", codes)


if __name__ == "__main__":
    unittest.main()
