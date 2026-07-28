from __future__ import annotations

import os
import re
from collections.abc import Mapping
from contextlib import suppress
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class TagSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str
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
    tags: dict[str, TagSpec] = Field(default_factory=dict)
    event_tags: dict[str, str] = Field(default_factory=dict)
    tag_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rockwell: ConnectionConfig | None = None
    siemens: ConnectionConfig | None = None
    simulator: SimulatorConfig | None = None
    opcua_modules: list[OpcuaModuleConfig] = Field(default_factory=list)
    force_reconnect_every_sec: int = Field(default=3600, ge=30)
    max_consecutive_fails: int = Field(default=10, ge=1)

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
    # Legacy-only fallback used to derive the transfer origin during upgrades.
    sync_url: str | None = None
    transfer_url: str | None = None
    token_env: str = "COLLECTOR_OFFLINE_TOKEN"
    timeout_sec: float = Field(default=10.0, gt=0, le=120)
    verify_tls: bool = True
    ca_bundle: str | None = None
    allow_private_http: bool = False
    health_attempts: int = Field(default=1, ge=1, le=10)
    health_backoff_base_sec: float = Field(default=0.5, ge=0, le=30)

    @field_validator("health_url", "sync_url", "transfer_url")
    @classmethod
    def normalize_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("central URL cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_tls(self) -> CentralConfig:
        if self.verify_tls is False:
            raise ValueError("verify_tls=false is not allowed")
        if not self.transfer_url and not self.sync_url:
            raise ValueError("central.transfer_url is required")
        for value in (self.health_url, self.transfer_url or self.sync_url):
            if value is None:
                continue
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

    @property
    def offline_transfer_url(self) -> str:
        if self.transfer_url:
            return self.transfer_url.rstrip("/")
        if not self.sync_url:
            raise ValueError("central.transfer_url is required")
        parsed = urlsplit(self.sync_url)
        legacy_suffix = "/ingest/offline/v1/backfill"
        legacy_path = parsed.path.rstrip("/")
        prefix = (
            legacy_path[: -len(legacy_suffix)]
            if legacy_path.endswith(legacy_suffix)
            else ""
        )
        return urlunsplit(
            (parsed.scheme, parsed.netloc, f"{prefix}/offline-transfer", "", "")
        )


class SupervisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health_interval_sec: float = Field(default=30, ge=1)
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
    """Accepted only so existing site YAML remains backward compatible."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=25, ge=1, le=5000)
    lease_sec: int = Field(default=120, ge=10)
    max_attempts: int = Field(default=10, ge=1)
    backoff_base_sec: float = Field(default=2, ge=0.1)
    backoff_max_sec: float = Field(default=300, ge=1)
    idle_interval_sec: float = Field(default=2, ge=0.1, le=60)


class ChunkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outbox_path: Path = Path("/var/lib/crystal-offline/outbox")
    max_rows: int = Field(default=20_000, ge=1, le=20_000)
    max_uncompressed_size_mb: float = Field(default=25, gt=0, le=25)
    max_age_minutes: int = Field(default=360, ge=1)
    claim_timeout_seconds: int = Field(default=900, ge=30)

    @property
    def max_uncompressed_size_bytes(self) -> int:
        return int(self.max_uncompressed_size_mb * 1024 * 1024)


class TransferConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_size_kb: int = Field(default=1024, ge=64, le=2048)
    max_parallel_uploads: Literal[1] = 1
    connect_timeout_seconds: float = Field(default=10, gt=0, le=120)
    request_timeout_seconds: float = Field(default=60, gt=0, le=600)
    retry_initial_seconds: float = Field(default=15, gt=0)
    retry_max_seconds: float = Field(default=300, gt=0)
    poll_interval_seconds: float = Field(default=5, ge=0.1, le=300)
    lease_seconds: int = Field(default=180, ge=30)

    @model_validator(mode="after")
    def validate_retry_window(self) -> TransferConfig:
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds must be >= retry_initial_seconds")
        return self

    @property
    def part_size_bytes(self) -> int:
        return self.part_size_kb * 1024


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    days: int = Field(default=90, ge=1)
    batch_size: int = Field(default=1000, ge=1, le=10000)
    interval_sec: int = Field(default=3600, ge=60)
    verified_files_days: int = Field(default=7, ge=1)
    verified_metadata_days: int = Field(default=90, ge=1)


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
    chunk: ChunkConfig = Field(default_factory=ChunkConfig)
    transfer: TransferConfig = Field(default_factory=TransferConfig)
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


_OFFLINE_SITE_KEYS = {
    "version",
    "source_node_id",
    "central",
    "database",
    "supervisor",
    "capture",
    "sync",
    "chunk",
    "transfer",
    "retention",
    "status_server",
    "logging",
}


def _default_source_node_id(lagoon_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", lagoon_id.strip().lower()).strip("-")
    if not slug:
        raise ValueError("lagoon_id cannot produce a valid source_node_id")
    return f"edge-{slug}-01"


def _is_site_document(raw: dict[str, Any]) -> bool:
    return (
        "lagoon_id" in raw
        and "source" in raw
        and "node" not in raw
        and "plcs" not in raw
    )


def _expand_site_document(raw: dict[str, Any]) -> dict[str, Any]:
    offline_raw = raw.get("offline")
    if not isinstance(offline_raw, dict):
        raise ValueError(
            "site YAML requires an 'offline' mapping with at least offline.central"
        )
    unknown = set(offline_raw).difference(_OFFLINE_SITE_KEYS)
    if unknown:
        raise ValueError(f"unsupported offline options: {sorted(unknown)}")

    central_raw = offline_raw.get("central")
    if not isinstance(central_raw, dict):
        raise ValueError("site YAML requires offline.central")
    central = dict(central_raw)
    base_url_raw = central.pop("base_url", None)
    if base_url_raw is not None:
        base_url = str(base_url_raw).strip().rstrip("/")
        if not base_url:
            raise ValueError("offline.central.base_url cannot be empty")
        central.setdefault("health_url", f"{base_url}/health/ready")
        central.setdefault("transfer_url", f"{base_url}/offline-transfer")
    if not central.get("health_url") or not (
        central.get("transfer_url") or central.get("sync_url")
    ):
        raise ValueError(
            "offline.central requires base_url or health_url plus transfer_url"
        )

    lagoon_id = str(raw.get("lagoon_id") or "").strip()
    source_node_id = str(
        offline_raw.get("source_node_id") or _default_source_node_id(lagoon_id)
    ).strip()
    plc = {key: value for key, value in raw.items() if key != "offline"}
    expanded: dict[str, Any] = {
        "version": offline_raw.get("version", 1),
        "node": {
            "source_node_id": source_node_id,
            "lagoon_id": lagoon_id,
            "product_type": raw.get("product_type", "crystal"),
        },
        "central": central,
        "plcs": [plc],
    }
    for section in (
        "database",
        "supervisor",
        "capture",
        "sync",
        "chunk",
        "transfer",
        "retention",
        "status_server",
        "logging",
    ):
        if section in offline_raw:
            expanded[section] = offline_raw[section]
    return expanded


def load_app_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = _read_yaml(config_path)
    try:
        normalized = _expand_site_document(raw) if _is_site_document(raw) else raw
        expanded = _expand_includes(normalized, config_path)
        return AppConfig.model_validate(expanded)
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"invalid collector configuration: {exc}") from exc


def load_config(
    path: str | Path, environ: Mapping[str, str] | None = None
) -> tuple[AppConfig, RuntimeSecrets]:
    config = load_app_config(path)

    env = os.environ if environ is None else environ
    database_url = env.get(config.database.url_env, "").strip()
    central_token = env.get(config.central.token_env, "").strip()
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
        raise ValueError(f"invalid runtime secrets: {exc}") from exc
    return config, secrets


def secret_from_env(
    variable_name: str | None, environ: Mapping[str, str] | None = None
) -> str | None:
    if not variable_name:
        return None
    env = os.environ if environ is None else environ
    value = env.get(variable_name)
    return value if value else None
