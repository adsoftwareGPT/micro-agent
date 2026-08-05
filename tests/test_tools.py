"""Unit tests for micro.py tool-call handling and pure helpers.

These tests exercise deterministic logic only — no live LLM, browser, or
network calls. The shell tool is tested with safe local commands.
"""

import micro


class TestSummarize:
    def test_uses_tool_key(self):
        assert micro.summarize("shell", {"cmd": "ls -la"}) == "ls -la"

    def test_falls_back_to_json(self):
        # unknown tool with no mapped key -> JSON dump
        s = micro.summarize("unknown", {"x": 1})
        assert "x" in s and "1" in s

    def test_truncates_long_input(self):
        s = micro.summarize("shell", {"cmd": "x" * 500})
        assert len(s) <= 104  # 100 chars + "..."

    def test_empty_args_returns_placeholder(self):
        # an empty resolved string (not an actual value) hits the placeholder
        assert micro.summarize("shell", {"cmd": ""}) == "(no details)"


class TestNormalizeToolArgs:
    def test_maps_alias_to_canonical(self):
        # model sends "command" but the tool parameter is "cmd"
        args = micro._normalize_tool_args("shell", {"command": "ls"})
        assert args == {"cmd": "ls"}

    def test_does_not_clobber_canonical(self):
        args = micro._normalize_tool_args("shell", {"command": "a", "cmd": "b"})
        assert args["cmd"] == "b"

    def test_unknown_tool_untouched(self):
        args = {"foo": 1}
        assert micro._normalize_tool_args("nope", dict(args)) == args


class TestHandleToolCalls:
    def test_valid_call_runs_shell(self):
        messages = []
        tc = [{"function": {"name": "shell", "arguments": '{"cmd": "echo hi"}'}, "id": "1"}]
        micro.handle_tool_calls(tc, messages)
        assert messages[-1]["role"] == "tool"
        assert "hi" in messages[-1]["content"]

    def test_malformed_json_does_not_raise(self, capsys):
        # A garbage arguments string must be caught, not crash the loop.
        messages = []
        tc = [{"function": {"name": "shell", "arguments": "{not valid json"}, "id": "2"}]
        micro.handle_tool_calls(tc, messages)
        assert messages[-1]["role"] == "tool"

    def test_unknown_tool_reported(self):
        messages = []
        tc = [{"function": {"name": "does_not_exist", "arguments": "{}"}, "id": "3"}]
        micro.handle_tool_calls(tc, messages)
        assert "Unknown tool" in messages[-1]["content"]

    def test_bad_arguments_returned_to_model(self):
        # Wrong key name for a shell call -> TypeError handled gracefully.
        messages = []
        tc = [{"function": {"name": "shell", "arguments": '{"wrong_key": "x"}'}, "id": "4"}]
        micro.handle_tool_calls(tc, messages)
        assert "bad arguments" in messages[-1]["content"].lower()


class TestExecuteShell:
    def test_returns_stdout(self):
        out = micro.execute_shell("echo smoke-ok")
        assert "smoke-ok" in out

    def test_returns_stderr_when_rc_nonzero(self):
        out = micro.execute_shell("echo boom 1>&2; exit 3")
        assert "boom" in out

    def test_times_out_gracefully(self, monkeypatch):
        # Force a TimeoutExpired so the graceful error path is exercised.
        import subprocess

        def slow_run(*a, **k):
            raise subprocess.TimeoutExpired("x", 1)

        monkeypatch.setattr(subprocess, "run", slow_run)
        out = micro.execute_shell("sleep 5")
        assert "timed out" in out.lower()


class TestLegacyHttpFetch:
    """Test the pure HTML -> text extraction without doing network I/O."""

    def test_strips_script_and_style(self, monkeypatch):
        class FakeResp:
            headers = {"content-type": "text/html"}

            def raise_for_status(self):
                return None

            @property
            def text(self):
                return (
                    "<html><head><title>T</title></head><body>"
                    "<script>var x=1;</script>"
                    "Hello <b>World</b>"
                    "<style>.hidden{}</style>"
                    "Tail</body></html>"
                )

        class FakeSession:
            def get(self, url, timeout=30):
                return FakeResp()

        monkeypatch.setattr(micro, "_get_session", lambda: FakeSession())
        out = micro._legacy_http_fetch("https://example.com/")
        # JS and CSS content must not leak into the readable text
        assert "var x" not in out
        assert "hidden" not in out
        assert "Hello" in out and "World" in out and "Tail" in out

    def test_returns_error_on_failure(self, monkeypatch):
        class BoomSession:
            def get(self, url, timeout=30):
                raise RuntimeError("nope")

        monkeypatch.setattr(micro, "_get_session", lambda: BoomSession())
        out = micro._legacy_http_fetch("https://example.com/")
        assert "Error" in out
