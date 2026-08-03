#!/usr/bin/env python3
"""CDP-based webpage fetcher for micro.py.

Uses a VISIBLE Chromium instance to render JS-heavy pages and bypass
bot detection (Cloudflare, etc.). Chromium is started on-demand via
systemd and auto-stops after 30 minutes idle.
Falls back to plain HTTP if Chromium is unavailable.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
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


CHROMIUM_BIN_CANDIDATES = [
    "/snap/bin/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
]

SERVICE_FILE = os.path.expanduser(
    "~/.config/systemd/user/chromium-cdp-visible.service"
)


def find_chromium_binary():
    """Return the path to an installed Chromium/Chrome binary, or None."""
    for p in CHROMIUM_BIN_CANDIDATES:
        if os.path.isfile(p):
            return p
    for name in (
        "chromium", "chromium-browser", "google-chrome",
        "google-chrome-stable", "microsoft-edge",
    ):
        p = shutil.which(name)
        if p:
            return p
    return None


def _install_command():
    """Detect the package manager and return a Chromium install command.

    Returns a list of args (no sudo prefix), or None if unsupported.
    """
    if shutil.which("snap"):
        return ["snap", "install", "chromium"]
    if shutil.which("apt-get"):
        return ["sh", "-c", "apt-get update -y && apt-get install -y chromium-browser"]
    if shutil.which("dnf"):
        return ["dnf", "install", "-y", "chromium"]
    if shutil.which("yum"):
        return ["yum", "install", "-y", "chromium"]
    if shutil.which("pacman"):
        return ["pacman", "-S", "--noconfirm", "chromium"]
    if shutil.which("apk"):
        return ["apk", "add", "chromium"]
    return None


def ensure_chromium_installed(auto_install=True, verbose=True):
    """Ensure a Chromium/Chrome binary is available, installing if missing.

    Returns the binary path, or None if unavailable.

    Installation needs root and uses `sudo -n` (non-interactive), so it
    fails fast instead of blocking on a password prompt. If a password is
    required, the exact command to run manually is printed to stderr.
    """
    binary = find_chromium_binary()
    if binary:
        return binary
    if not auto_install:
        return None

    cmd = _install_command()
    if cmd is None:
        if verbose:
            print(
                "cdp_fetch: no supported package manager found; install "
                "Chromium manually (https://www.google.com/chrome/)",
                file=sys.stderr,
            )
        return None

    runner = cmd if os.geteuid() == 0 else ["sudo", "-n"] + cmd
    try:
        proc = subprocess.run(runner, capture_output=True, text=True, timeout=900)
    except Exception as e:
        if verbose:
            print(f"cdp_fetch: chromium install failed to run: {e}", file=sys.stderr)
        return None

    if proc.returncode != 0:
        if verbose:
            tail = (proc.stderr or proc.stdout or "").strip()
            print("cdp_fetch: chromium install failed:", file=sys.stderr)
            if tail:
                print("  " + tail.replace("\n", "\n  "), file=sys.stderr)
            print("  Run it manually: sudo " + " ".join(cmd), file=sys.stderr)
        return None

    binary = find_chromium_binary()
    if binary is None:
        if verbose:
            print(
                "cdp_fetch: chromium installed but binary not found in known "
                "paths; run manually: sudo " + " ".join(cmd),
                file=sys.stderr,
            )
        return None
    return binary


def _ensure_chromium():
    """Ensure Chromium is running. Install + start it on-demand if needed.

    Uses systemctl to start the visible Chromium service; installs
    Chromium first if no binary is present.
    Returns True if CDP is available.
    """
    if _cdp_ping():
        return True

    # Install Chromium on-demand if it's missing
    if not find_chromium_binary():
        if not ensure_chromium_installed():
            return False

    # Warn if the service file points at a binary that isn't installed
    if os.path.isfile(SERVICE_FILE):
        try:
            with open(SERVICE_FILE) as f:
                m = re.search(r"^ExecStart=(\S+)", f.read(), re.M)
            if m and not os.path.isfile(m.group(1)):
                print(
                    f"cdp_fetch: warning: {SERVICE_NAME} ExecStart points at "
                    f"{m.group(1)} (missing); found {find_chromium_binary()}",
                    file=sys.stderr,
                )
        except Exception:
            pass

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
            if not _HAS_WS:
                print(
                    "cdp_fetch: warning: Chromium is running but the "
                    "'websocket-client' Python package is missing; CDP fetch "
                    "will fall back to plain HTTP. Install with: "
                    "pip install websocket-client",
                    file=sys.stderr,
                )
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


def _curl_get(url, timeout=30):
    """Fetch raw HTML with curl: HTTP/2, modern TLS, gzip/brotli, redirects.

    Returns decoded HTML string, or None on failure.
    """
    ua = random.choice([
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
    ])
    try:
        proc = subprocess.run(
            [
                "curl", "-sS", "-L", "--compressed", "--http2",
                "--max-time", str(timeout),
                "-A", ua,
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "-H", "Upgrade-Insecure-Requests: 1",
                "-H", "Sec-Fetch-Dest: document",
                "-H", "Sec-Fetch-Mode: navigate",
                "-H", "Sec-Fetch-Site: none",
                "--tls-max", "1.3",
                url,
            ],
            capture_output=True, timeout=timeout + 5,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        return proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


def _urllib_get(url, timeout=30):
    """Last-resort fetch with urllib (used only if curl is unavailable).

    Returns decoded HTML string, or None on failure.
    """
    ua = random.choice([
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
    ])
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        # urllib does not auto-decompress; handle gzip manually
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            import gzip
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _http_fetch(url):
    """Fallback: curl first (HTTP/2 + modern TLS), urllib as last resort.

    Still no JS rendering — that requires Chromium — but curl's HTTP/2 and
    TLS fingerprinting succeed where urllib gets blocked by bot checks.
    """
    from html.parser import HTMLParser

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

    html = _curl_get(url)
    if html is None:
        html = _urllib_get(url)
    if not html:
        return f"Error fetching webpage: no usable response from {url}"

    ext = _TextExtractor()
    ext.feed(html)
    text = ext.get_text()
    if not text:
        return f"Error fetching webpage: empty content from {url}"
    return text[:MAX_TEXT_CHARS]


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
        print(
            "cdp_fetch: warning: Chromium CDP returned no usable content for "
            f"{encoded_url}; falling back to plain HTTP",
            file=sys.stderr,
        )
    else:
        print(
            "cdp_fetch: warning: Chromium CDP unavailable; falling back to "
            "plain HTTP (no JS rendering, weaker bot-detection bypass)",
            file=sys.stderr,
        )
    return _http_fetch(encoded_url)


if __name__ == "__main__":
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]

    if "--install-chromium" in flags or "-i" in flags:
        path = ensure_chromium_installed()
        print(f"Chromium: {path or 'NOT INSTALLED'}")
        sys.exit(0 if path else 1)

    test_url = positional[0] if positional else "https://example.com"
    print(f"Fetching: {test_url}")
    result = fetch_webpage(test_url)
    print(result[:3000])
    print(f"\n--- {len(result)} chars total ---")
