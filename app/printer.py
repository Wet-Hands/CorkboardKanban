"""Talk to the GEZHI micro-printer through the usblp device node (/dev/usb/lp0).

The printer disconnects from USB when it powers itself off, which removes the
device node. Callers check `is_available()` and leave jobs in the queue until
it comes back. A periodic keepalive (DLE EOT 1, a harmless status query) is
sent while idle to stop the printer from powering off in the first place.

With PRINTER_DEVICE=dry-run, cards are saved as PNGs in data/printed/ instead.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from PIL import Image

import config

log = logging.getLogger(__name__)

KEEPALIVE = b"\x10\x04\x01"  # DLE EOT 1: transmit printer status


class PrinterUnavailable(Exception):
    """The device node is missing or can't be opened; retry later without counting an attempt."""


class PrintError(Exception):
    """The printer accepted the connection but the print failed."""


def is_available() -> bool:
    if config.PRINTER_DRY_RUN:
        return True
    return os.path.exists(config.PRINTER_DEVICE)


def status() -> dict:
    device = "dry-run" if config.PRINTER_DRY_RUN else config.PRINTER_DEVICE
    available = is_available()
    writable = config.PRINTER_DRY_RUN or (available and os.access(config.PRINTER_DEVICE, os.W_OK))
    return {"device": device, "available": available, "writable": writable}


def _open():
    from escpos.printer import File  # imported lazily so the web process starts fast

    if not os.path.exists(config.PRINTER_DEVICE):
        raise PrinterUnavailable(f"{config.PRINTER_DEVICE} not found (printer off or unplugged)")
    if not os.access(config.PRINTER_DEVICE, os.W_OK):
        raise PrinterUnavailable(
            f"Permission denied on {config.PRINTER_DEVICE}: add this user to the lp group "
            "or install the udev rule (see README)"
        )
    try:
        printer = File(devfile=config.PRINTER_DEVICE, auto_flush=False)
        printer.open()
    except Exception as exc:  # escpos wraps OSError in its own DeviceNotFoundError
        raise PrinterUnavailable(f"Can't open {config.PRINTER_DEVICE}: {exc}") from exc
    # Tell python-escpos the paper width so it can check image sizes.
    try:
        printer.profile.profile_data["media"]["width"]["pixels"] = config.PAPER_WIDTH_PX
    except (KeyError, TypeError):
        pass
    return printer


def print_image(img: Image.Image, label: str = "card") -> None:
    """Print a 1-bit image and feed enough paper to tear it off."""
    if img.width > config.PAPER_WIDTH_PX:
        raise PrintError(f"Image is {img.width}px wide; paper is {config.PAPER_WIDTH_PX}px")
    if config.PRINTER_DRY_RUN:
        config.PRINTED_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = config.PRINTED_DIR / f"{stamp}-{label}.png"
        img.save(path)
        log.info("Dry run: saved %s", path)
        return
    printer = _open()
    try:
        printer.image(
            img,
            impl=config.PRINTER_IMAGE_IMPL,
            fragment_height=config.PRINTER_FRAGMENT_HEIGHT,
        )
        printer.ln(config.PRINTER_FEED_LINES)
        printer.flush()
    except FileNotFoundError as exc:
        raise PrinterUnavailable(str(exc)) from exc
    except OSError as exc:
        # ENODEV / EIO: the printer dropped off USB mid-job.
        if exc.errno in (5, 6, 19):
            raise PrinterUnavailable(f"Printer disconnected while printing: {exc}") from exc
        raise PrintError(str(exc)) from exc
    except Exception as exc:
        raise PrintError(str(exc)) from exc
    finally:
        try:
            printer.close()
        except Exception:
            pass


def keepalive() -> bool:
    """Send a status query so the printer stays awake. Returns True if it was sent."""
    if config.PRINTER_DRY_RUN or not os.path.exists(config.PRINTER_DEVICE):
        return False
    try:
        fd = os.open(config.PRINTER_DEVICE, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        log.debug("Keepalive skipped: %s", exc)
        return False
    try:
        os.write(fd, KEEPALIVE)
        return True
    except OSError as exc:
        log.debug("Keepalive write failed: %s", exc)
        return False
    finally:
        os.close(fd)
