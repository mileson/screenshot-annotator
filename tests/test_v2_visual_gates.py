from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from scripts.screenshot_annotator_v2.common import normalize_style
from scripts.screenshot_annotator_v2.common import STYLE
from scripts.screenshot_annotator_v2.rendering import (
    build_annotation_crops,
    build_overview,
    resolve_placements,
)


def _annotation(
    annotation_id: str,
    rect: tuple[int, int, int, int],
    *,
    label_rect: tuple[int, int, int, int] | None = None,
) -> dict:
    return {
        "id": annotation_id,
        "asset_id": "screen",
        "rect": rect,
        "semantic_role": "single",
        "callout": {
            "text": "点击确认",
            "anchor": "manual" if label_rect is not None else "auto",
            "label_rect": label_rect,
            "resolved_marker": None,
        },
    }


class VisualGateTests(unittest.TestCase):
    def test_human_style_profile_matches_runtime_presets(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("维护期未安装 PyYAML，运行时不依赖它。")
        profile_path = (
            Path(__file__).resolve().parents[1] / "data" / "annotation-style.yaml"
        )
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        orange = profile["presets"]["action-orange-v2"]
        red = profile["presets"]["action-red-v2"]
        self.assertEqual(profile["default_preset"], "action-orange-v2")
        self.assertEqual(orange["rect"]["color"], STYLE["color"])
        self.assertEqual(orange["rect"]["line_width"], STYLE["line_width"])
        self.assertEqual(orange["rect"]["radius"], STYLE["rect_radius"])
        self.assertEqual(
            orange["callout"]["font_size"],
            STYLE["callout_font_size"],
        )
        self.assertEqual(
            orange["callout"]["max_manual_distance"],
            STYLE["max_manual_label_distance"],
        )
        self.assertEqual(
            red["rect"]["color"],
            normalize_style("action-red-v2")["color"],
        )

    def test_manual_label_cannot_detach_or_cover_another_target(self) -> None:
        style = normalize_style(
            {
                "preset": "action-orange-v2",
                "callout": {"max_manual_distance": 80},
            }
        )
        annotations = [
            _annotation("first", (20, 20, 120, 100), label_rect=(430, 20, 590, 90)),
            _annotation("second", (430, 20, 590, 90)),
        ]
        _resolved, blockers, _warnings = resolve_placements(
            annotations,
            [],
            (640, 360),
            style,
        )
        codes = {item["code"] for item in blockers}
        self.assertIn("manual_label_detached", codes)
        self.assertIn("unrelated_target_collision", codes)

    def test_overview_reports_unreadable_label_scale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "tall.png"
            Image.new("RGB", (400, 4000), "white").save(source)
            result = build_overview(
                [
                    {
                        "asset_id": "tall",
                        "title": "超长页面",
                        "path": source,
                        "source_size": [400, 4000],
                        "label_font_size": 24,
                    }
                ],
                root / "overview",
                {
                    "mode": "vertical",
                    "max_pixels": 250_000,
                    "max_page_height": 200,
                    "min_label_font_px": 14,
                },
            )
            self.assertEqual(
                [item["code"] for item in result["readability_issues"]],
                ["overview_label_unreadable"],
            )
            placement = result["pages"][0]["placements"][0]
            self.assertLess(placement["effective_label_font_px"], 14)

    def test_annotation_crop_matches_annotated_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            annotated = root / "annotated.png"
            image = Image.new("RGB", (320, 240), "#F5F5F4")
            for x in range(40, 281):
                for y in range(30, 211):
                    image.putpixel((x, y), (x % 255, y % 255, (x + y) % 255))
            image.save(annotated)
            annotation = {
                "id": "confirm",
                "rect": [60, 80, 180, 140],
                "callout": {"label_rect": [170, 130, 280, 190]},
            }
            records = build_annotation_crops(
                annotated,
                "screen",
                [annotation],
                root / "crops",
                padding=10,
            )
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["crop_rect"], [50, 70, 290, 200])
            with Image.open(annotated) as opened:
                expected = opened.convert("RGBA").crop(tuple(record["crop_rect"]))
            with Image.open(root / "crops" / record["name"]) as actual:
                difference = ImageChops.difference(expected, actual.convert("RGBA"))
                self.assertIsNone(difference.getbbox())
                self.assertEqual(actual.info, {})


if __name__ == "__main__":
    unittest.main()
