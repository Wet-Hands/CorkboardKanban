from datetime import timedelta

from app import models, render


def _task(**overrides):
    base = {"id": 7, "title": "Title", "description": "", "due_date": None, "priority": "medium",
            "status": "todo", "recurrence": "none", "source": "web", "url": None}
    return {**base, **overrides}


def test_card_is_1bit_and_paper_width():
    img = render.render_card(_task())
    assert img.mode == "1" and img.width == 384
    assert set(img.convert("L").tobytes()) <= {0, 255}


def test_card_grows_with_description_but_is_capped():
    short = render.render_card(_task())
    longer = render.render_card(_task(description="word " * 40))
    huge = render.render_card(_task(description="word " * 2000))
    assert short.height < longer.height < huge.height < 1400


def test_every_priority_and_banner_renders(today):
    for priority in ("low", "medium", "high"):
        for due in (today - timedelta(days=1), today, today + timedelta(days=1), today + timedelta(days=10)):
            img = render.render_card(_task(priority=priority, due_date=due.isoformat(), source="github",
                                           url="https://github.com/o/r/pull/1"), today)
            assert img.width == 384


def test_wrap_breaks_long_words():
    font = render.font(22)
    lines = render.wrap("a" * 200 + " tail", font, 368)
    assert len(lines) > 2 and all(font.getlength(line) <= 368 for line in lines)


def test_digest_empty_and_full(today):
    empty = render.render_digest(models.digest(today))
    for i in range(12):
        models.create_task(f"Task {i}", due_date=today - timedelta(days=1), priority="high", print_card=False)
    full = render.render_digest(models.digest(today))
    assert empty.mode == full.mode == "1" and full.height > empty.height


def test_png_bytes():
    assert render.to_png(render.render_card(_task())).startswith(b"\x89PNG")
