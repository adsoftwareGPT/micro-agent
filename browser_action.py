#!/usr/bin/env python3
"""Browser action tool for micro.py — interactive browser automation via CDP.

Provides click, type, scroll, wait, screenshot, and a generic
login_with_google() helper that handles the full Google Identity Services
(GIS) OAuth popup flow AND the FedCM (Federated Credential Management) API.

Requires the visible Chromium on port 9222 (same as cdp_fetch.py).
"""

import json
import time
import urllib.request

CDP_PORT = 9222
CDP_HOST = "127.0.0.1"
DEFAULT_TIMEOUT = 30

try:
    import websocket

    _HAS_WS = True
except ImportError:
    _HAS_WS = False


# ── Low-level CDP helpers ───────────────────────────────────────────────────


def _cdp_ping():
    try:
        urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _ensure_chromium():
    """Start Chromium if not running (delegates to cdp_fetch)."""
    if _cdp_ping():
        return True
    try:
        from cdp_fetch import _ensure_chromium as _ec

        return _ec()
    except Exception:
        return False


def _get_pages():
    """Return list of CDP page targets."""
    r = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5)
    return [t for t in json.loads(r.read()) if t["type"] == "page"]


def _get_all_targets():
    """Return ALL browser targets including popups (uses browser-level CDP).

    Unlike _get_pages() which only sees /json/list (attached targets),
    this uses Target.getTargets via the browser websocket to find
    native popup windows that aren't in /json/list.
    """
    try:
        r = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=5)
        ver = json.loads(r.read())
        browser_ws = ver.get("webSocketDebuggerUrl")
        if not browser_ws:
            return []
        ws = websocket.create_connection(browser_ws, timeout=5, origin="*")
        ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
        resp = json.loads(ws.recv())
        ws.close()
        return resp.get("result", {}).get("targetInfos", [])
    except Exception:
        return []


def _find_google_popup():
    """Find a Google account chooser popup among ALL targets.

    Returns the target dict (may not have webSocketDebuggerUrl for native popups).
    """
    targets = _get_all_targets()
    for t in targets:
        if t.get("type") != "page":
            continue
        u = t.get("url", "")
        if "accounts.google.com" in u and "gsi/button" not in u and "gsi/client" not in u and "gsi/iframe" not in u:
            return t
    return None


def _attach_to_target(target_id):
    """Attach to a CDP target that has no webSocketDebuggerUrl (native popup).

    Returns (browser_ws, session_id) or (None, None).
    """
    try:
        r = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/version", timeout=5)
        ver = json.loads(r.read())
        browser_ws_url = ver.get("webSocketDebuggerUrl")
        if not browser_ws_url:
            return None, None
        ws = websocket.create_connection(browser_ws_url, timeout=10, origin="*")
        ws.send(
            json.dumps({"id": 1, "method": "Target.attachToTarget", "params": {"targetId": target_id, "flatten": True}})
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            resp = json.loads(ws.recv())
            if resp.get("id") == 1:
                sid = resp.get("result", {}).get("sessionId")
                if sid:
                    return ws, sid
            if resp.get("method") == "Target.attachedToTarget":
                info = resp.get("params", {})
                if info.get("targetInfo", {}).get("targetId") == target_id:
                    return ws, info.get("sessionId")
        ws.close()
        return None, None
    except Exception:
        return None, None


def _cdp_session_eval(ws, session_id, js, msg_id=1):
    """Evaluate JS in a target via a flattened session."""
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "method": "Runtime.evaluate",
                "sessionId": session_id,
                "params": {"expression": js, "returnByValue": True},
            }
        )
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        resp = json.loads(ws.recv())
        if resp.get("id") == msg_id:
            return resp.get("result", {}).get("result", {}).get("value")
    return None


def _get_page_ws(url_contains: str = "", title_contains: str = ""):
    """Find a page target by URL or title substring, return its websocket URL."""
    pages = _get_pages()
    for p in pages:
        u = p.get("url", "")
        t = p.get("title", "")
        if url_contains and url_contains in u:
            return p["webSocketDebuggerUrl"]
        if title_contains and title_contains.lower() in t.lower():
            return p["webSocketDebuggerUrl"]
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    return None


class CDPSession:
    """Thin wrapper over a CDP websocket connection."""

    def __init__(self, ws_url, timeout=DEFAULT_TIMEOUT):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, origin="*")
        self._id = 0
        self.timeout = timeout

    def send_raw(self, msg):
        """Send a raw message and return the next response."""
        self.ws.send(json.dumps(msg))

    def recv_raw(self):
        """Receive a raw message (may be event or response)."""
        return json.loads(self.ws.recv())

    def cdp(self, method, params=None):
        """Send a CDP command and return its result."""
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._id:
                return data.get("result", {})
        return {}

    def eval(self, js, return_by_value=True):
        """Evaluate JS and return the result."""
        r = self.cdp(
            "Runtime.evaluate",
            {
                "expression": js,
                "returnByValue": return_by_value,
                "awaitPromise": True,
            },
        )
        if return_by_value:
            return r.get("result", {}).get("value")
        return r

    def navigate(self, url, wait=5):
        """Navigate to a URL and wait for load."""
        self.cdp("Page.enable")
        self.cdp("Page.navigate", {"url": url})
        time.sleep(wait)

    def click_at(self, x, y):
        """Dispatch a real mouse click at viewport coordinates via Input domain."""
        self.cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(0.05)
        self.cdp(
            "Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}
        )
        time.sleep(0.05)
        self.cdp(
            "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}
        )

    def click_selector(self, selector):
        """Click an element by CSS selector. Returns True if found and clicked."""
        result = self.eval(f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return 'not_found';
                el.scrollIntoView({{block: 'center'}});
                el.click();
                return el.textContent.trim().substring(0, 80);
            }})()
        """)
        return result != "not_found"

    def click_text(self, text, tag=None):
        """Click the first element whose text contains the given string.

        Uses CDP Input domain mouse events (not JS .click()) because
        React/Next.js buttons often ignore JS click events.
        """
        pos_result = self.eval(f"""
            (function() {{
                var els = document.querySelectorAll('{tag or "*"}');
                for (var i = 0; i < els.length; i++) {{
                    if (els[i].textContent.trim().includes({json.dumps(text)})) {{
                        els[i].scrollIntoView({{block:'center'}});
                        var rect = els[i].getBoundingClientRect();
                        if (rect.width > 0) {{
                            return JSON.stringify({{x: rect.left + rect.width/2, y: rect.top + rect.height/2, found: true}});
                        }}
                    }}
                }}
                return JSON.stringify({{found: false}});
            }})()
        """)
        if not pos_result:
            return False
        pos = json.loads(pos_result) if isinstance(pos_result, str) else pos_result
        if not pos.get("found"):
            return False
        # Click via CDP Input domain only
        self.click_at(pos["x"], pos["y"])
        time.sleep(0.5)
        return True

    def type_text(self, selector, text):
        """Type text into an input element."""
        focused = self.eval(f"""
            (function() {{
                var el = document.querySelector({json.dumps(selector)});
                if (!el) return 'not_found';
                el.focus();
                el.value = '';
                return 'focused';
            }})()
        """)
        if focused != "focused":
            return False
        for char in text:
            self.cdp("Input.dispatchKeyEvent", {"type": "char", "text": char})
            time.sleep(0.01)
        return True

    def scroll(self, x=0, y=0):
        """Scroll the page."""
        self.eval(f"window.scrollTo({x}, {y})")

    def get_text(self, max_chars=2000):
        """Get the visible text of the page."""
        return self.eval(f"(document.body ? document.body.innerText : '').substring(0, {max_chars})") or ""

    def get_page_info(self):
        """Get URL, title, and body snippet."""
        return self.eval("""
            JSON.stringify({
                url: window.location.href,
                title: document.title,
                body: (document.body ? document.body.innerText : '').substring(0, 1000)
            })
        """)

    def screenshot(self, path="/tmp/browser_screenshot.jpg"):
        """Take a screenshot and save to file."""
        r = self.cdp("Page.captureScreenshot", {"format": "jpeg", "quality": 70})
        import base64

        data = base64.b64decode(r.get("data", ""))
        with open(path, "wb") as f:
            f.write(data)
        return f"Screenshot saved: {path} ({len(data)} bytes)"

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ── High-level actions ─────────────────────────────────────────────────────


def action_navigate(url, wait=5):
    """Navigate the browser to a URL."""
    if not _ensure_chromium():
        return "Error: Chromium not available"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        s.navigate(url, wait)
        info = json.loads(s.get_page_info())
        return f"Navigated to: {info['url']}\nTitle: {info['title']}\n\n{info['body'][:500]}"
    finally:
        s.close()


def action_click(selector=None, text=None, x=None, y=None, iframe_selector=None):
    """Click an element by CSS selector, text match, or coordinates."""
    if not _ensure_chromium():
        return "Error: Chromium not available"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        if selector:
            if iframe_selector:
                pos_str = s.eval(f"""
                    (function() {{
                        var iframe = document.querySelector({json.dumps(iframe_selector)});
                        if (!iframe) return null;
                        var rect = iframe.getBoundingClientRect();
                        return JSON.stringify({{x: rect.left + rect.width/2, y: rect.top + rect.height/2}});
                    }})()
                """)
                if pos_str:
                    pos = json.loads(pos_str) if isinstance(pos_str, str) else pos_str
                    s.click_at(pos["x"], pos["y"])
                    return f"Clicked iframe at ({pos['x']:.0f}, {pos['y']:.0f})"
            ok = s.click_selector(selector)
            return f"Clicked '{selector}': {'success' if ok else 'not found'}"
        elif text:
            ok = s.click_text(text)
            return f"Clicked text '{text}': {'success' if ok else 'not found'}"
        elif x is not None and y is not None:
            s.click_at(x, y)
            return f"Clicked at ({x}, {y})"
        else:
            return "Error: provide selector, text, or x/y coordinates"
    finally:
        s.close()


def action_type(selector, text):
    """Type text into an input field."""
    if not _ensure_chromium():
        return "Error: Chromium not available"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        ok = s.type_text(selector, text)
        return f"Typed into '{selector}': {'success' if ok else 'element not found'}"
    finally:
        s.close()


def action_get_state():
    """Get the current state of the browser — all open tabs with URLs and titles."""
    if not _ensure_chromium():
        return "Error: Chromium not available"
    pages = _get_pages()
    lines = [f"=== {len(pages)} open tabs ==="]
    for i, p in enumerate(pages):
        lines.append(f"  {i + 1}. [{p.get('title', '')[:40]}] {p.get('url', '')[:100]}")
    if pages:
        ws_url = pages[0]["webSocketDebuggerUrl"]
        s = CDPSession(ws_url)
        try:
            info = json.loads(s.get_page_info())
            lines.append("\n=== Active tab ===")
            lines.append(f"URL: {info['url']}")
            lines.append(f"Title: {info['title']}")
            lines.append(f"\n{info['body'][:800]}")
        finally:
            s.close()
    return "\n".join(lines)


def action_wait(seconds=5, url_contains=None, text_contains=None, timeout=30):
    """Wait for a condition: time, URL change, or text appearance."""
    if not _ensure_chromium():
        return "Error: Chromium not available"
    if not url_contains and not text_contains:
        time.sleep(seconds)
        return f"Waited {seconds}s"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            info = json.loads(s.get_page_info())
            if url_contains and url_contains in info.get("url", ""):
                return f"URL matched '{url_contains}': {info['url'][:100]}"
            if text_contains and text_contains.lower() in info.get("body", "").lower():
                return f"Text found '{text_contains}'"
            time.sleep(1)
        return f"Timeout after {timeout}s — condition not met"
    finally:
        s.close()


def action_screenshot(path="/tmp/browser_screenshot.jpg"):
    """Take a screenshot."""
    if not _ensure_chromium():
        return "Error: Chromium not available"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        return s.screenshot(path)
    finally:
        s.close()


def action_eval(script):
    """Evaluate a JS expression in the page and return the result as a string.

    Lets the agent extract structured content itself (rather than relying on
    screenshots), e.g. on an infinite feed: count matching articles, pull
    their text/permalinks as JSON, dedup in the loop.
    """
    if not _ensure_chromium():
        return "Error: Chromium not available"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        val = s.eval(script)
        if val is None:
            return "null"
        return str(val)
    finally:
        s.close()


def action_scroll(x=None, y=None, selector=None):
    """Scroll the page.

    Mirrors newbrowser/browser.py:926 — a deliberately dumb primitive. The
    agent (LLM) owns the loop: it calls `eval` to extract content, then this
    to scroll, repeat. Keeping the tool dumb avoids the failure mode where a
    site-specific container/WheelEvent heuristic breaks scrolling everywhere.
    """
    if not _ensure_chromium():
        return "Error: Chromium not available"
    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"
    s = CDPSession(ws_url)
    try:
        if selector:
            s.eval(
                "var el = document.querySelector(" + json.dumps(selector) + "); "
                "if (el) el.scrollIntoView({block: 'center'});"
            )
            return "Scrolled element '" + str(selector) + "' into view"
        if x is not None and y is not None:
            js = "window.scrollTo(" + str(int(x)) + "," + str(int(y)) + ")"
        elif y is not None:
            js = "window.scrollTo(0," + str(int(y)) + ")"
        elif x is not None:
            js = "window.scrollTo(" + str(int(x)) + ",0)"
        else:
            js = "window.scrollBy(0,window.innerHeight)"
        s.eval(js)
        return "Scrolled: " + js
    finally:
        s.close()


# ── login_with_google ──────────────────────────────────────────────────────


def _handle_fedcm_dialog(ws, account_email=None):
    """Listen for and handle a FedCM dialog via CDP.

    Returns (success, message).
    """
    # Enable FedCM domain
    ws.send(json.dumps({"id": 999, "method": "FedCm.enable", "params": {"disableRejectionDelay": True}}))

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            ws.settimeout(3)
            raw = ws.recv()
            data = json.loads(raw)

            if data.get("method") == "FedCm.dialogShown":
                params = data.get("params", {})
                accounts = params.get("accounts", [])
                dialog_id = params.get("dialogId", "0")

                if not accounts:
                    return False, "FedCM dialog shown but no accounts"

                # Find the right account
                idx = 0
                if account_email:
                    for i, acc in enumerate(accounts):
                        if acc.get("email") == account_email:
                            idx = i
                            break

                selected = accounts[idx]
                ws.send(
                    json.dumps(
                        {
                            "id": 1000,
                            "method": "FedCm.selectAccount",
                            "params": {
                                "dialogId": dialog_id,
                                "accountIndex": idx,
                            },
                        }
                    )
                )

                # Wait for selectAccount response
                resp_deadline = time.time() + 5
                while time.time() < resp_deadline:
                    raw2 = ws.recv()
                    data2 = json.loads(raw2)
                    if data2.get("id") == 1000:
                        return True, f"Selected account: {selected.get('email')} ({selected.get('name')})"

                return True, f"Selected account: {selected.get('email')} (no confirmation)"

        except Exception:
            continue

    return False, "No FedCM dialog appeared within 15s"


def login_with_google(
    site_url,
    login_page_url=None,
    google_btn_selector=None,
    google_btn_text="Continue with Google",
    account_email=None,
    wait_after_login=12,
):
    """Log in to a website using Google OAuth.

    Handles TWO Google login mechanisms:
    1. FedCM (Federated Credential Management) — native browser dialog
       Sites: Pinterest, Reddit (newer), etc.
    2. GIS Popup — Google Identity Services popup window
       Sites: Upwork, Reddit (older flow), etc.

    The method automatically detects which mechanism the site uses.
    """
    if not _ensure_chromium():
        return "Error: Chromium not available"
    if not _HAS_WS:
        return "Error: websocket-client not installed"

    login_url = login_page_url or f"{site_url.rstrip('/')}/login"
    site_domain = site_url.split("//")[1].split("/")[0]

    ws_url = _get_page_ws()
    if not ws_url:
        return "Error: No browser page found"

    s = CDPSession(ws_url)
    steps = []

    try:
        # Step 1: Navigate to login page
        s.navigate(login_url, 5)
        time.sleep(2)

        # Accept cookies
        s.eval("""
            var btns = document.querySelectorAll('button, a');
            for (var b of btns) {
                var t = b.textContent.trim().toLowerCase();
                if (t === 'accept all' || t === 'accept') { b.click(); break; }
            }
        """)
        time.sleep(1)

        info = json.loads(s.get_page_info())
        steps.append(f"1. Navigated to: {info['url'][:80]}")

        # Check if already logged in
        # Only "log in" / "sign in" / "continue with google" indicate NOT logged in
        # "sign up" alone doesn't count (logged-in pages often have sign-up links too)
        body_lower = info.get("body", "").lower()
        login_keywords = ["log in", "sign in", "login", "continue with google"]
        has_login_form = any(kw in body_lower for kw in login_keywords)
        # Already logged in? We only treat "log in" / "sign in" / "continue with
        # google" as indicating NOT logged in ("sign up" alone doesn't count —
        # logged-in pages often have sign-up links too).
        if not has_login_form:
            steps.append("   ✅ Already logged in (no login form visible)")
            return "\n".join(steps)

        # Step 2: Find and click the Google login button
        # Enable FedCM BEFORE clicking — the dialog may appear instantly
        s.ws.send(json.dumps({"id": 998, "method": "FedCm.enable", "params": {"disableRejectionDelay": True}}))

        google_clicked = False
        click_method = ""

        # Strategy A: Direct CSS selector
        if google_btn_selector and s.click_selector(google_btn_selector):
            google_clicked = True
            click_method = f"selector: {google_btn_selector}"

        # Strategy B: Text match
        if not google_clicked and s.click_text(google_btn_text):
            google_clicked = True
            click_method = f"text: '{google_btn_text}'"

        # Strategy C: GIS iframe button (click via Input domain)
        if not google_clicked:
            gis_info = s.eval("""
                (function() {
                    var iframe = document.querySelector('iframe[id*="gsi_"], iframe[src*="accounts.google.com/gsi"]');
                    var btn = document.getElementById('login_google_submit') ||
                              document.querySelector('button[class*="gsso"]') ||
                              document.querySelector('[data-provider="google"]');
                    if (btn) return JSON.stringify({type: 'button', id: btn.id});
                    if (iframe) {
                        var rect = iframe.getBoundingClientRect();
                        if (rect.width > 0) return JSON.stringify({type: 'iframe', x: rect.left+rect.width/2, y: rect.top+rect.height/2});
                    }
                    return JSON.stringify({type: 'none'});
                })()
            """)
            if gis_info:
                gis = json.loads(gis_info) if isinstance(gis_info, str) else gis_info
                if gis.get("type") == "button":
                    s.eval(f"""
                        window.__oauthUrl = null;
                        var orig = window.open;
                        window.open = function(url) {{ window.__oauthUrl = url; return orig.apply(window, arguments); }};
                        var btn = document.getElementById({json.dumps(gis.get("id", ""))});
                        if (btn) btn.click();
                    """)
                    google_clicked = True
                    click_method = f"GIS button (id: {gis.get('id', '')})"
                elif gis.get("type") == "iframe":
                    s.click_at(gis["x"], gis["y"])
                    google_clicked = True
                    click_method = f"GIS iframe at ({gis['x']:.0f}, {gis['y']:.0f})"

        if not google_clicked:
            steps.append("2. ❌ Could not find Google login button")
            steps.append(f"\nPage content:\n{s.get_text(500)}")
            return "\n".join(steps)

        steps.append(f"2. Clicked Google button ({click_method})")

        # Step 3: Handle the account selection
        # First check: did a FedCM dialog appear?
        time.sleep(3)

        # Try FedCM approach first (read pending events)
        fedcm_handled = False
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                s.ws.settimeout(2)
                try:
                    raw = s.ws.recv()
                    data = json.loads(raw)
                    if data.get("method") == "FedCm.dialogShown":
                        params = data.get("params", {})
                        accounts = params.get("accounts", [])
                        dialog_id = params.get("dialogId", "0")
                        steps.append(f"3. FedCM dialog: {len(accounts)} account(s)")
                        for acc in accounts:
                            steps.append(f"   - {acc.get('email')} ({acc.get('name')})")

                        # Select account
                        idx = 0
                        if account_email:
                            for i, acc in enumerate(accounts):
                                if acc.get("email") == account_email:
                                    idx = i
                                    break

                        s.ws.send(
                            json.dumps(
                                {
                                    "id": 1001,
                                    "method": "FedCm.selectAccount",
                                    "params": {"dialogId": dialog_id, "accountIndex": idx},
                                }
                            )
                        )
                        steps.append(f"4. Selected: {accounts[idx].get('email')}")
                        fedcm_handled = True
                        break
                except Exception:
                    break
        except Exception:
            pass

        # If FedCM didn't fire, check for Google popup window
        if not fedcm_handled:
            google_popup = None
            for _attempt in range(5):
                time.sleep(2)
                google_popup = _find_google_popup()
                if google_popup:
                    break

            if google_popup:
                steps.append(f"3. Found Google popup: {google_popup.get('url', '')[:80]}")
                target_id = google_popup.get("targetId")
                popup_ws = google_popup.get("webSocketDebuggerUrl")

                if popup_ws:
                    # Direct websocket connection (popup is attached)
                    s2 = CDPSession(popup_ws)
                    try:
                        time.sleep(2)
                        if account_email:
                            s2.click_selector(f"[data-identifier='{account_email}']")
                            steps.append(f"4. Selected: {account_email}")
                        else:
                            result = s2.eval(
                                "(function(){var e=document.querySelector('[data-identifier]');if(e){e.click();return e.getAttribute('data-identifier')}return 'none'})()"
                            )
                            steps.append(f"4. Auto-selected: {result}")
                        time.sleep(3)
                        # Handle consent if needed
                        ci = json.loads(s2.get_page_info())
                        if "accounts.google.com" in ci.get("url", ""):
                            s2.eval(
                                """(function(){var b=document.querySelector('#submit_approve_access')||document.querySelector('button[name=submit_approve_access]');if(b)b.click();else{var bs=document.querySelectorAll('button,div[role=button]');for(var b of bs){var t=b.textContent.trim().toLowerCase();if(t.includes('continue')||t.includes('allow')){b.click();break;}}}})()"""
                            )
                            steps.append("   Handled consent screen")
                    finally:
                        s2.close()
                elif target_id:
                    # Attach via browser-level CDP (native popup)
                    bws, sid = _attach_to_target(target_id)
                    if bws:
                        steps.append("   Attached to popup via Target.attachToTarget")
                        _cdp_session_eval(bws, sid, "Runtime.enable", msg_id=50)
                        _cdp_session_eval(bws, sid, "Page.enable", msg_id=51)
                        time.sleep(3)
                        if account_email:
                            sel = f"[data-identifier='{account_email}']"
                            _cdp_session_eval(
                                bws, sid, f"var e=document.querySelector('{sel}');if(e)e.click()", msg_id=52
                            )
                            steps.append(f"4. Selected: {account_email}")
                        else:
                            r = _cdp_session_eval(
                                bws,
                                sid,
                                "(function(){var e=document.querySelector('[data-identifier]');if(e){e.click();return e.getAttribute('data-identifier')}return 'none'})()",
                                msg_id=52,
                            )
                            steps.append(f"4. Auto-selected: {r}")
                        # Handle consent
                        time.sleep(5)
                        cr = _cdp_session_eval(
                            bws,
                            sid,
                            "(function(){var b=document.querySelector('#submit_approve_access')||document.querySelector('button[name=submit_approve_access]');if(b){b.click();return 'clicked'}var bs=document.querySelectorAll('button,div[role=button]');for(var b of bs){var t=b.textContent.trim().toLowerCase();if(t.includes('continue')||t.includes('allow')){b.click();return 'clicked:'+b.textContent.trim()}}return 'no consent'})()",
                            msg_id=53,
                        )
                        if cr and "click" in str(cr).lower():
                            steps.append(f"   Consent: {cr}")
                        bws.close()
                    else:
                        steps.append("   ⚠️ Could not attach to popup")
                else:
                    steps.append("   ⚠️ Popup has no targetId or WS URL")
            else:
                steps.append("3. No popup — Google may have auto-selected or login failed")

        # Step 5: Wait for redirect
        steps.append(f"5. Waiting {wait_after_login}s for redirect...")
        time.sleep(wait_after_login)

        # Check final state
        pages = _get_pages()
        site_pages = [p for p in pages if site_domain in p.get("url", "")]

        if site_pages:
            s3 = CDPSession(site_pages[0]["webSocketDebuggerUrl"])
            try:
                final_info = json.loads(s3.get_page_info())
                body = final_info.get("body", "").lower()
                logged_in = not any(kw in body for kw in ["log in", "sign in", "continue with google"])
                status = "✅ LOGGED IN" if logged_in else "⚠️ Login form still visible"
                steps.append(f"\n=== Result: {status} ===")
                steps.append(f"URL: {final_info['url'][:100]}")
                steps.append(f"Title: {final_info['title']}")
                steps.append(f"\n{final_info['body'][:400]}")
            finally:
                s3.close()
        else:
            steps.append("⚠️ Could not find site page after login")

        return "\n".join(steps)

    except Exception as e:
        return f"Error during Google login: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


# ── Main dispatcher ────────────────────────────────────────────────────────


def browser_action(action, **kwargs):
    """Main entry point — dispatch browser actions.

    Actions:
        navigate      - Go to a URL (url, wait)
        click         - Click element (selector, text, x, y, iframe_selector)
        type          - Type text into input (selector, text)
        scroll        - Scroll page (x, y, or selector)
        wait          - Wait for condition (seconds, url_contains, text_contains)
        screenshot    - Take screenshot (path)
        eval          - Run JS expression in page (script), return result as string
        get_state     - Get current browser state
        login_google  - Log in via Google OAuth (url, email, login_url, btn_text)
    """
    if not _ensure_chromium():
        return "Error: Chromium not available"

    actions = {
        "navigate": lambda: action_navigate(kwargs.get("url", ""), kwargs.get("wait", 5)),
        "click": lambda: action_click(
            selector=kwargs.get("selector"),
            text=kwargs.get("text"),
            x=kwargs.get("x"),
            y=kwargs.get("y"),
            iframe_selector=kwargs.get("iframe_selector"),
        ),
        "type": lambda: action_type(kwargs.get("selector", ""), kwargs.get("text", "")),
        "scroll": lambda: action_scroll(kwargs.get("x"), kwargs.get("y"), kwargs.get("selector")),
        "wait": lambda: action_wait(
            seconds=kwargs.get("seconds", 5),
            url_contains=kwargs.get("url_contains"),
            text_contains=kwargs.get("text_contains"),
            timeout=kwargs.get("timeout", 30),
        ),
        "screenshot": lambda: action_screenshot(kwargs.get("path", "/tmp/browser_screenshot.jpg")),
        "eval": lambda: action_eval(kwargs.get("script", "")),
        "get_state": lambda: action_get_state(),
        "login_google": lambda: login_with_google(
            site_url=kwargs.get("url", ""),
            login_page_url=kwargs.get("login_url"),
            google_btn_selector=kwargs.get("btn_selector"),
            google_btn_text=kwargs.get("btn_text", "Continue with Google"),
            account_email=kwargs.get("email"),
            wait_after_login=kwargs.get("wait", 12),
        ),
    }

    fn = actions.get(action)
    if not fn:
        return f"Unknown action: '{action}'. Available: {', '.join(actions.keys())}"

    try:
        return fn()
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: browser_action.py <action> [args...]")
        print("Actions: navigate, click, type, scroll, eval, wait, screenshot, get_state, login_google")
        sys.exit(1)

    action = sys.argv[1]
    kwargs = {}
    for arg in sys.argv[2:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            try:
                v = int(v)
            except ValueError:
                pass
            kwargs[k] = v
        else:
            if action in ("navigate", "login_google") and "url" not in kwargs:
                kwargs["url"] = arg

    print(browser_action(action, **kwargs))
