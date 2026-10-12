from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from PIL import Image, ImageDraw, PngImagePlugin


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"


def load_script_module(filename: str, module_name: str) -> ModuleType:
    """从 skill 的 scripts 目录加载可导入入口。"""
    path = SCRIPTS_DIR / filename
    if not path.exists():
        raise AssertionError(f"缺少待测脚本：{path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载待测脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    scripts_path = str(SCRIPTS_DIR)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} 应为 JSON object")
    return value


def make_synthetic_screenshot(
    path: Path,
    *,
    size: tuple[int, int] = (1200, 900),
    with_metadata: bool = False,
) -> Path:
    """生成没有真实用户素材或个人信息的确定性教程截图。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, "#F4F6F8")
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle((0, 0, width, 88), fill="#233044")
    draw.text((40, 30), "Synthetic Tutorial", fill="#FFFFFF")
    draw.rounded_rectangle(
        (70, 145, width - 70, height - 85),
        radius=28,
        fill="#FFFFFF",
        outline="#CAD2DC",
        width=3,
    )
    draw.text((115, 190), "Demo workflow", fill="#263449")
    card_width = (width - 310) // 2
    for index in range(4):
        column = index % 2
        row = index // 2
        x1 = 115 + column * (card_width + 80)
        y1 = 260 + row * 220
        x2 = x1 + card_width
        y2 = y1 + 155
        draw.rounded_rectangle(
            (x1, y1, x2, y2),
            radius=18,
            fill="#E9EEF4",
            outline="#B6C1CE",
            width=2,
        )
        draw.text((x1 + 28, y1 + 28), f"Action {index + 1}", fill="#273449")
        draw.rounded_rectangle(
            (x1 + 28, y2 - 58, x1 + 188, y2 - 22),
            radius=12,
            fill="#2E6BE6",
        )
        draw.text((x1 + 50, y2 - 49), "Continue", fill="#FFFFFF")

    if with_metadata:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Author", "Synthetic Test Fixture")
        metadata.add_text("Comment", "Must not survive derived output")
        exif = Image.Exif()
        exif[315] = "Synthetic Test Fixture"
        image.save(path, pnginfo=metadata, exif=exif)
    else:
        image.save(path)
    return path


def make_animated_gif(path: Path) -> Path:
    """生成无真实素材的两帧 GIF，用于 unsupported 路由测试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    first = Image.new("RGB", (320, 240), "#DDE6F0")
    second = Image.new("RGB", (320, 240), "#C9D7E8")
    first.save(
        path,
        save_all=True,
        append_images=[second],
        duration=80,
        loop=0,
    )
    return path
