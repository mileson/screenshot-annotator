from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from suggest_target_rect import suggest_bottom_nav, suggest_color_component, suggest_row


class SceneTemplateTests(unittest.TestCase):
    def test_bottom_nav_uses_complete_equal_width_cell(self) -> None:
        result = suggest_bottom_nav((20, 200, 420, 300), index=3, count=4)
        self.assertEqual(result["rect"], [320, 200, 420, 300])
        self.assertGreaterEqual(result["confidence"], 0.7)
        self.assertIn("requires_visual_container_review", result["evidence"])

    def test_color_component_finds_only_action_button(self) -> None:
        image = Image.new("RGB", (300, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((40, 80, 140, 130), radius=8, fill="#16A34A")
        draw.rounded_rectangle((165, 80, 270, 130), radius=8, fill="#D1D5DB")
        result = suggest_color_component(image, (80, 100))
        left, top, right, bottom = result["rect"]
        self.assertLessEqual(left, 45)
        self.assertGreaterEqual(right, 135)
        self.assertLess(right, 165)
        self.assertLessEqual(top, 85)
        self.assertGreaterEqual(bottom, 125)

    def test_row_scene_respects_declared_container_width(self) -> None:
        image = Image.new("RGB", (400, 260), "white")
        draw = ImageDraw.Draw(image)
        draw.line((30, 80, 370, 80), fill="#9CA3AF", width=2)
        draw.line((30, 160, 370, 160), fill="#9CA3AF", width=2)
        draw.rectangle((80, 105, 180, 130), fill="#111827")
        result = suggest_row(image, (80, 100, 185, 135), (30, 60, 370, 180))
        self.assertEqual(result["rect"][0], 30)
        self.assertEqual(result["rect"][2], 370)
        self.assertLessEqual(result["rect"][1], 100)
        self.assertGreaterEqual(result["rect"][3], 135)


if __name__ == "__main__":
    unittest.main()
