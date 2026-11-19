from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class TagSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1)
    data_type: Literal["auto", "bool", "int", "float", "state"] = "auto"
    event: bool | None = None
    label: str | None = None

    @model_validator(mode="before")
    @classmethod
    def from_legacy_address(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"address": value}
        return value


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ip: str | None = None
    slot: int = 0
    opc_server_url: str | None = None
    timeout_sec: float = Field(default=5.0, gt=0, le=60)
    username_env: str | None = None
    password_env: str | None = None


class OpcuaModuleConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    driver: Literal["siemens", "opcua"] = "siemens"
    ip: str | None = None
    opc_server_url: str | None = None
    timeout_sec: float = Field(default=5.0, gt=0, le=60)
    username_env: str | None = None
    password_env: str | None = None
    tags: dict[str, TagSpec]


class SimulatorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    seed: int | None = None
    tags: dict[str, Any] = Field(default_factory=dict)


class PlcConfig(BaseModel):
    """Compatible con el YAML actual del Collector Central.

    Los campos adicionales del YAML central se ignoran de forma deliberada; los
    tags escalares se convierten al formato enriquecido local.
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    lagoon_id: str | None = None
    source: Literal["rockwell", "siemens", "simulator"]
    timezone: str = "UTC"
    tags: dict[str, TagSpec] = Field(default_factory=dict)
    event_tags: dict[str, str] = Field(default_factory=dict)
    tag_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rockwell: ConnectionConfig | None = None
    siemens: ConnectionConfig | None = None
    simulator: SimulatorConfig | None = None
    opcua_modules: list[OpcuaModuleConfig] = Field(default_factory=list)
    force_reconnect_every_sec: int = Field(default=3600, ge=30)
    max_consecutive_fails: int = Field(default=10, ge=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA zone") from exc
        return value

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def apply_tag_options_and_validate_connection(self) -> PlcConfig:
        for tag_id, label in self.event_tags.items():
            if tag_id not in self.tags:
                raise ValueError(f"event_tags references unknown tag {tag_id!r}")
            merged = {
                **self.tags[tag_id].model_dump(),
                "event": True,
                "label": label,
            }
            self.tags[tag_id] = TagSpec.model_validate(merged)

        for tag_id, options in self.tag_options.items():
            if tag_id not in self.tags:
                raise ValueError(f"tag_options references unknown tag {tag_id!r}")
            merged = {**self.tags[tag_id].model_dump(), **options}
            self.tags[tag_id] = TagSpec.model_validate(merged)

        if self.source == "rockwell":
            if not self.rockwell or not self.rockwell.ip:
                raise ValueError("rockwell.ip is required for source=rockwell")
            if not self.tags:
                raise ValueError("tags is required for source=rockwell")
        if (
            self.source == "siemens"
            and not self.opcua_modules
            and (not self.siemens or not self.siemens.opc_server_url)
        ):
            raise ValueError("siemens.opc_server_url or opcua_modules is required")
        if (
            self.source == "simulator"
            and not (self.simulator and self.simulator.tags)
            and not self.tags
        ):
            raise ValueError("simulator.tags or tags is required for source=simulator")
        return self

    @property
    def name(self) -> str:
        return self.id or self.lagoon_id or self.source


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    lagoon_id: str = Field(min_length=1, max_length=64)
    product_type: Literal["crystal", "small"] = "crystal"


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url_env: str = "COLLECTOR_OFFLINE_DATABASE_URL"
    statement_timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    lock_timeout_ms: int = Field(default=2_000, ge=100, le=60_000)
    pool_size: int = Field(default=5, ge=1, le=30)


class CentralConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health_url: str
    sync_url: str
    token_env: str = "COLLECTOR_OFFLINE_TOKEN"
    timeout_sec: float = Field(default=10.0, gt=0, le=120)
    verify_tls: bool = True
    ca_bundle: str | None = None
    allow_private_http: bool = False
    health_attempts: int = Field(default=2, ge=1, le=10)
    health_backoff_base_sec: float = Field(default=0.5, ge=0, le=30)

    @field_validator("health_url", "sync_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("central URL cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_tls(self) -> CentralConfig:
        if self.verify_tls is False:
            raise ValueError("verify_tls=false is not allowed")
        for value in (self.health_url, self.sync_url):
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            is_loopback = hostname in {"localhost", "127.0.0.1", "::1"}
            is_private_ip = False
            with suppress(ValueError):
                is_private_ip = ip_address(hostname).is_private
            allowed_private_http = self.allow_private_http and is_private_ip
            if parsed.scheme == "https":
                continue
            if parsed.scheme == "http" and (is_loopback or allowed_private_http):
                continue
            raise ValueError(
                "central URLs must use HTTPS; HTTP requires loopback or "
                "allow_private_http=true with a private IP"
            )
        return self


class SupervisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health_interval_sec: float = Field(default=60, ge=1)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_threshold: int = Field(default=2, ge=1)
    syncing_failure_threshold: int = Field(default=1, ge=1)
    runtime_tick_sec: float = Field(default=1, ge=0.1, le=10)


class CaptureConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_sec: int = Field(default=60, ge=60)

    @field_validator("interval_sec")
    @classmethod
    def require_minute_alignment(cls, value: int) -> int:
        if value % 60:
            raise ValueError("capture interval must be a multiple of 60 seconds")
        return value


class SyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=250, ge=1, le=5000)
    lease_sec: int = Field(default=120, ge=10)
    max_attempts: int = Field(default=10, ge=1)
    backoff_base_sec: float = Field(default=2, ge=0.1)
    backoff_max_sec: float = Field(default=300, ge=1)
    idle_interval_sec: float = Field(default=2, ge=0.1, le=60)


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    days: int = Field(default=90, ge=1)
    batch_size: int = Field(default=1000, ge=1, le=10000)
    interval_sec: int = Field(default=3600, ge=60)


class StatusServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=9187, ge=1024, le=65535)

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("status server must bind to loopback")
        return value


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_output: bool = Field(default=True, alias="json")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    node: NodeConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    central: CentralConfig
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    status_server: StatusServerConfig = Field(default_factory=StatusServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    plcs: list[PlcConfig]

    @model_validator(mode="after")
    def validate_lagoons(self) -> AppConfig:
        if not self.plcs:
            raise ValueError("at least one PLC is required")
        mismatches = {
            plc.lagoon_id
            for plc in self.plcs
            if plc.lagoon_id and plc.lagoon_id != self.node.lagoon_id
        }
        if mismatches:
            raise ValueError(f"PLC lagoon_id must match node lagoon_id: {sorted(mismatches)}")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for plc in self.plcs:
            configured_ids = set(plc.tags)
            for module in plc.opcua_modules:
                configured_ids.update(module.tags)
            if plc.simulator:
                configured_ids.update(plc.simulator.tags)
            if any(not tag_id.strip() or len(tag_id) > 64 for tag_id in configured_ids):
                raise ValueError("tag_id must contain 1..64 characters (central contract)")
            duplicates.update(seen.intersection(configured_ids))
            seen.update(configured_ids)
        if duplicates:
            raise ValueError(f"tag ids must be unique across PLC definitions: {sorted(duplicates)}")
        return self


class RuntimeSecrets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str
    central_token: str = Field(min_length=24)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"cannot read configuration {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"configuration {path} must contain a mapping")
    return parsed


def _expand_includes(raw: dict[str, Any], config_path: Path) -> dict[str, Any]:
    plcs = raw.get("plcs")
    if not isinstance(plcs, list):
        return raw
    expanded: list[Any] = []
    for index, entry in enumerate(plcs):
        if not isinstance(entry, dict):
            raise ValueError(f"plcs[{index}] must be a mapping")
        include = entry.get("include")
        if not include:
            expanded.append(entry)
            continue
        include_path = Path(str(include))
        if not include_path.is_absolute():
            include_path = (config_path.parent / include_path).resolve()
        included = _read_yaml(include_path)
        overrides = {key: value for key, value in entry.items() if key != "include"}
        expanded.append({**included, **overrides})
    return {**raw, "plcs": expanded}


def load_config(
    path: str | Path, environ: Mapping[str, str] | None = None
) -> tuple[AppConfig, RuntimeSecrets]:
    config_path = Path(path).resolve()
    raw = _expand_includes(_read_yaml(config_path), config_path)
    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        raise ValueError(f"invalid collector configuration: {errors}") from exc

    env = os.environ if environ is None else environ
    database_url = (secret_from_env(config.database.url_env, env) or "").strip()
    central_token = (secret_from_env(config.central.token_env, env) or "").strip()
    missing = []
    if not database_url:
        missing.append(config.database.url_env)
    if not central_token:
        missing.append(config.central.token_env)
    if missing:
        raise ValueError(f"required environment variables are missing: {', '.join(missing)}")

    try:
        secrets = RuntimeSecrets(database_url=database_url, central_token=central_token)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False)
        raise ValueError(f"invalid runtime secrets: {errors}") from exc
    return config, secrets


def secret_from_env(
    variable_name: str | None, environ: Mapping[str, str] | None = None
) -> str | None:
    if not variable_name:
        return None
    env = os.environ if environ is None else environ
    value = env.get(variable_name)
    file_path = env.get(f"{variable_name}_FILE")
    if value and file_path:
        raise ValueError(f"use only {variable_name} or {variable_name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            raise ValueError(f"cannot read secret file for {variable_name}") from exc
    return value if value else None
