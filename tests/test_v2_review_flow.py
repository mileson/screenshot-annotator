from __future__ import annotations

import copy
import importlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.support import (
    load_script_module,
    make_synthetic_screenshot,
    read_json,
    sha256,
    write_json,
)


def _codes(summary: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for item in summary.get("blockers", []):
        if isinstance(item, dict) and item.get("code"):
            values.add(str(item["code"]))
        elif isinstance(item, str):
            values.add(item)
    return values


class V2ReviewAndFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.batch = load_script_module(
            "annotate_batch.py",
            "screenshot_annotator_v2_review_flow_batch",
        )
        cls.validator_entry = load_script_module(
            "validate_annotation_project.py",
            "screenshot_annotator_v2_review_flow_validator",
        )
        cls.engine = importlib.import_module("screenshot_annotator_v2.engine")
        cls.validator = importlib.import_module("screenshot_annotator_v2.validator")

    def _asset(
        self,
        source: Path,
        *,
        asset_id: str = "screen-a",
        annotation_id: str = "action-a",
        semantic_role: str = "single",
    ) -> dict[str, Any]:
        return {
            "id": asset_id,
            "source": str(source),
            "output_name": f"{asset_id}.png",
            "kind": "sequential_action",
            "protected_regions": [],
            "annotations": [
                {
                    "id": annotation_id,
                    "rect": [115, 260, 535, 415],
                    "semantic_role": semantic_role,
                    "callout": {
                        "text": "核对演示结果",
                        "marker": "none" if semantic_role == "single" else "auto",
                        "anchor": "auto",
                    },
                }
            ],
        }

    def _manifest(
        self,
        *,
        job_id: str,
        assets: list[dict[str, Any]],
        edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "job_id": job_id,
            "style": {"preset": "action-orange-v2"},
            "privacy": {
                "source_handling": "read_only_copy",
                "sensitive_data": "not_present_synthetic_fixture",
                "watermark": "preserve_unknown",
                "authorized_watermark_removal": False,
            },
            "flow": {
                "type": "linear",
                "sort": "declared_order",
                "order": [str(asset["id"]) for asset in assets],
                "edges": edges or [],
            },
            "overview": {"mode": "vertical", "max_pixels": 40_000_000},
            "assets": assets,
        }

    def _run(self, root: Path, manifest: dict[str, Any]) -> Path:
        plan = write_json(root / "input-plan.json", manifest)
        return Path(self.batch.run_batch(plan, root / "output"))

    @staticmethod
    def _approved(
        review: dict[str, Any],
        *,
        review_index: int = 2,
    ) -> dict[str, Any]:
        result = copy.deepcopy(review)
        result.update(
            {
                "status": "passed",
                "review_index": review_index,
                "reviewer": "Synthetic Reviewer",
                "reviewed_at": "2026-07-31T12:00:00+08:00",
                "decision": "passed",
                "notes": "Synthetic manual review completed.",
                "blockers": [],
            }
        )
        for asset in result["assets"]:
            asset.update(
                {
                    "decision": "passed",
                    "notes": "Synthetic asset checked.",
                }
            )
            for annotation in asset["annotations"]:
                annotation.update(
                    {
                        "alignment": "passed",
                        "readability": "passed",
                        "non_obstruction": "passed",
                        "decision": "passed",
                        "notes": "Synthetic fixture checked.",
                    }
                )
        return result

    def test_engine_and_validator_double_check_flow_graph_contract(self) -> None:
        order = ["a", "b", "c"]
        roles = {
            "a": {"go": "sequence", "choice": "alternative"},
            "b": {"go": "sequence"},
            "c": {"go": "sequence"},
        }
        output_assets = [
            {
                "id": asset_id,
                "annotations": [
                    {"id": annotation_id, "semantic_role": role}
                    for annotation_id, role in roles[asset_id].items()
                ],
            }
            for asset_id in order
        ]
        cases = {
            "flow_callout_required": [
                {"from": "a", "to": "b", "kind": "next"}
            ],
            "flow_from_missing": [
                {"from": "missing", "to": "b", "kind": "next", "callout_id": "go"}
            ],
            "flow_callout_missing": [
                {"from": "a", "to": "b", "kind": "next", "callout_id": "missing"}
            ],
            "flow_edge_duplicate": [
                {"from": "a", "to": "b", "kind": "next", "callout_id": "go"},
                {"from": "a", "to": "b", "kind": "next", "callout_id": "go"},
            ],
            "flow_callout_fanout": [
                {"from": "a", "to": "b", "kind": "next", "callout_id": "go"},
                {"from": "a", "to": "c", "kind": "next", "callout_id": "go"},
            ],
            "flow_self_loop": [
                {"from": "a", "to": "a", "kind": "next", "callout_id": "go"}
            ],
            "flow_cycle": [
                {"from": "a", "to": "b", "kind": "next", "callout_id": "go"},
                {"from": "b", "to": "c", "kind": "next", "callout_id": "go"},
                {"from": "c", "to": "a", "kind": "next", "callout_id": "go"},
            ],
            "flow_kind_role_mismatch": [
                {"from": "a", "to": "b", "kind": "next", "callout_id": "choice"}
            ],
        }

        for expected_code, edges in cases.items():
            with self.subTest(expected_code=expected_code):
                raw = {
                    "type": "linear",
                    "sort": "declared_order",
                    "order": order,
                    "edges": edges,
                }
                _, engine_blockers = self.engine._normalize_flow(raw, order, roles)
                self.assertIn(
                    expected_code,
                    {str(item.get("code")) for item in engine_blockers},
                )
                validator_blockers: list[dict[str, Any]] = []
                self.validator._validate_flow(
                    {"flow": raw, "assets": output_assets},
                    validator_blockers,
                )
                self.assertIn(
                    expected_code,
                    {str(item.get("code")) for item in validator_blockers},
                )

    def test_review_001_is_manifest_bound_and_valid_review_002_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-v2-review-pass-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            version = self._run(
                root,
                self._manifest(job_id="review-pass", assets=[self._asset(source)]),
            )
            manifest = read_json(version / "manifest.json")
            review_workspace = version.parent.parent / "reviews" / version.name
            first_review_path = review_workspace / "review-001.json"
            first_review = read_json(first_review_path)

            self.assertEqual(
                manifest["files"]["review_workspace"],
                f"reviews/{version.name}",
            )
            self.assertEqual(first_review["manifest_sha256"], sha256(version / "manifest.json"))
            awaiting = self.validator_entry.validate_project(version)
            self.assertEqual(awaiting["status"], "awaiting_manual_review")
            self.assertFalse(awaiting["passed"])

            write_json(review_workspace / "review-002.json", self._approved(first_review))
            approved = self.validator_entry.validate_project(version)
            self.assertEqual(approved["status"], "passed")
            self.assertTrue(approved["passed"])
            self.assertEqual(approved["manual_review_status"], "passed")

    def test_malformed_or_nonsequential_review_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-v2-review-invalid-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            version = self._run(
                root,
                self._manifest(job_id="review-invalid", assets=[self._asset(source)]),
            )
            workspace = version.parent.parent / "reviews" / version.name
            first = read_json(workspace / "review-001.json")
            malformed = self._approved(first)
            malformed["manifest_sha256"] = "0" * 64
            malformed["reviewed_at"] = "2026-07-31T12:00:00"
            malformed["assets"][0]["annotations"] = []
            write_json(workspace / "review-003.json", malformed)

            summary = self.validator_entry.validate_project(version)
            self.assertEqual(summary["status"], "blocked")
            self.assertFalse(summary["passed"])
            self.assertTrue(
                {
                    "review_sequence_gap",
                    "review_manifest_mismatch",
                    "review_time_invalid",
                    "review_annotation_coverage_mismatch",
                }.issubset(_codes(summary))
            )

    def test_manual_block_keeps_deterministic_result_and_asset_decision_is_required(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-v2-review-block-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            version = self._run(
                root,
                self._manifest(job_id="review-human-block", assets=[self._asset(source)]),
            )
            workspace = version.parent.parent / "reviews" / version.name
            first = read_json(workspace / "review-001.json")

            blocked = copy.deepcopy(first)
            blocked.update(
                {
                    "review_index": 2,
                    "status": "blocked",
                    "reviewer": "Synthetic Reviewer",
                    "reviewed_at": "2026-07-31T12:00:00+08:00",
                    "decision": "blocked",
                    "notes": "Synthetic visual issue.",
                    "blockers": [{"code": "visual_alignment"}],
                }
            )
            write_json(workspace / "review-002.json", blocked)
            summary = self.validator_entry.validate_project(version)
            self.assertEqual(summary["status"], "blocked")
            self.assertTrue(summary["deterministic_passed"])
            self.assertEqual(summary["manual_review_status"], "blocked")

            (workspace / "review-002.json").unlink()
            approved = self._approved(first)
            approved["assets"][0].pop("decision")
            write_json(workspace / "review-002.json", approved)
            missing_asset_decision = self.validator_entry.validate_project(version)
            self.assertIn("review_asset_state_invalid", _codes(missing_asset_decision))

    def test_recorded_blocker_stays_blocked_and_manual_status_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-v2-review-blocked-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            asset = self._asset(source, semantic_role="sequence")
            version = self._run(
                root,
                self._manifest(
                    job_id="review-blocked",
                    assets=[asset],
                    edges=[
                        {
                            "from": "screen-a",
                            "to": "screen-a",
                            "kind": "next",
                            "callout_id": "action-a",
                        }
                    ],
                ),
            )
            manifest = read_json(version / "manifest.json")
            workspace = version.parent.parent / "reviews" / version.name
            first = read_json(workspace / "review-001.json")

            self.assertEqual(manifest["status"], "blocked")
            self.assertEqual(manifest["manual_review_status"], "not_ready")
            self.assertEqual(first["status"], "not_ready")

            write_json(workspace / "review-002.json", self._approved(first))
            summary = self.validator_entry.validate_project(version)
            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(summary["manual_review_status"], "not_ready")
            self.assertIn("flow_self_loop", _codes(summary))

    def test_engine_refuses_to_overwrite_existing_review_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="screenshot-v2-review-immutable-") as temp:
            root = Path(temp)
            source = make_synthetic_screenshot(root / "source.png")
            manifest = self._manifest(
                job_id="review-immutable",
                assets=[self._asset(source)],
            )
            plan = write_json(root / "input-plan.json", manifest)
            workspace = root / "output" / "review-immutable" / "reviews" / "v001"
            workspace.mkdir(parents=True)
            sentinel = write_json(workspace / "sentinel.json", {"keep": True})

            with self.assertRaises(FileExistsError):
                self.batch.run_batch(plan, root / "output", version="v001")
            self.assertEqual(read_json(sentinel), {"keep": True})


if __name__ == "__main__":
    unittest.main()
