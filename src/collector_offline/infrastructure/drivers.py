from __future__ import annotations

import logging
import random
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from collector_offline.config import PlcConfig, TagSpec, secret_from_env

logger = logging.getLogger("collector_offline.driver")


class TagReader(Protocol):
    def read_once(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class BoundReader:
    name: str
    reader: TagReader
    tag_specs: dict[str, TagSpec]


class SimulatorReader:
    def __init__(self, specs: dict[str, Any], seed: int | None = None) -> None:
        self.specs = specs
        self._random = random.Random(seed)
        self._state: dict[str, Any] = {}

    def read_once(self) -> dict[str, Any]:
        return {tag_id: self._next_value(tag_id, spec) for tag_id, spec in self.specs.items()}

    def _next_value(self, tag_id: str, spec: Any) -> Any:
        if not isinstance(spec, dict):
            return spec
        value_type = str(spec.get("type") or "float").lower()
        if value_type == "bool":
            current = self._state.get(tag_id, bool(spec.get("initial", False)))
            if self._random.random() < float(spec.get("change_probability", 0.05)):
                current = not current
            self._state[tag_id] = current
            return current
        if value_type in {"state", "choice"}:
            values = spec.get("values") or [0, 1]
            current = self._state.get(tag_id, values[0])
            if self._random.random() < float(spec.get("change_probability", 0.12)):
                current = self._random.choice(values)
            self._state[tag_id] = current
            return current
        low = float(spec.get("min", 0))
        high = float(spec.get("max", 100))
        current = float(self._state.get(tag_id, self._random.uniform(low, high)))
        step = float(spec.get("step", max(high - low, 1) / 12))
        current = min(max(current + self._random.uniform(-step, step), low), high)
        self._state[tag_id] = current
        if value_type == "int":
            return int(round(current))
        return round(current, int(spec.get("decimals", 2)))

    def close(self) -> None:
        return None


class RockwellReader:
    def __init__(self, config: PlcConfig) -> None:
        if not config.rockwell or not config.rockwell.ip:
            raise ValueError("rockwell connection is missing")
        self.ip = config.rockwell.ip
        self.slot = config.rockwell.slot
        self.timeout = config.rockwell.timeout_sec
        self.force_reconnect_every_sec = config.force_reconnect_every_sec
        self.max_consecutive_fails = config.max_consecutive_fails
        self.tag_map = {tag_id: spec.address for tag_id, spec in config.tags.items()}
        self._plc_tags = list(self.tag_map.values())
        self._logical_by_address = {address: tag_id for tag_id, address in self.tag_map.items()}
        self._driver: Any = None
        self._connected_at = 0.0
        self._failures = 0

    def _connect(self) -> None:
        self.close()
        try:
            from pycomm3 import LogixDriver
        except ImportError as exc:
            raise RuntimeError("install the 'plc' extra to use Rockwell") from exc
        driver = LogixDriver(self.ip, slot=self.slot, timeout=self.timeout)
        driver.open()  # type: ignore[no-untyped-call]
        self._driver = driver
        self._connected_at = time.monotonic()
        self._failures = 0
        logger.info("Connected to Rockwell PLC", extra={"ip": self.ip})

    def read_once(self) -> dict[str, Any]:
        if (
            self._driver is None
            or time.monotonic() - self._connected_at >= self.force_reconnect_every_sec
        ):
            self._connect()
        try:
            results = self._driver.read(*self._plc_tags)
            if not isinstance(results, list):
                results = [results]
            values: dict[str, Any] = {}
            for result in results:
                if result is None:
                    continue
                tag_id = self._find_logical_tag(result.tag)
                values[tag_id] = None if result.error else self._normalize_value(result.value)
            self._failures = 0
            return values
        except Exception:
            self._failures += 1
            if self._failures >= self.max_consecutive_fails:
                self.close()
            raise

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return float(value)
        return value

    def _find_logical_tag(self, plc_tag: str) -> str:
        return self._logical_by_address.get(plc_tag, plc_tag)

    def close(self) -> None:
        if self._driver is not None:
            with suppress(Exception):
                self._driver.close()
        self._driver = None


class SiemensReader:
    def __init__(
        self,
        endpoint: str,
        tag_specs: dict[str, TagSpec],
        timeout_sec: float,
        username_env: str | None = None,
        password_env: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.tag_specs = tag_specs
        self.timeout_sec = timeout_sec
        self.username = secret_from_env(username_env)
        self.password = secret_from_env(password_env)
        self._client: Any = None
        self._nodes: list[Any] = []
        self._tag_ids = list(tag_specs)

    def _connect(self) -> None:
        self.close()
        try:
            from opcua import Client
        except ImportError as exc:
            raise RuntimeError("install the 'plc' extra to use Siemens OPC UA") from exc
        client = Client(self.endpoint, timeout=self.timeout_sec)
        if self.username:
            client.set_user(self.username)
        if self.password:
            client.set_password(self.password)
        client.connect()
        self._client = client
        self._nodes = [client.get_node(self.tag_specs[tag_id].address) for tag_id in self._tag_ids]

    def read_once(self) -> dict[str, Any]:
        if self._client is None:
            self._connect()
        try:
            values = self._client.get_values(self._nodes)
            return dict(zip(self._tag_ids, values, strict=True))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._client is not None:
            with suppress(Exception):
                self._client.disconnect()
        self._client = None
        self._nodes = []


class CompositeReader:
    def __init__(
        self, readers: list[TagReader], supplemental: dict[str, Any] | None = None
    ) -> None:
        self.readers = readers
        self.supplemental = supplemental or {}

    def read_once(self) -> dict[str, Any]:
        values = dict(self.supplemental)
        for reader in self.readers:
            try:
                values.update(reader.read_once())
            except Exception as exc:
                logger.warning("PLC module read failed", extra={"error": str(exc)})
        return values

    def close(self) -> None:
        for reader in self.readers:
            reader.close()


def build_bound_reader(config: PlcConfig) -> BoundReader:
    if config.source == "rockwell":
        return BoundReader(config.name, RockwellReader(config), dict(config.tags))

    if config.source == "siemens" and config.opcua_modules:
        readers: list[TagReader] = []
        combined_specs: dict[str, TagSpec] = dict(config.tags)
        for module in config.opcua_modules:
            endpoint = module.opc_server_url or (f"opc.tcp://{module.ip}:4840" if module.ip else "")
            if not endpoint:
                raise ValueError(f"OPC UA endpoint is missing for module {module.id}")
            readers.append(
                SiemensReader(
                    endpoint,
                    module.tags,
                    module.timeout_sec,
                    module.username_env,
                    module.password_env,
                )
            )
            combined_specs.update(module.tags)
        supplemental = config.simulator.tags if config.simulator else {}
        combined_specs.update(
            {
                tag_id: TagSpec(address=tag_id)
                for tag_id in supplemental
                if tag_id not in combined_specs
            }
        )
        return BoundReader(
            config.name,
            CompositeReader(readers, supplemental=supplemental),
            combined_specs,
        )

    if config.source == "siemens":
        assert config.siemens and config.siemens.opc_server_url
        return BoundReader(
            config.name,
            SiemensReader(
                config.siemens.opc_server_url,
                config.tags,
                config.siemens.timeout_sec,
                config.siemens.username_env,
                config.siemens.password_env,
            ),
            dict(config.tags),
        )

    simulator_specs = config.simulator.tags if config.simulator else {}
    if not simulator_specs:
        simulator_specs = {tag_id: 0 for tag_id in config.tags}
    tag_specs = dict(config.tags)
    tag_specs.update(
        {tag_id: TagSpec(address=tag_id) for tag_id in simulator_specs if tag_id not in tag_specs}
    )
    return BoundReader(
        config.name,
        SimulatorReader(simulator_specs, seed=config.simulator.seed if config.simulator else None),
        tag_specs,
    )
