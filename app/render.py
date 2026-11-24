"""Draw task cards and the morning digest as 1-bit images for the thermal printer.

Cards are drawn in greyscale with antialiased text, then thresholded (not
dithered) to pure black and white so text stays crisp on thermal paper.
"""

from __future__ import annotations

import io
from datetime import date
from functools import lru_cache
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

import config
from app.models import due_state, today

BLACK, WHITE = 0, 255
MARGIN = 8
LINE = 3  # rule thickness; the printer needs >= 2 px
MAX_DESCRIPTION_LINES = 14
SOURCE_TAGS = {"github": "PR", "claude": "CLAUDE", "recurring": "RECURRING"}

_FONT_FILES = {
    "regular": ["DejaVuSans.ttf"],
    "bold": ["DejaVuSans-Bold.ttf"],
}
_SYSTEM_FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu")]


@lru_cache(maxsize=None)
def font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    for directory in [config.FONTS_DIR, *_SYSTEM_FONT_DIRS]:
        for name in _FONT_FILES[weight]:
            path = directory / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    raise FileNotFoundError(
        "DejaVu fonts not found: put DejaVuSans.ttf and DejaVuSans-Bold.ttf in fonts/ "
        "or install fonts-dejavu-core."
    )


def wrap(text: str, fnt: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Word-wrap to a pixel width, breaking words that are too long on their own."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if fnt.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while fnt.getlength(word) > width:
                cut = len(word)
                while cut > 1 and fnt.getlength(word[:cut]) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def fit_lines(lines: list[str], fnt: ImageFont.FreeTypeFont, width: int, max_lines: int) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and fnt.getlength(last + "…") > width:
        last = last[:-1]
    kept[-1] = last.rstrip() + "…"
    return kept


class Canvas:
    """A tall greyscale canvas with a y cursor; `finish()` crops and thresholds it."""

    def __init__(self, width: int = 0):
        self.width = width or config.PAPER_WIDTH_PX
        self.inner = self.width - 2 * MARGIN
        self.image = Image.new("L", (self.width, 6000), WHITE)
        self.draw = ImageDraw.Draw(self.image)
        self.y = MARGIN

    def gap(self, px: int) -> None:
        self.y += px

    def text_lines(self, lines: list[str], fnt, *, spacing: int = 4, x: int = MARGIN, fill: int = BLACK,
                   align: str = "left") -> None:
        ascent, descent = fnt.getmetrics()
        for line in lines:
            lx = x
            if align == "center":
                lx = (self.width - fnt.getlength(line)) / 2
            self.draw.text((lx, self.y), line, font=fnt, fill=fill)
            self.y += ascent + descent + spacing

    def paragraph(self, text: str, fnt, *, max_lines: int | None = None, spacing: int = 4,
                  align: str = "left") -> None:
        lines = wrap(text, fnt, self.inner)
        if max_lines:
            lines = fit_lines(lines, fnt, self.inner, max_lines)
        self.text_lines(lines, fnt, spacing=spacing, align=align)

    def rule(self, thickness: int = LINE) -> None:
        self.draw.rectangle([MARGIN, self.y, self.width - MARGIN - 1, self.y + thickness - 1], fill=BLACK)
        self.y += thickness

    def bar(self, label: str, fnt, *, solid: bool, outline: int = LINE, pad: int = 6) -> None:
        ascent, descent = fnt.getmetrics()
        height = ascent + descent + 2 * pad
        box = [MARGIN, self.y, self.width - MARGIN - 1, self.y + height - 1]
        if solid:
            self.draw.rectangle(box, fill=BLACK)
        else:
            self.draw.rectangle(box, outline=BLACK, width=outline)
        tx = (self.width - fnt.getlength(label)) / 2
        self.draw.text((tx, self.y + pad), label, font=fnt, fill=WHITE if solid else BLACK)
        self.y += height

    def finish(self) -> Image.Image:
        cropped = self.image.crop((0, 0, self.width, self.y + MARGIN))
        return cropped.point(lambda v: 255 if v >= config.PRINTER_THRESHOLD else 0, mode="1")


def qr_image(data: str, max_px: int) -> Image.Image:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.modules_count
    box = max(2, max_px // modules)
    img = qr.make_image(fill_color="black", back_color="white").get_image().convert("L")
    return img.resize((modules * box, modules * box), Image.NEAREST)


def human_date(value: date, ref: date | None = None) -> str:
    """"Fri Nov 28", with the year added when it isn't the current one ("Fri Jan 1, 2027")."""
    ref = ref or today()
    text = f"{value:%a %b} {value.day}"
    return text if value.year == ref.year else f"{text}, {value.year}"


def task_link(task: dict) -> str:
    return task.get("url") or f"{config.BASE_URL}/task/{task['id']}"


def render_card(task: dict, ref: date | None = None) -> Image.Image:
    ref = ref or today()
    c = Canvas()

    # 1. Priority header
    c.bar(f"{task['priority'].upper()} PRIORITY", font(28), solid=True)
    c.gap(10)

    # 2. Title
    c.paragraph(task["title"], font(34), max_lines=6, spacing=2)

    # 3. Description
    if task.get("description"):
        c.gap(6)
        c.paragraph(task["description"], font(22, "bold"), max_lines=MAX_DESCRIPTION_LINES, spacing=3)

    # 4. Due date and banner
    if task.get("due_date"):
        c.gap(10)
        due = date.fromisoformat(task["due_date"])
        c.text_lines([f"Due {human_date(due, ref)}"], font(26))
        banner = {"overdue": "OVERDUE", "today": "DUE TODAY", "tomorrow": "DUE TOMORROW"}.get(
            due_state(task, ref) or ""
        )
        if banner:
            c.gap(4)
            c.bar(banner, font(26), solid=True, pad=4)

    # 5. Footer: id, source tag, QR
    c.gap(10)
    c.rule()
    c.gap(8)
    qr = qr_image(task_link(task), 120)
    top = c.y
    c.draw.text((MARGIN, top), f"#{task['id']}", font=font(40), fill=BLACK)
    tag = SOURCE_TAGS.get(task.get("source", ""))
    if tag:
        tag_font = font(22)
        ty = top + 52
        tw = tag_font.getlength(tag)
        c.draw.rectangle([MARGIN, ty, MARGIN + tw + 12, ty + 32], fill=BLACK)
        c.draw.text((MARGIN + 6, ty + 3), tag, font=tag_font, fill=WHITE)
    if task.get("recurrence", "none") != "none":
        c.draw.text((MARGIN, top + 92), f"repeats {task['recurrence']}", font=font(22), fill=BLACK)
    c.image.paste(qr, (c.width - MARGIN - qr.width, top))
    c.y = top + max(qr.height, 120)
    return c.finish()


def render_digest(data: dict) -> Image.Image:
    c = Canvas()
    day = date.fromisoformat(data["date"])
    c.bar("GOOD MORNING", font(30), solid=True)
    c.gap(6)
    c.text_lines([f"{day:%A}", f"{day:%B} {day.day}"], font(28), align="center", spacing=2)
    c.gap(6)
    counts = data["counts"]
    c.text_lines(
        [f"To do {counts['todo']} · Doing {counts['doing']}", f"Done yesterday {counts['done_yesterday']}"],
        font(22), align="center", spacing=2,
    )

    sections = [
        ("OVERDUE", data["overdue"], True),
        ("DUE TODAY", data["today"], True),
        ("IN PROGRESS", data["doing"], False),
        ("HIGH PRIORITY", data["high"], False),
    ]
    entry_font = font(22)
    shown_any = False
    for heading, tasks, solid in sections:
        if not tasks:
            continue
        shown_any = True
        c.gap(12)
        c.bar(heading, font(24), solid=solid, pad=3)
        c.gap(4)
        for task in tasks[:8]:
            marker = {"high": "!!", "medium": "!", "low": ""}[task["priority"]]
            label = f"#{task['id']} {marker} {task['title']}".replace("  ", " ")
            if heading == "OVERDUE":
                label += f" ({human_date(date.fromisoformat(task['due_date']), day)})"
            lines = fit_lines(wrap(label, entry_font, c.inner - 16), entry_font, c.inner - 16, 2)
            ascent, descent = entry_font.getmetrics()
            c.draw.rectangle([MARGIN, c.y + 8, MARGIN + 7, c.y + 15], fill=BLACK)
            c.text_lines(lines, entry_font, x=MARGIN + 16, spacing=2)
        if len(tasks) > 8:
            c.text_lines([f"+ {len(tasks) - 8} more"], entry_font, x=MARGIN + 16)
    if not shown_any:
        c.gap(16)
        c.paragraph("Nothing urgent today. Enjoy it.", font(26), align="center")
    c.gap(12)
    c.rule()
    return c.finish()


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
