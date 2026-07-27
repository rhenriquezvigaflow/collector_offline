from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from collector_offline.config import PlcConfig
from collector_offline.infrastructure import drivers
from collector_offline.infrastructure.drivers import (
    RockwellReader,
    SiemensReader,
    build_bound_reader,
)


class FakeTag:
    def __init__(self, tag: str, value: Any, error: str | None = None) -> None:
        self.tag = tag
        self.value = value
        self.error = error


class FakeLogixDriver:
    instances: list[FakeLogixDriver] = []

    def __init__(self, ip: str, *, slot: int, timeout: float) -> None:
        self.ip = ip
        self.slot = slot
        self.timeout = timeout
        self.opened = False
        self.closed = False
        self.read_calls: list[tuple[str, ...]] = []
        self.__class__.instances.append(self)

    def open(self) -> None:
        self.opened = True

    def read(self, *tags: str) -> list[FakeTag]:
        self.read_calls.append(tags)
        return [
            FakeTag(tags[0], 12.5),
            FakeTag(tags[1], True),
            FakeTag(tags[2], None, "path segment error"),
        ]

    def close(self) -> None:
        self.closed = True


def install_fake_pycomm3(monkeypatch: Any) -> None:
    FakeLogixDriver.instances = []
    module = ModuleType("pycomm3")
    module.LogixDriver = FakeLogixDriver  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pycomm3", module)


def rockwell_config(**overrides: Any) -> PlcConfig:
    raw: dict[str, Any] = {
        "id": "rockwell-test",
        "source": "rockwell",
        "rockwell": {"ip": "192.168.26.10", "slot": 2, "timeout_sec": 4},
        "force_reconnect_every_sec": 3600,
        "max_consecutive_fails": 10,
        "tags": {
            "PRESSURE": "PT203_R",
            "RUNNING": "P300_ST",
            "BROKEN": "INVALID_TAG",
        },
    }
    raw.update(overrides)
    return PlcConfig.model_validate(raw)


def test_yaml_source_selects_matching_driver_without_connecting() -> None:
    rockwell = build_bound_reader(rockwell_config())
    siemens = build_bound_reader(
        PlcConfig.model_validate(
            {
                "id": "siemens-test",
                "source": "siemens",
                "siemens": {"opc_server_url": "opc.tcp://192.168.100.10:4840"},
                "tags": {"PRESSURE": "ns=4;i=3"},
            }
        )
    )

    assert isinstance(rockwell.reader, RockwellReader)
    assert isinstance(siemens.reader, SiemensReader)


def test_rockwell_reader_matches_central_batch_mapping(monkeypatch: Any) -> None:
    install_fake_pycomm3(monkeypatch)
    binding = build_bound_reader(rockwell_config())

    values = binding.reader.read_once()

    assert values == {"PRESSURE": 12.5, "RUNNING": True, "BROKEN": None}
    assert binding.tag_specs["PRESSURE"].address == "PT203_R"
    assert len(FakeLogixDriver.instances) == 1
    driver = FakeLogixDriver.instances[0]
    assert (driver.ip, driver.slot, driver.timeout) == ("192.168.26.10", 2, 4.0)
    assert driver.opened is True
    assert driver.read_calls == [("PT203_R", "P300_ST", "INVALID_TAG")]

    binding.reader.read_once()
    assert len(FakeLogixDriver.instances) == 1
    binding.reader.close()
    assert driver.closed is True


def test_rockwell_reader_rotates_persistent_connection(monkeypatch: Any) -> None:
    install_fake_pycomm3(monkeypatch)
    clock = iter([100.0, 131.0, 131.0])
    monkeypatch.setattr(drivers.time, "monotonic", lambda: next(clock))
    binding = build_bound_reader(rockwell_config(force_reconnect_every_sec=30))

    binding.reader.read_once()
    first = FakeLogixDriver.instances[0]
    binding.reader.read_once()

    assert len(FakeLogixDriver.instances) == 2
    assert first.closed is True
