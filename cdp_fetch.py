#!/usr/bin/env python3
"""CDP-based webpage fetcher for micro.py.

Uses a VISIBLE Chromium instance to render JS-heavy pages and bypass
bot detection (Cloudflare, etc.). Chromium is started on-demand via
systemd and auto-stops after 30 minutes idle.
Falls back to plain HTTP if Chromium is unavailable.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from typing import Optional

CDP_PORT = 9222
CDP_HOST = "127.0.0.1"
FETCH_TIMEOUT = 45        # increased — real rendering + bot checks take longer
RENDER_WAIT = 3           # wait after load for JS to execute
CLOUDFLARE_WAIT = 10      # extra wait if Cloudflare challenge detected
MAX_TEXT_CHARS = 350000
STARTUP_TIMEOUT = 15      # seconds to wait for Chromium cold start

SERVICE_NAME = "chromium-cdp-visible.service"

try:
    import websocket
    _HAS_WS = True
except ImportError:
    _HAS_WS = False


def _cdp_ping():
    """Quick check if Chromium CDP is responding."""
    try:
        urllib.request.urlopen(
            f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=2
        )
        return True
    except Exception:
        return False


def _ensure_chromium():
    """Ensure Chromium is running. Start it on-demand if needed.

    Uses systemctl to start the visible Chromium service.
    Returns True if CDP is available.
    """
    if _cdp_ping():
        return True

    # Start the systemd service
    try:
        subprocess.run(
            ["systemctl", "--user", "start", SERVICE_NAME],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass

    # Wait for CDP to become available
    for _ in range(int(STARTUP_TIMEOUT / 0.3)):
        if _cdp_ping():
            return True
        time.sleep(0.3)

    return False


def shutdown_chromium():
    """Stop the Chromium service to free memory."""
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", SERVICE_NAME],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _cdp_fetch(url, timeout=FETCH_TIMEOUT):
    """Fetch a URL via Chromium CDP. Returns rendered text or None."""
    if not _HAS_WS:
        return None

    try:
        r = urllib.request.urlopen(
            f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=5
        )
        pages = json.loads(r.read())
        page_ws = next(
            (p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page"),
            None,
        )
        if not page_ws:
            return None
        ws = websocket.create_connection(page_ws, timeout=timeout, origin="*")
    except Exception:
        return None

    try:
        msg_id = 0
        def cdp(method, params=None):
            nonlocal msg_id
            msg_id += 1
            msg = {"id": msg_id, "method": method}
            if params:
                msg["params"] = params
            ws.send(json.dumps(msg))
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = ws.recv()
                data = json.loads(raw)
                if data.get("id") == msg_id:
                    return data.get("result", {})
            return {}

        cdp("Page.enable")
        cdp("Runtime.enable")
        cdp("Network.enable")

        # Set a realistic User-Agent (strip HeadlessChrome)
        cdp("Network.setUserAgentOverride", {
            "userAgent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "platform": "Linux x86_64",
        })

        cdp("Page.navigate", {"url": url})

        # Wait for page load
        deadline = time.time() + timeout
        load_fired = False
        while time.time() < deadline:
            data = json.loads(ws.recv())
            if data.get("method") == "Page.loadEventFired":
                load_fired = True
                break
        if not load_fired:
            return None

        time.sleep(RENDER_WAIT)

        # Check for Cloudflare challenge — wait longer if detected
        title_result = cdp("Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True,
        })
        page_title = title_result.get("result", {}).get("value", "")

        # Cloudflare / bot challenge indicators
        if any(s in page_title.lower() for s in [
            "just a moment", "attention required", "checking your browser",
            "ddos protection", "cloudflare",
        ]):
            # Wait for challenge to resolve
            time.sleep(CLOUDFLARE_WAIT)
        else:
            time.sleep(1)

        # Dismiss cookie consent banners
        cdp("Runtime.evaluate", {
            "expression": (
                "var b=document.querySelector('#sp-cc-accept')"
                "||document.querySelector('#onetrust-accept-btn-handler')"
                "||document.querySelector('[data-testid=\"cookie-policy-dialog-accept-button\"]')"
                "||document.querySelector('button[id*=\"accept\"]');"
                "if(b)b.click()"
            ),
            "returnByValue": True,
        })
        time.sleep(1)

        # Scroll down to trigger lazy-loaded content
        cdp("Runtime.evaluate", {
            "expression": "window.scrollTo(0, document.body.scrollHeight/3)",
            "returnByValue": True,
        })
        time.sleep(2)

        # Extract clean text
        js = (
            "(function(){"
            "var c=document.body.cloneNode(true);"
            "c.querySelectorAll('script,style,noscript,svg,[aria-hidden=true],[hidden]').forEach(function(e){e.remove()});"
            "var t=(c.innerText||c.textContent||'').replace(/\\n{3,}/g,'\\n\\n').trim();"
            "return(document.title?document.title+'\\n\\n':'')+t;"
            "})()"
        )
        result = cdp("Runtime.evaluate", {"expression": js, "returnByValue": True})
        text = result.get("result", {}).get("value", "")
        if text and len(text) > 50:
            return text[:MAX_TEXT_CHARS]

        # Retry extraction after additional wait
        time.sleep(5)
        result = cdp("Runtime.evaluate", {"expression": js, "returnByValue": True})
        text = result.get("result", {}).get("value", "")
        return text[:MAX_TEXT_CHARS] if text else None
    except Exception:
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _http_fetch(url):
    """Fallback: plain HTTP fetch with HTML stripping."""
    from html.parser import HTMLParser
    import random

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = False
        def handle_starttag(self, tag, attrs):
            if tag.lower() in ("script", "style"):
                self._skip = True
        def handle_endtag(self, tag):
            if tag.lower() in ("script", "style"):
                self._skip = False
        def handle_data(self, data):
            if not self._skip:
                t = data.strip()
                if t:
                    self.parts.append(t)
        def get_text(self):
            return chr(10).join(self.parts)

    ua = random.choice([
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
    ])
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        ext = _TextExtractor()
        ext.feed(html)
        return ext.get_text()[:MAX_TEXT_CHARS]
    except Exception as e:
        return f"Error fetching webpage: {e}"


def fetch_webpage(url):
    """Smart webpage fetcher: Chromium CDP first, HTTP fallback.

    Starts Chromium on-demand if not already running.
    Uses a VISIBLE browser to bypass bot detection.
    """
    if not url or not url.startswith(("http://", "https://")):
        return f"Error: invalid URL: {url}"

    # URL-encode non-ASCII characters (fixes German umlaut URLs)
    from urllib.parse import quote, urlparse, urlunparse
    parsed = urlparse(url)
    encoded_path = quote(parsed.path, safe='/')
    encoded_url = urlunparse((
        parsed.scheme, parsed.netloc, encoded_path,
        parsed.params, parsed.query, parsed.fragment
    ))

    if _ensure_chromium():
        text = _cdp_fetch(encoded_url)
        if text and len(text) > 50:
            return text
    return _http_fetch(encoded_url)


if __name__ == "__main__":
    import sys
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    print(f"Fetching: {test_url}")
    result = fetch_webpage(test_url)
    print(result[:3000])
    print(f"\n--- {len(result)} chars total ---")
