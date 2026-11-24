#!/usr/bin/env python3
"""Find a keepalive that stops the printer from powering itself off.

For each candidate command, it waits for you to switch the printer on, sends
the command every few seconds, and times how long the printer stays
connected. A command "works" if the printer is still there after --hold seconds.

    sudo systemctl stop corkboard-worker
    .venv/bin/python scripts/keepalive_test.py
    sudo systemctl start corkboard-worker

Options:
    --commands none,dle-eot,gs-r   which to try (default: all; "none" is the baseline)
    --interval 10                  seconds between sends (default 10)
    --hold 150                     seconds a command must keep it awake (default 150)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from app import printer  # noqa: E402

CANDIDATES = {"none": ""} | printer.KEEPALIVE_COMMANDS


def wait_for(predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def throttled() -> str | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        return subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None


def run_one(device: str, name: str, interval: float, hold: float) -> tuple[str, float, bytes]:
    present = lambda: os.path.exists(device)  # noqa: E731
    if present():
        print("  Switch the printer OFF, then back ON (so the timing starts fresh)...")
        if not wait_for(lambda: not present(), 300):
            return "skipped (printer was never switched off)", 0.0, b""
    else:
        print("  Switch the printer ON...")
    if not wait_for(present, 300):
        return "skipped (printer never appeared)", 0.0, b""
    start = time.monotonic()
    data = bytes.fromhex(CANDIDATES[name])
    action = f"Sending {name} every {interval:.0f}s" if data else "Sending nothing"
    print(f"  Connected. {action} for up to {hold:.0f}s", end="", flush=True)
    reply = b""
    next_send = start
    while time.monotonic() - start < hold:
        if not present():
            print()
            return "DISCONNECTED", time.monotonic() - start, reply
        if time.monotonic() >= next_send:
            try:
                got = printer.send_raw(device, data, read_reply=bool(data))
                reply = reply or got
                print(".", end="", flush=True)
            except OSError as exc:
                if not present():
                    print()
                    return "DISCONNECTED", time.monotonic() - start, reply
                print(f"\n  write failed: {exc}")
                return f"error: {exc}", time.monotonic() - start, reply
            next_send += interval
        time.sleep(0.25)
    print()
    return "STAYED ON", time.monotonic() - start, reply


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=config.PRINTER_DEVICE)
    parser.add_argument("--commands", default=",".join(CANDIDATES))
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--hold", type=float, default=150)
    args = parser.parse_args()

    names = [n.strip() for n in args.commands.split(",") if n.strip()]
    unknown = [n for n in names if n not in CANDIDATES]
    if unknown:
        print(f"Unknown command(s): {', '.join(unknown)}. Choose from: {', '.join(CANDIDATES)}")
        return 2
    if config.PRINTER_DRY_RUN and not args.device.startswith("/"):
        print("PRINTER_DEVICE is dry-run; pass --device /dev/usb/lp0")
        return 2

    print(f"Testing keepalives on {args.device}. Make sure corkboard-worker is stopped.")
    before = throttled()
    results = []
    for name in names:
        print(f"\n[{name}] {CANDIDATES[name] or '(send nothing: baseline)'}")
        outcome, seconds, reply = run_one(args.device, name, args.interval, args.hold)
        results.append((name, outcome, seconds, reply))
        print(f"  -> {outcome} after {seconds:.0f}s" + (f", printer replied {reply.hex(' ')}" if reply else ""))

    print("\nSummary")
    for name, outcome, seconds, reply in results:
        print(f"  {name:8} {outcome:14} {seconds:5.0f}s  {'reply ' + reply.hex(' ') if reply else ''}")
    after = throttled()
    if after:
        print(f"\nPower check: {after} (before: {before}). Anything but throttled=0x0 means the Pi is short of power,")
        print("which can also drop the printer off USB.")

    working = [name for name, outcome, *_ in results if outcome == "STAYED ON" and name != "none"]
    if working:
        best = working[0]
        print(f"\n{best} kept the printer awake. Put this in .env and restart the worker:")
        print(f"  PRINTER_KEEPALIVE_COMMAND={CANDIDATES[best]}")
        print(f"  PRINTER_KEEPALIVE_SECONDS={int(args.interval)}")
    elif any(outcome == "STAYED ON" for name, outcome, *_ in results if name == "none"):
        print("\nThe printer stayed on even with no keepalive, so the disconnects are probably power or cable.")
    else:
        print("\nNo command kept it awake. The auto-off likely ignores USB traffic: try a separate power supply,")
        print("or look for an auto-off setting on the self-test page (hold FEED while switching on).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
