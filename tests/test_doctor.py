import pytest
from click.testing import CliRunner

from invincible import __version__
from invincible.cli import cli

VALID_YAML = (
    "providers:\n  - name: solo\n    tier: 1\n    base_url: https://solo.example.com/v1\n"
    "    api_key_env: SOLO_API_KEY\n    model_id: solo-model\n"
)


@pytest.fixture(autouse=True)
def _clean_invincible_env(monkeypatch):
    """doctor reads INVINCIBLE_* env vars; earlier tests may leak them into
    the process environment, so make every test start from a clean slate."""
    monkeypatch.delenv("INVINCIBLE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("INVINCIBLE_DB_PATH", raising=False)


def _set_secrets(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gw-key")
    monkeypatch.setenv("MCP_SHARED_SECRET", "mcp-key")


def _invoke(args=None):
    return CliRunner().invoke(cli, args or ["doctor"])


def test_doctor_help():
    result = _invoke(["doctor", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "diagnostics" in result.output


def test_doctor_all_ok(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert f"Invincible version: {__version__}" in result.output
    assert "OK  providers.yaml exists" in result.output
    assert "OK  providers.yaml loads" in result.output
    assert "OK  session database accessible" in result.output
    assert "OK  GATEWAY_API_KEY exists" in result.output
    assert "OK  MCP_SHARED_SECRET exists" in result.output


def test_doctor_prints_version(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_missing_secrets_fail(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert "FAIL  GATEWAY_API_KEY exists" in result.output
    assert "FAIL  MCP_SHARED_SECRET exists" in result.output


def test_doctor_missing_providers_yaml_fails(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(missing))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert f"FAIL  providers.yaml exists  ({missing})" in result.output
    assert "FAIL  providers.yaml loads" in result.output


def test_doctor_malformed_providers_yaml_fails(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    bad = tmp_path / "providers.yaml"
    bad.write_text("key: [1, 2\n", encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(bad))
    monkeypatch.chdir(tmp_path)

    result = _invoke()
    assert result.exit_code == 1
    assert f"OK  providers.yaml exists  ({bad})" in result.output
    assert "FAIL  providers.yaml loads" in result.output


def test_doctor_inaccessible_session_db_fails(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    monkeypatch.setenv("INVINCIBLE_DB_PATH", str(blocker / "sessions.db"))

    result = _invoke()
    assert result.exit_code == 1
    assert "FAIL  session database accessible" in result.output


def test_doctor_uses_rich_console_when_available(monkeypatch, tmp_path):
    _set_secrets(monkeypatch)
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    monkeypatch.chdir(tmp_path)

    printed = []

    class FakeConsole:
        def print(self, text):
            printed.append(text)

    monkeypatch.setattr("invincible.cli._doctor_console", lambda: FakeConsole())
    result = _invoke()
    assert result.exit_code == 0
    assert f"Invincible version: {__version__}" in printed
    assert any("[green]OK[/green]" in line for line in printed)


def test_doctor_rich_console_propagates_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
    config = tmp_path / "providers.yaml"
    config.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr("invincible.cli._doctor_config_source", lambda: str(config))
    monkeypatch.chdir(tmp_path)

    printed = []

    class FakeConsole:
        def print(self, text):
            printed.append(text)

    monkeypatch.setattr("invincible.cli._doctor_console", lambda: FakeConsole())
    result = _invoke()
    assert result.exit_code == 1
    assert any("[red]FAIL[/red]  GATEWAY_API_KEY exists" in line for line in printed)
