"""v2 渲染器与校验器共享的无状态基础能力。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - 环境门禁
    raise SystemExit(
        "缺少 Pillow。可安装：python3 -m pip install Pillow；"
        "或使用 Codex 打包 Python 运行时。"
    ) from exc


SCHEMA_VERSION = "2.0"
STYLE_PRESET = "action-orange-v2"
V2_STYLE_PRESETS = {"action-orange-v2", "action-red-v2"}
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP"}
ANCHORS = {
    "auto",
    "bottom_right",
    "inside_top_right",
    "top_right",
    "bottom_left",
    "top_left",
    "inside_bottom_right",
    "inside_top_left",
    "manual",
}
SEMANTIC_ROLE_ALIASES = {
    "single": "single",
    "single_action": "single",
    "action": "single",
    "sequence": "sequence",
    "sequence_step": "sequence",
    "step": "sequence",
    "alternative": "alternative",
    "alternative_choice": "alternative",
    "choice": "alternative",
}
STYLE = {
    "preset": STYLE_PRESET,
    "color": "#F97316",
    "text_color": "#F97316",
    "reference_width": 1179,
    "scale_with_image": True,
    "line_width": 8,
    "rect_radius": 26,
    "callout_fill": "#FFFFFF",
    "callout_border_width": 4,
    "callout_radius": 26,
    "callout_font_size": 44,
    "callout_min_font_size": 24,
    "callout_padding_x": 22,
    "callout_padding_y": 14,
    "anchor_gap": 18,
    "marker_fill": "#F97316",
    "marker_text_color": "#FFFFFF",
    "marker_radius": 28,
    "marker_font_size": 34,
    "max_label_width": 720,
    "max_manual_label_distance": 360,
}
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
SENSITIVE_METADATA_KEYS = {
    "exif",
    "icc_profile",
    "xmp",
    "XML:com.adobe.xmp",
    "comment",
    "description",
    "author",
    "copyright",
}
TECHNICAL_COPY_PATTERN = re.compile(
    r"(?i)(?:\bAPI\b|\bSDK\b|\bHTTP\b|\bSQL\b|\btoken\b|"
    r"数据库|缓存|错误码|状态码|内部模块|算法)"
)
ACTION_KINDS = {"operation", "sequential_action", "action", "alternative"}
INFORMATION_KINDS = {"information", "result", "reference"}
KNOWN_ASSET_KINDS = ACTION_KINDS | INFORMATION_KINDS | {"screenshot"}
ACTION_PREFIXES = ("点击", "选择", "填写", "上传", "阅读", "核对", "查看并确认")
INFORMATION_PREFIXES = ("阅读", "核对", "结果：")


Rect = tuple[int, int, int, int]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, label: str = "JSON 文件") -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到{label}：{path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取{label}：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON object：{path}")
    return payload


def load_font(size: int) -> ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=max(8, int(size)))
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=max(8, int(size)))
    except TypeError:  # Pillow 旧版本
        return ImageFont.load_default()


def normalize_rect(raw: Any, label: str) -> Rect:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError(f"{label}必须是 [x1, y1, x2, y2]。")
    try:
        values = [int(round(float(value))) for value in raw]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}只能包含有限数字。") from exc
    if any(not math.isfinite(float(value)) for value in raw):
        raise ValueError(f"{label}只能包含有限数字。")
    x1, y1, x2, y2 = values
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    if left == right or top == bottom:
        raise ValueError(f"{label}必须具有正宽度和正高度。")
    return left, top, right, bottom


def rect_to_list(rect: Rect) -> list[int]:
    return [int(value) for value in rect]


def rect_inside(rect: Rect, width: int, height: int) -> bool:
    x1, y1, x2, y2 = rect
    return 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height


def rect_intersection_area(first: Rect, second: Rect) -> int:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    return max(0, x2 - x1) * max(0, y2 - y1)


def rect_area(rect: Rect) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def rect_distance(first: Rect, second: Rect) -> int:
    """返回两个矩形边缘之间的欧氏距离；相交或相贴时为 0。"""

    horizontal = max(first[0] - second[2], second[0] - first[2], 0)
    vertical = max(first[1] - second[3], second[1] - first[3], 0)
    return int(round(math.hypot(horizontal, vertical)))


def clamp_label_rect(rect: Rect, width: int, height: int) -> Rect:
    x1, y1, x2, y2 = rect
    rect_width = x2 - x1
    rect_height = y2 - y1
    if rect_width >= width:
        x1, x2 = 0, rect_width
    elif x1 < 0:
        x1, x2 = 0, rect_width
    elif x2 > width:
        x2 = width
        x1 = width - rect_width
    if rect_height >= height:
        y1, y2 = 0, rect_height
    elif y1 < 0:
        y1, y2 = 0, rect_height
    elif y2 > height:
        y2 = height
        y1 = height - rect_height
    return int(x1), int(y1), int(x2), int(y2)


def safe_component(value: Any, label: str) -> str:
    text = str(value).strip()
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(character) < 32 for character in text)
    ):
        raise ValueError(f"{label}不是安全的文件名组成部分：{value!r}")
    return text


def portable_name_key(value: Any) -> str:
    """生成适用于大小写不敏感、Unicode 规范化文件系统的唯一键。"""

    return unicodedata.normalize("NFC", str(value)).casefold()


def safe_output_name(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip()
    if (
        Path(text).name != text
        or text in {"", ".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(character) < 32 for character in text)
    ):
        raise ValueError(f"output_name 必须是不含目录的文件名：{text!r}")
    suffix = Path(text).suffix.lower()
    if suffix != ".png":
        raise ValueError(
            f"v2 output_name 必须使用 .png，才能进行无损像素门禁：{text!r}"
        )
    return text


def normalize_style(raw: Any, style_preset: Any = None) -> dict[str, Any]:
    if raw is None:
        preset = style_preset or STYLE_PRESET
        overrides: dict[str, Any] = {}
    elif isinstance(raw, str):
        preset = raw
        overrides = {}
    elif isinstance(raw, dict):
        preset = raw.get("preset", style_preset or STYLE_PRESET)
        overrides = raw
    else:
        raise ValueError("style 必须是 preset 字符串或 JSON object。")
    if preset == "classic-red-v1":
        raise ValueError(
            "classic-red-v1 仅供旧版 annotate_screenshot.py 使用；"
            "v2 请改用 action-red-v2。"
        )
    if preset not in V2_STYLE_PRESETS:
        raise ValueError(
            "v2 仅支持样式预设 action-orange-v2 或 action-red-v2；"
            f"收到：{preset!r}。"
        )
    result = dict(STYLE)
    if preset == "action-red-v2":
        result.update(
            {
                "color": "#E62323",
                "text_color": "#E62323",
                "marker_fill": "#E62323",
            }
        )
    # 允许调用方微调 v2；不会读取或覆盖 v1 配置。
    for key in STYLE:
        if key == "preset" or key not in overrides:
            continue
        value = overrides[key]
        if value is not None:
            result[key] = value
    if isinstance(overrides.get("rect"), dict):
        rect_style = overrides["rect"]
        for source_key, target_key in {
            "color": "color",
            "line_width": "line_width",
            "radius": "rect_radius",
        }.items():
            if rect_style.get(source_key) is not None:
                result[target_key] = rect_style[source_key]
    if isinstance(overrides.get("callout"), dict):
        callout_style = overrides["callout"]
        for source_key, target_key in {
            "fill": "callout_fill",
            "text_color": "text_color",
            "border_width": "callout_border_width",
            "radius": "callout_radius",
            "font_size": "callout_font_size",
            "min_font_size": "callout_min_font_size",
            "padding_x": "callout_padding_x",
            "padding_y": "callout_padding_y",
            "anchor_gap": "anchor_gap",
            "max_width": "max_label_width",
            "max_manual_distance": "max_manual_label_distance",
        }.items():
            if callout_style.get(source_key) is not None:
                result[target_key] = callout_style[source_key]
    if isinstance(overrides.get("marker"), dict):
        marker_style = overrides["marker"]
        for source_key, target_key in {
            "fill": "marker_fill",
            "text_color": "marker_text_color",
            "radius": "marker_radius",
            "font_size": "marker_font_size",
        }.items():
            if marker_style.get(source_key) is not None:
                result[target_key] = marker_style[source_key]
    if isinstance(overrides.get("scaling"), dict):
        scaling = overrides["scaling"]
        if scaling.get("enabled") is not None:
            result["scale_with_image"] = bool(scaling["enabled"])
    numeric_keys = (
        "reference_width",
        "line_width",
        "rect_radius",
        "callout_border_width",
        "callout_radius",
        "callout_font_size",
        "callout_min_font_size",
        "callout_padding_x",
        "callout_padding_y",
        "anchor_gap",
        "marker_radius",
        "marker_font_size",
        "max_label_width",
        "max_manual_label_distance",
    )
    for key in numeric_keys:
        try:
            number = int(result[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"style.{key} 必须是正整数。") from exc
        if number <= 0:
            raise ValueError(f"style.{key} 必须是正整数。")
        result[key] = number
    result["preset"] = str(preset)
    return result


def normalize_semantic_role(raw: Any) -> str:
    role = str(raw or "single").strip()
    normalized = SEMANTIC_ROLE_ALIASES.get(role)
    if normalized is None:
        allowed = "、".join(sorted({"single", "sequence", "alternative"}))
        raise ValueError(f"semantic_role 必须是 {allowed} 之一：{role!r}")
    return normalized


def resolve_marker(
    requested: Any,
    role: str,
    number_index: int,
    letter_index: int,
) -> tuple[str | None, int, int]:
    marker = str(requested if requested is not None else "auto").strip()
    allowed_by_role = {
        "single": {"auto", "none"},
        "sequence": {"auto", "number"},
        "alternative": {"auto", "letter"},
    }
    allowed = allowed_by_role.get(role, set())
    if marker not in allowed:
        allowed_text = "、".join(sorted(allowed))
        raise ValueError(
            f"callout.marker={marker!r} 与 semantic_role={role!r} 不兼容；"
            f"只允许 {allowed_text}。"
        )
    if marker == "auto":
        if role == "single":
            return None, number_index, letter_index
        if role == "sequence":
            return str(number_index), number_index + 1, letter_index
        return alphabetic_marker(letter_index), number_index, letter_index + 1
    if marker == "none":
        return None, number_index, letter_index
    if marker == "number":
        return str(number_index), number_index + 1, letter_index
    return alphabetic_marker(letter_index), number_index, letter_index + 1


def alphabetic_marker(index: int) -> str:
    if index < 1:
        raise ValueError("字母 marker 序号必须从 1 开始。")
    result = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def lint_callout_text(text: Any) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    normalized = str(text if text is not None else "").strip()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not normalized:
        blockers.append({"code": "callout_text_empty", "message": "动作标签文字不能为空。"})
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        blockers.append(
            {"code": "callout_text_control_character", "message": "动作标签文字含控制字符。"}
        )
    if "\n" in normalized or "\r" in normalized or "\t" in normalized:
        blockers.append(
            {"code": "callout_text_multiline", "message": "动作标签文字必须是单行文本。"}
        )
    if len(normalized) > 40:
        blockers.append(
            {
                "code": "callout_text_too_long",
                "message": "动作标签文字超过 40 个字符，请改成简短动作。",
            }
        )
    if re.search(r"https?://|www\.", normalized, flags=re.IGNORECASE):
        blockers.append(
            {"code": "callout_text_url", "message": "动作标签文字不应直接显示网址。"}
        )
    if TECHNICAL_COPY_PATTERN.search(normalized):
        warnings.append(
            {
                "code": "callout_text_technical",
                "message": "动作标签含技术实现词，请确认普通用户能够理解。",
            }
        )
    if re.match(r"^\s*(?:\d+|[A-Z])[.、:：)]\s*", normalized):
        warnings.append(
            {
                "code": "callout_text_marker_prefix",
                "message": "动作标签文字疑似自带编号，可能与 marker 重复。",
            }
        )
    return normalized, blockers, warnings


def lint_copy_for_kind(
    text: str,
    kind: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    normalized_kind = str(kind).strip()
    if re.search(r"(?:去往|前往|跳转到)?\s*\d+\s*/\s*\d+", text):
        blockers.append(
            {
                "code": "production_page_number",
                "message": "动作标签不得写入“3/10”或“去往7/10”这类生产页码。",
            }
        )
    if normalized_kind in ACTION_KINDS and not text.startswith(ACTION_PREFIXES):
        blockers.append(
            {
                "code": "action_copy_missing_verb",
                "message": (
                    "操作型素材的动作标签必须以点击、选择、填写、上传、阅读、"
                    "核对或查看并确认开头。"
                ),
            }
        )
    elif normalized_kind in INFORMATION_KINDS and not text.startswith(
        INFORMATION_PREFIXES
    ):
        blockers.append(
            {
                "code": "information_copy_missing_verb",
                "message": "信息型素材的标签必须以阅读、核对或“结果：”开头。",
            }
        )
    elif normalized_kind not in KNOWN_ASSET_KINDS:
        warnings.append(
            {
                "code": "asset_kind_unknown_for_copy_lint",
                "message": f"素材 kind={normalized_kind!r} 未配置动作文案规则，请人工确认。",
            }
        )
    return blockers, warnings


def image_has_sensitive_metadata(image: Image.Image) -> list[str]:
    return sorted(
        str(key)
        for key, value in image.info.items()
        if key in SENSITIVE_METADATA_KEYS
        and value is not None
        and value != b""
        and value != ""
    )


def save_clean_image(image: Image.Image, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    mode = "RGB" if suffix in {".jpg", ".jpeg"} else (
        "RGBA" if "A" in image.getbands() else "RGB"
    )
    clean = Image.new(mode, image.size, (255, 255, 255, 0) if mode == "RGBA" else "white")
    clean.paste(image.convert(mode))
    if suffix in {".jpg", ".jpeg"}:
        clean.save(output, format="JPEG", quality=94, optimize=True, exif=b"")
    elif suffix == ".webp":
        clean.save(output, format="WEBP", quality=94, method=4, exif=b"", xmp=b"")
    else:
        clean.save(output, format="PNG", optimize=True)


def file_record(path: Path, root: Path, role: str) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    return {
        "path": relative,
        "role": role,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def all_pairs(values: Iterable[dict[str, Any]]) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    items = list(values)
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            yield first, second
