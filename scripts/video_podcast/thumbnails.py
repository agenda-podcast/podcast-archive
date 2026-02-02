from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass(frozen=True)
class ThumbnailSpec:
    w: int = 1280
    h: int = 720
    # Left panel is square, right panel fills the rest.
    left_pad: int = 0


def _load_font(paths: Tuple[str, ...], size: int):
    try:
        from PIL import ImageFont  # type: ignore
    except Exception as e:
        raise RuntimeError("Pillow is required for thumbnail generation") from e

    for p in paths:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_w: int):
    words = [w for w in text.replace("\n", " ").split(" ") if w]
    if not words:
        return []
    lines = []
    cur = words[0]
    for w in words[1:]:
        test = cur + " " + w
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def _fit_text(draw, text: str, *, box_w: int, box_h: int, bold: bool):
    # Prefer DejaVu on Linux runners.
    if bold:
        font_paths = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
    else:
        font_paths = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        )

    # Start large, step down until fits.
    for size in range(80, 14, -2):
        font = _load_font(font_paths, size=size)
        lines = _wrap_text(draw, text, font, box_w)
        if not lines:
            return font, []
        line_h = int(size * 1.2)
        total_h = line_h * len(lines)
        if total_h <= box_h:
            return font, lines
    font = _load_font(font_paths, size=14)
    return font, _wrap_text(draw, text, font, box_w)


def ensure_thumbnail_template(
    *,
    repo_root: Optional[Path] = None,
    left_image_path: Optional[Path] = None,
    template_path: Optional[Path] = None,
    # Back-compat keyword aliases used by older call sites.
    left_img_path: Optional[Path] = None,
    template_png: Optional[Path] = None,
    spec: ThumbnailSpec = ThumbnailSpec(),
) -> None:
    """Create a 16:9 template with a square left image and black right panel.

    If left_image_path does not exist, a simple placeholder is used.
    """

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception as e:
        raise RuntimeError("Pillow is required for thumbnail generation") from e

    # Normalize keyword aliases.
    if left_image_path is None:
        left_image_path = left_img_path
    if template_path is None:
        template_path = template_png
    if left_image_path is None or template_path is None:
        raise ValueError("left_image_path and template_path are required")

    w, h = spec.w, spec.h
    left_size = h
    right_w = w - left_size

    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    if left_image_path.exists():
        src = Image.open(left_image_path).convert("RGB")
        # Center-crop to square, then fit to left panel.
        sw, sh = src.size
        s = min(sw, sh)
        x0 = (sw - s) // 2
        y0 = (sh - s) // 2
        src_sq = src.crop((x0, y0, x0 + s, y0 + s)).resize((left_size, left_size))
        canvas.paste(src_sq, (0, 0))
    else:
        # Placeholder: dark gray gradient box with label.
        draw.rectangle((0, 0, left_size, left_size), fill=(25, 25, 25))
        f = _load_font(("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",), size=36)
        msg = "PLACEHOLDER"
        tw = draw.textlength(msg, font=f)
        draw.text(((left_size - tw) / 2, h * 0.45), msg, font=f, fill=(200, 200, 200))

    # Right panel is already black.
    template_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(template_path)


def render_episode_thumbnail(
    *,
    template_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    # Back-compat keyword aliases used by older call sites.
    template_png: Optional[Path] = None,
    out_png: Optional[Path] = None,
    episode_title: Optional[str] = None,
    episode_description: Optional[str] = None,
    spec: ThumbnailSpec = ThumbnailSpec(),
) -> None:
    """Render a per-episode thumbnail.

    Layout:
      - left: square image from template
      - right: black background
      - right top 1/3: title (bold, large, white)
      - right bottom 2/3: description (regular, white)
    """

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception as e:
        raise RuntimeError("Pillow is required for thumbnail generation") from e

    if template_path is None:
        template_path = template_png
    if out_path is None:
        out_path = out_png
    if title is None:
        title = episode_title or ""
    if description is None:
        description = episode_description or ""
    if template_path is None or out_path is None:
        raise ValueError("template_path and out_path are required")

    img = Image.open(template_path).convert("RGB")
    w, h = spec.w, spec.h
    if img.size != (w, h):
        img = img.resize((w, h))

    draw = ImageDraw.Draw(img)
    left_size = h
    right_x0 = left_size
    right_w = w - left_size

    pad = 32
    box_x0 = right_x0 + pad
    box_x1 = w - pad
    top_h = h // 3
    bot_h = h - top_h

    # Title box.
    title_box = (box_x0, pad, box_x1, top_h - pad)
    font_t, lines_t = _fit_text(draw, title, box_w=title_box[2] - title_box[0], box_h=title_box[3] - title_box[1], bold=True)
    y = title_box[1]
    line_h = int(getattr(font_t, "size", 32) * 1.2)
    for line in lines_t:
        draw.text((title_box[0], y), line, font=font_t, fill=(255, 255, 255))
        y += line_h

    # Description box.
    desc_box = (box_x0, top_h + pad, box_x1, h - pad)
    font_d, lines_d = _fit_text(draw, description, box_w=desc_box[2] - desc_box[0], box_h=desc_box[3] - desc_box[1], bold=False)
    y = desc_box[1]
    line_h = int(getattr(font_d, "size", 22) * 1.25)
    for line in lines_d:
        draw.text((desc_box[0], y), line, font=font_d, fill=(255, 255, 255))
        y += line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)