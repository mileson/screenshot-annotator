from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests.support import SKILL_ROOT, make_synthetic_screenshot, sha256, write_json


class V1CompatibilityTests(unittest.TestCase):
    def test_legacy_rect_and_badge_plan_still_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v1-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            source_hash = sha256(source)
            plan = write_json(
                root / "legacy-plan.json",
                {
                    "style": {
                        "color": "#E62323",
                        "line_width": 7,
                        "badge_radius": 28,
                    },
                    "annotations": [
                        {
                            "id": "legacy-step",
                            "rect": [100, 245, 550, 430],
                            "badge": {"text": "1", "center": [82, 225]},
                        }
                    ],
                },
            )
            output = root / "annotated.png"
            qa_dir = root / "qa"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_ROOT / "scripts" / "annotate_screenshot.py"),
                    "--input",
                    str(source),
                    "--plan",
                    str(plan),
                    "--output",
                    str(output),
                    "--qa-dir",
                    str(qa_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output.is_file())
            self.assertTrue((qa_dir / "qa-contact-sheet.png").is_file())
            self.assertTrue((qa_dir / "qa-summary.json").is_file())
            review = json.loads((qa_dir / "review.json").read_text(encoding="utf-8"))
            self.assertTrue(review["deterministic_passed"])
            self.assertTrue(review["manual_review_required"])
            self.assertEqual(review["status"], "needs_manual_review")
            self.assertEqual(sha256(source), source_hash, "v1 不得覆盖原截图")
            with Image.open(source) as source_image, Image.open(output) as output_image:
                self.assertEqual(output_image.size, source_image.size)
                self.assertEqual(output_image.convert("RGB").getpixel((100, 270)), (230, 35, 35))

    def test_legacy_rejects_source_overwrite_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-annotator-v1-safety-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            source_hash = sha256(source)
            plan = write_json(
                root / "legacy-plan.json",
                {
                    "annotations": [
                        {
                            "id": "legacy-step",
                            "rect": [100, 245, 550, 430],
                            "badge": {"text": "1", "center": [82, 225]},
                        }
                    ],
                },
            )
            command = [
                sys.executable,
                str(SKILL_ROOT / "scripts" / "annotate_screenshot.py"),
                "--input",
                str(source),
                "--plan",
                str(plan),
            ]

            same_path = subprocess.run(
                [*command, "--output", str(source)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(same_path.returncode, 0)
            self.assertIn("拒绝覆盖源图", same_path.stderr)
            self.assertEqual(sha256(source), source_hash)

            existing = root / "existing.png"
            existing.write_bytes(b"keep-existing-output")
            existing_hash = sha256(existing)
            existing_path = subprocess.run(
                [*command, "--output", str(existing)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(existing_path.returncode, 0)
            self.assertIn("拒绝覆盖", existing_path.stderr)
            self.assertEqual(sha256(existing), existing_hash)


if __name__ == "__main__":
    unittest.main()
