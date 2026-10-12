# Release validation / 发布验证

Verified on 2026-10-12 using the existing Python 3.9 runtime and Pillow 11.3.0. No server, browser or new dependency environment was started.

- `python3 -m unittest discover -s tests -t .`: **38 tests passed**, 9.584 seconds. Synthetic fixtures cover v1 compatibility, v2 batch behavior, scene contracts, review integrity and visual gates.
- `python3 examples/run_demo.py`: generated the actual synthetic image in this documentation; automatic summary was **awaiting_manual_review**, as expected.
- The annotated synthetic image was opened for visual inspection. This release does not fabricate a production-job manual sign-off.
- `docs/demo-source.png` and `docs/demo-annotated.png`: no PNG text, EXIF, GPS or XMP metadata; dimensions 1200 × 900.
- Public copy uses empty private-history and neutral branding files. No course screenshots, personal profiles, credentials or absolute home paths are included.
- Classic-pattern scan, Python/Pillow dependency review and boundary scan of configs/examples/references found no credential values or personal account identifiers. No pre-existing Git history is included in this export.

Limitations: current action-label validation is Chinese-first; fonts vary across systems; declared protection regions need human/agent input; privacy detection and redaction are supplied externally; final correctness requires inspecting the actual images. The regression run validates this local runtime, not every OS.

本次复用了现有 Python 与 Pillow。38 项合成回归测试通过，演示自动状态为等待人工复核。公开版没有课程真实截图、个人账号、私人执行记录或密钥。中文文案检查、字体环境、保护区域输入与人工看图门禁仍是使用条件。
