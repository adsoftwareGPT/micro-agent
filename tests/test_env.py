"""Unit tests for the built-in .env parser and config-path helpers."""

import os

import pytest

import micro


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point MAGENT_CONFIG_DIR at a temp dir and reload the env parser."""
    monkeypatch.setenv("MAGENT_CONFIG_DIR", str(tmp_path))
    return tmp_path


class TestFindResource:
    def test_returns_path_when_file_exists(self, isolated_config, tmp_path):
        (tmp_path / "foo.txt").write_text("hello")
        assert micro._find_resource("foo.txt") == str(tmp_path / "foo.txt")

    def test_returns_none_when_missing(self, isolated_config):
        assert micro._find_resource("does-not-exist.txt") is None


class TestDesiredResource:
    def test_creates_dir_and_returns_path(self, isolated_config, tmp_path):
        # _desired_resource takes just a filename key against config dir
        result = micro._desired_resource("new.env")
        assert result == str(tmp_path / "new.env")
        # directory is created lazily
        assert tmp_path.is_dir()


class TestLoadEnvFile:
    def test_parses_key_value_and_quotes(self, isolated_config, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text(
            "# comment\n"
            "FOO=bar\n"
            'QUOTED="value"\n'
            "SINGLE='v2'\n"
            "NOEQ\n"  # skipped: no '='
            ""
        )
        # isolate from any real env pollution
        for k in ("FOO", "QUOTED", "SINGLE"):
            monkeypatch.delenv(k, raising=False)
        micro._load_env_file()
        assert os.environ.get("FOO") == "bar"
        assert os.environ.get("QUOTED") == "value"
        assert os.environ.get("SINGLE") == "v2"

    def test_setdefault_does_not_overwrite_existing(self, isolated_config, tmp_path, monkeypatch):
        monkeypatch.setenv("EXISTS", "original")
        (tmp_path / ".env").write_text("EXISTS=ignored\n")
        micro._load_env_file()
        assert os.environ.get("EXISTS") == "original"

    def test_no_env_file_is_noop(self, isolated_config):
        # should not raise when no .env present
        micro._load_env_file()
