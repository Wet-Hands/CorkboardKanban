#!/usr/bin/env python3
"""Print a sample card of each priority (and optionally the digest) straight to the printer.

    python scripts/test_print.py              # three sample cards
    python scripts/test_print.py --digest     # plus a sample digest
    python scripts/test_print.py --save out/  # save PNGs instead of printing
    python scripts/test_print.py --impl bitImageColumn --fragment-height 256

Stop corkboard-worker first (sudo systemctl stop corkboard-worker) so the two
don't write to the printer at the same time.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from app import printer, render  # noqa: E402
from app.models import today  # noqa: E402


def samples() -> list[dict]:
    day = today()
    common = {"status": "todo", "recurrence": "none", "url": None}
    return [
        {**common, "id": 101, "priority": "high", "source": "web",
         "title": "Replace the furnace filter",
         "description": "16x25x1, MERV 11. Write today's date on the new one.",
         "due_date": day.isoformat()},
        {**common, "id": 102, "priority": "medium", "source": "github",
         "title": "Review #482: Add retry logic to the webhook dispatcher",
         "description": "acme/payments · by @octocat · +212 −48 · 7 files",
         "due_date": (day + timedelta(days=1)).isoformat(), "url": "https://github.com"},
        {**common, "id": 103, "priority": "low", "source": "recurring", "recurrence": "weekly",
         "title": "Water the plants", "description": "", "due_date": (day + timedelta(days=4)).isoformat()},
    ]


def sample_digest(tasks: list[dict]) -> dict:
    return {"date": today().isoformat(), "overdue": [], "today": [tasks[0]], "doing": [], "high": [],
            "counts": {"todo": 3, "doing": 0, "done_yesterday": 2}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--digest", action="store_true", help="also print a sample morning digest")
    parser.add_argument("--save", metavar="DIR", help="save PNGs to DIR instead of printing")
    parser.add_argument("--device", help=f"printer device (default {config.PRINTER_DEVICE})")
    parser.add_argument("--impl", choices=["bitImageRaster", "bitImageColumn", "graphics"])
    parser.add_argument("--fragment-height", type=int)
    args = parser.parse_args()

    if args.device:
        config.PRINTER_DEVICE = args.device
        config.PRINTER_DRY_RUN = False
    if args.impl:
        config.PRINTER_IMAGE_IMPL = args.impl
    if args.fragment_height:
        config.PRINTER_FRAGMENT_HEIGHT = args.fragment_height

    tasks = samples()
    images = [(f"sample-{t['priority']}", render.render_card(t)) for t in tasks]
    if args.digest:
        images.append(("sample-digest", render.render_digest(sample_digest(tasks))))

    if args.save:
        out = Path(args.save)
        out.mkdir(parents=True, exist_ok=True)
        for name, img in images:
            img.save(out / f"{name}.png")
            print(f"saved {out / f'{name}.png'} ({img.width}x{img.height})")
        return 0

    state = printer.status()
    if not state["available"]:
        print(f"Printer not found at {state['device']}. Is it switched on and plugged in? "
              "(It powers itself off after about 86 s idle.)")
        return 1
    if not state["writable"]:
        print(f"No permission to write {state['device']}. Run: sudo usermod -aG lp $USER "
              "(then log out and back in), or install deploy/99-h22-printer.rules.")
        return 1
    for name, img in images:
        print(f"printing {name} ({img.width}x{img.height}, impl={config.PRINTER_IMAGE_IMPL}) ...")
        try:
            printer.print_image(img, name)
        except (printer.PrinterUnavailable, printer.PrintError) as exc:
            print(f"failed: {exc}")
            return 1
    print("done. If the cards are garbled or striped, try --impl bitImageColumn or --fragment-height 256.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
