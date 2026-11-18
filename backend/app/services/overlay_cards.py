"""Cartões de hino e versículo no modelo visual de novembro.

O culto de novembro foi gerado com PNG por cue e `overlay` do ffmpeg
(não ASS): painel escuro translúcido, filete dourado à esquerda, EB Garamond,
cabeçalho dourado e letra centralizada. Este módulo reproduz esse cartão.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

GOLD = (222, 184, 96, 255)
CREAM = (225, 215, 195, 230)
TEXT = (250, 247, 240, 255)
PANEL = (10, 10, 18, 178)
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_ROMAN = "EBGaramond-VariableFont_wght.ttf"
_ITALIC = "EBGaramond-Italic-VariableFont_wght.ttf"


@dataclass(frozen=True)
class OverlayCard:
    path: Path
    start: float
    end: float
    fade_in: float
    fade_out: float
    kind: str = "lyric"


def font_path(italic: bool = False) -> Path:
    name = _ITALIC if italic else _ROMAN
    bundled = FONTS_DIR / name
    if bundled.is_file():
        return bundled
    for candidate in (
        Path("/usr/share/fonts/truetype/sand-box/google/EB Garamond") / name,
        Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf" if italic else "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf" if italic else "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("não achei EB Garamond nem uma serifada de reserva")


def _truetype(path: Path, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(path), size)
    if weight:
        try:
            font.set_variation_by_axes([weight])
        except Exception:
            pass
    return font


def load_font(size: int, *, italic: bool = False, weight: int | None = None) -> ImageFont.FreeTypeFont:
    return _truetype(font_path(italic=italic), size, weight)


def probe_video_size(video: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        width_s, height_s = result.stdout.strip().split("x")
        width, height = int(width_s), int(height_s)
        if width >= 16 and height >= 16:
            return width, height
    except (OSError, subprocess.CalledProcessError, ValueError):
        pass
    return 1920, 1080


def output_size(video: Path | None, max_height: int = 0) -> tuple[int, int]:
    width, height = probe_video_size(video) if video is not None and video.is_file() else (1920, 1080)
    if max_height and height > max_height:
        width = max(2, int(round(width * max_height / height)) & ~1)
        height = int(max_height)
    return width, height


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines() or [""]:
        words = raw.split()
        if not words:
            continue
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            if current and draw.textlength(trial, font=font) > max_width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
    return lines


def _panel(
    draw: ImageDraw.ImageDraw,
    x0: int,
    y0: int,
    width: int,
    height: int,
    radius: int,
    bar_inset: int,
) -> None:
    draw.rounded_rectangle([x0, y0, x0 + width, y0 + height], radius=radius, fill=PANEL)
    draw.rectangle([x0, y0 + bar_inset, x0 + max(4, bar_inset // 2 + 2), y0 + height - bar_inset], fill=GOLD)


def render_hymn_card(
    dest: Path,
    *,
    number: str,
    title: str,
    label: str,
    lines: list[str],
    instrumental: bool = False,
    size: tuple[int, int] = (1920, 1080),
) -> Path:
    width, height = size
    scale = width / 1920
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    body_lines = [line.strip() for line in lines if line.strip()]
    if not body_lines:
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest)
        return dest
    big = len(body_lines) > 4
    body_font = load_font(int((33 if big else 38) * scale), weight=500)
    head_font = load_font(int(27 * scale), weight=600)
    tag_font = load_font(int(25 * scale), italic=True, weight=450)
    line_height = int((39 if big else 46) * scale)
    pad = int(26 * scale)
    head = f"Cantor Cristão nº {number} — {title}" if number else title
    tag = label + (" · instrumental" if instrumental and "instrumental" not in label.lower() else "")
    text_w = max(draw.textlength(line, font=body_font) for line in body_lines)
    panel_w = int(
        max(
            text_w + 2 * pad + 60 * scale,
            draw.textlength(head, font=head_font) + draw.textlength(tag, font=tag_font) + 2 * pad + 50 * scale,
            860 * scale,
        )
    )
    panel_w = min(panel_w, width - int(40 * scale))
    x0 = (width - panel_w) // 2
    panel_h = pad + int(30 * scale) + int(12 * scale) + len(body_lines) * line_height + pad - int(12 * scale)
    y0 = height - int(26 * scale) - panel_h
    _panel(draw, x0, y0, panel_w, panel_h, radius=max(8, int(18 * scale)), bar_inset=int(16 * scale))
    draw.text((x0 + pad, y0 + pad - int(6 * scale)), head, font=head_font, fill=GOLD)
    tag_w = draw.textlength(tag, font=tag_font)
    if tag:
        draw.text((x0 + panel_w - pad - tag_w, y0 + pad - int(4 * scale)), tag, font=tag_font, fill=CREAM)
    y = y0 + pad + int(30 * scale) + int(8 * scale)
    for line in body_lines:
        line_w = draw.textlength(line, font=body_font)
        draw.text(((width - line_w) / 2, y), line, font=body_font, fill=TEXT)
        y += line_height
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def render_verse_card(
    dest: Path,
    *,
    reference: str,
    text: str,
    size: tuple[int, int] = (1920, 1080),
) -> Path:
    width, height = size
    scale = width / 1920
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    body_font = load_font(int(40 * scale), italic=True, weight=450)
    ref_font = load_font(int(32 * scale), weight=600)
    panel_w = min(int(1500 * scale), width - int(40 * scale))
    x0 = (width - panel_w) // 2
    pad = int(34 * scale)
    header = reference if "(ARC)" in reference else f"{reference}  (ARC)"
    lines = _wrap(draw, text, body_font, panel_w - 2 * pad)
    line_height = int(50 * scale)
    panel_h = pad + int(40 * scale) + int(14 * scale) + max(len(lines), 1) * line_height + pad - int(8 * scale)
    y0 = height - int(34 * scale) - panel_h
    _panel(draw, x0, y0, panel_w, panel_h, radius=max(8, int(18 * scale)), bar_inset=int(16 * scale))
    draw.text((x0 + pad, y0 + pad - int(6 * scale)), header, font=ref_font, fill=GOLD)
    y = y0 + pad + int(40 * scale) + int(8 * scale)
    for line in lines:
        draw.text((x0 + pad, y), line, font=body_font, fill=TEXT)
        y += line_height
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def fade_for(kind: str, duration: float) -> tuple[float, float]:
    fade_in, fade_out = (0.6, 0.7) if kind == "verse" else (0.35, 0.35)
    if duration <= 0:
        return 0.0, 0.0
    fade_in = min(fade_in, duration / 3)
    fade_out = min(fade_out, duration / 3)
    return fade_in, fade_out
