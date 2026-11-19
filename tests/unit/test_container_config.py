from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy.engine import make_url

from collector_offline import container
from collector_offline.config import PlcConfig, load_config, secret_from_env


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "site.yml"
    source = Path(__file__).parents[1] / "docker" / "site.yml"
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_secret_file_and_environment_are_mutually_exclusive(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("test-secret\n", encoding="utf-8")
    assert secret_from_env("TOKEN", {"TOKEN_FILE": str(secret)}) == "test-secret"
    with pytest.raises(ValueError, match="use only"):
        secret_from_env("TOKEN", {"TOKEN": "other", "TOKEN_FILE": str(secret)})
    with pytest.raises(ValueError, match="cannot read secret file"):
        secret_from_env("TOKEN", {"TOKEN_FILE": str(tmp_path / "missing")})


def test_database_password_is_url_encoded(monkeypatch, tmp_path: Path) -> None:
    password = "p@ss:/?#% secret"
    secret = tmp_path / "database_password"
    secret.write_text(password, encoding="utf-8")
    monkeypatch.delenv("COLLECTOR_OFFLINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    container.configure_database()
    url = make_url(__import__("os").environ["COLLECTOR_OFFLINE_DATABASE_URL"])
    assert url.host == "postgres" and url.password == password


def test_token_validation_does_not_echo_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as caught:
        load_config(
            config_file(tmp_path),
            {
                "COLLECTOR_OFFLINE_DATABASE_URL": "sqlite://",
                "COLLECTOR_OFFLINE_TOKEN": "sensitive-short-token",
            },
        )
    assert "sensitive-short-token" not in str(caught.value)


def test_configuration_validation_does_not_connect(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("COLLECTOR_OFFLINE_DATABASE_URL", "postgresql://unreachable/db")
    monkeypatch.setenv("COLLECTOR_OFFLINE_TOKEN", "isolated-token-with-at-least-24-chars")
    monkeypatch.delenv("COLLECTOR_OFFLINE_TOKEN_FILE", raising=False)
    container.validate(str(config_file(tmp_path)))
    assert json.loads(capsys.readouterr().out)["configuration"] == "valid"


def test_invalid_iana_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="IANA"):
        PlcConfig.model_validate(
            {"source": "simulator", "timezone": "Chile/Inventado", "tags": {"TEMP": "TEMP"}}
        )


def test_central_tag_id_limit_is_validated(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["plcs"][0]["simulator"]["tags"]["x" * 65] = 12
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="tag_id"):
        load_config(
            path,
            {"COLLECTOR_OFFLINE_DATABASE_URL": "sqlite://", "COLLECTOR_OFFLINE_TOKEN": "x" * 32},
        )
