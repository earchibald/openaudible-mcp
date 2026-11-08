import pytest

from openaudible_mcp.config import ConfigError, Settings


def test_env_and_placeholders(monkeypatch):
    monkeypatch.setenv("OPENAUDIBLE_URL", "nas.local:8000/")
    monkeypatch.setenv("OPENAUDIBLE_USERNAME", "admin")
    monkeypatch.setenv("OPENAUDIBLE_PASSWORD", "${user_config.password}")
    monkeypatch.setenv("OPENAUDIBLE_MP3_BITRATE", "")
    s = Settings()
    assert s.url == "http://nas.local:8000"
    assert s.credentials() == ("admin", "")
    assert s.mp3_bitrate == "96k"


def test_no_login(monkeypatch):
    for k in ("OPENAUDIBLE_USERNAME", "OPENAUDIBLE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert Settings().credentials() is None
    with pytest.raises(ConfigError):
        Settings(password="x").credentials()


def test_args_override_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAUDIBLE_USERNAME", "env-user")
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    s = Settings().apply_args(["--url", "http://h:9000", "--username", "arg-user", "--password-file", str(pw)])
    assert s.url == "http://h:9000"
    assert s.credentials() == ("arg-user", "s3cret")
    assert "s3cret" not in repr(s)


def test_env_file_fills_gaps_only(monkeypatch, tmp_path):
    for k in ("OPENAUDIBLE_URL", "OPENAUDIBLE_USERNAME", "OPENAUDIBLE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAUDIBLE_URL", "http://from-env:8000")
    monkeypatch.setenv("OPENAUDIBLE_PASSWORD", "")  # empty, as a launcher may pass it
    f = tmp_path / ".env"
    f.write_text('# comment\nexport OPENAUDIBLE_USERNAME="me"\nOPENAUDIBLE_PASSWORD=\'p w\'\n'
                 "OPENAUDIBLE_URL=http://from-file:8000\nOTHER=ignored\n")
    monkeypatch.chdir(tmp_path)
    s = Settings().apply_args([])
    assert s.url == "http://from-env:8000"
    assert s.credentials() == ("me", "p w")
    assert "OTHER" not in __import__("os").environ
