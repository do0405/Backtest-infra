#!/usr/bin/env python3
"""Bounded retries for GitHub Release asset reads on the hosted runner."""

import os
import re
import stat
import subprocess
import sys
import time


ASSET = re.compile(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/assets/[0-9]+")
HTTP_STATUS = re.compile(rb"(?m)^gh: [^\r\n]*\(HTTP ([0-9]{3})\)\s*$")
TRANSIENT_HTTP = {429, 500, 502, 503, 504}


def classify(stderr):
    matches = HTTP_STATUS.findall(stderr)
    if matches:
        status = int(matches[-1])
        return f"HTTP {status}", status in TRANSIENT_HTTP
    if any(
        marker in stderr.lower()
        for marker in (b"connection reset by peer", b"i/o timeout", b"unexpected eof",
                       b"tls handshake timeout", b"temporary failure in name resolution")
    ):
        return "temporary network error", True
    return "unclassified GitHub error", False


def report(attempt, reason):
    message = f"Release asset read attempt {attempt}: {reason}"
    print(message, file=sys.stderr)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with open(summary, "a", encoding="utf-8") as output:
                output.write(f"- {message}\n")
        except OSError:
            pass


def main():
    gh = os.environ["BACKTEST_REAL_GH"]
    args = sys.argv[1:]
    if (
        len(args) != 4
        or args[:3] != ["api", "-H", "Accept: application/octet-stream"]
        or not ASSET.fullmatch(args[3])
        or not stat.S_ISREG(os.fstat(1).st_mode)
    ):
        os.execv(gh, [gh, *args])

    # The caller opens a fresh destination. Reuse it without keeping another
    # asset copy, and leave time for the caller's 300-second timeout.
    start = os.lseek(1, 0, os.SEEK_CUR)
    deadline = time.monotonic() + 285
    for attempt, delay in enumerate((0, 2, 5), start=1):
        if time.monotonic() + delay + 1 >= deadline:
            break
        time.sleep(delay)
        os.ftruncate(1, start)
        os.lseek(1, start, os.SEEK_SET)
        try:
            result = subprocess.run([gh, *args], stderr=subprocess.PIPE,
                                    timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            reason, retry = "download deadline exceeded", False
        else:
            if result.returncode == 0:
                return 0
            reason, retry = classify(result.stderr)
        report(attempt, reason)
        if not retry:
            break
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
