#!/usr/bin/env python3
"""Smoke test for the micro-agent browser tooling.

Verifies that the browser_action / cdp_fetch modules still work end-to-end
after refactoring (lint formatting, rule-ignore config, subprocess check=False
changes). Runs REAL operations against the CDP Chromium on port 9222, so it
requires a browser to be available (it will start one on demand).

Usage:
    python3 browse_smoke.py

Exit code 0 = all checks passed; non-zero = at least one check failed.
"""
import os
import sys
import time

import cdp_fetch
import browser_action as ba

CHECKS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    CHECKS.append(cond)
    print(f"[{status}] {name}" + (f" — {detail}" if detail and cond else f" — {detail}" if detail else ""))
    return cond


def main():
    print("== micro-agent browser smoke test ==")

    # 1. Modules import and expose their public API.
    check("cdp_fetch imports", hasattr(cdp_fetch, "fetch_webpage"))
    check("browser_action imports", hasattr(ba, "browser_action"))
    check("cdp_fetch.ensure_chromium callable", callable(cdp_fetch._ensure_chromium))

    # 2. micro.py tool registry wires the browser tools correctly.
    try:
        import micro
        for name in ("fetch_webpage", "browser_action"):
            check(f"micro TOOL_EXECUTORS[{name}] wired",
                  name in micro.TOOL_EXECUTORS and callable(micro.TOOL_EXECUTORS[name]))
    except Exception as e:
        check("micro.py imports", False, repr(e))

    # 3. Ensure Chromium is up (starts on demand).
    ok = cdp_fetch._ensure_chromium()
    check("ensure_chromium started", ok, "CDP on 127.0.0.1:9222" if ok else "could not start")

    if not cdp_fetch._cdp_ping():
        print("\nRESULT: FAIL (browser unavailable)")
        sys.exit(1)

    # 4. fetch_webpage: real JS-rendered fetch (proves CDP render path).
    url = "https://example.com/"
    print(f"\n-- fetch_webpage({url}) --")
    text = cdp_fetch.fetch_webpage(url)
    showed = "Example Domain" if text else repr(text[:80])
    check("fetch_webpage returns content", bool(text) and len(text) > 50, f"got {len(text)} chars")
    check("fetch_webpage shows expected domain", "Example Domain" in text or "example.com" in text, showed)

    # 5. browser_action high-level actions.
    print("\n-- browser_action.get_state --")
    state = ba.browser_action("get_state")
    check("get_state returns tabs", isinstance(state, str) and "tabs" in state.lower(), state[:80].replace("\n", " "))

    print("\n-- browser_action.navigate --")
    nav = ba.browser_action("navigate", url="https://example.com/")
    check("navigate returns info", isinstance(nav, str) and len(nav) > 20, nav[:80].replace("\n", " "))
    check("navigate reached example.com", "example" in nav.lower(), nav[:50].replace("\n", " "))

    print("\n-- browser_action.eval --")
    ev = ba.browser_action("eval", script="document.title")
    check("eval returns page title", isinstance(ev, str) and "Example" in ev, str(ev)[:60])

    print("\n-- browser_action.scroll --")
    sc = ba.browser_action("scroll", y=200)
    check("scroll executed", isinstance(sc, str) and "Scrolled" in sc, str(sc)[:60])

    print("\n-- browser_action.screenshot --")
    shot = "/tmp/micro_browser_smoke.jpg"
    ss = ba.browser_action("screenshot", path=shot)
    ok_s = os.path.isfile(shot) and os.path.getsize(shot) > 1000
    check("screenshot saved", ok_s, f"{shot} ({os.path.getsize(shot) if os.path.exists(shot) else 0} bytes)")

    # 6. Cleanup: stop Chromium to free memory.
    cdp_fetch.shutdown_chromium()
    print("\n== RESULT: %s ==" % ("ALL PASS" if all(CHECKS) else "SOME CHECKS FAILED"))
    sys.exit(0 if all(CHECKS) else 1)


if __name__ == "__main__":
    main()
