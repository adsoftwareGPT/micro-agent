"""Unit tests for browser_action scroll JS generation.

The scroll action builds the exact window.scrollTo/scrollBy statement before
sending it over CDP. These tests verify the generated JS for each argument
combination WITHOUT needing a live browser (the CDP session is faked).
"""

import browser_action as ba


class _FakeSession:
    """Records the last eval'd JS instead of talking to a real browser."""

    def __init__(self, ws_url="", *a, **k):
        self.evals = []

    def eval(self, js):
        self.evals.append(js)
        return None

    def close(self):
        pass


def _run_scroll(x=None, y=None, selector=None, monkeypatch=None):
    """Run action_scroll with mocked browser startup + session, return result."""
    captured = {}

    def fake_ensure():
        return True

    def fake_ws_url(*a, **k):
        return "ws://fake"

    class CapturingSession(_FakeSession):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["session"] = self

    original_ensure = ba._ensure_chromium
    original_get_ws = ba._get_page_ws
    original_cdp = ba.CDPSession
    ba._ensure_chromium = fake_ensure
    ba._get_page_ws = fake_ws_url
    ba.CDPSession = CapturingSession
    try:
        result = ba.action_scroll(x=x, y=y, selector=selector)
        session = captured.get("session")
        return result, session
    finally:
        ba._ensure_chromium = original_ensure
        ba._get_page_ws = original_get_ws
        ba.CDPSession = original_cdp


def test_scroll_xy():
    result, session = _run_scroll(x=10, y=20)
    assert "window.scrollTo(10,20)" in result
    assert session is not None and session.evals, "expected at least one eval"


def test_scroll_y_only():
    result, session = _run_scroll(y=500)
    assert "window.scrollTo(0,500)" in result
    assert session is not None


def test_scroll_x_only():
    result, session = _run_scroll(x=300)
    assert "window.scrollTo(300,0)" in result
    assert session is not None


def test_scroll_no_args_scrolls_viewport():
    result, session = _run_scroll()
    assert "window.scrollBy(0,window.innerHeight)" in result
    assert session is not None


def test_scroll_selector_scrolls_into_view():
    result, session = _run_scroll(selector="#main")
    assert "Scrolled element" in result
    assert "#main" in result
    # the JS sent to the page should call scrollIntoView on the selector
    assert session is not None and session.evals
    assert any("scrollIntoView" in js and "#main" in js for js in session.evals)


def test_scroll_reports_browser_unavailable(monkeypatch):
    original_ensure = ba._ensure_chromium
    monkeypatch.setattr(ba, "_ensure_chromium", lambda: False)
    try:
        result = ba.action_scroll(y=10)
        assert "Error" in result
    finally:
        ba._ensure_chromium = original_ensure
