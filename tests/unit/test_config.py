from pathlib import Path

import pytest
import yaml

from collector_offline.config import CentralConfig, PlcConfig, load_config


def test_active_config_reuses_local_small_sim_yaml() -> None:
    path = Path(__file__).parents[2] / "config" / "collector-offline.yml"
    config, _ = load_config(
        path,
        {
            "COLLECTOR_OFFLINE_DATABASE_URL": "postgresql+psycopg2://u:p@localhost/db",
            "COLLECTOR_OFFLINE_TOKEN": "a" * 32,
        },
    )

    assert config.node.lagoon_id == "small_sim"
    assert config.node.product_type == "small"
    plc = config.plcs[0]
    assert plc.lagoon_id == "small_sim"
    assert plc.source in {"siemens", "rockwell", "simulator"}
    if plc.source == "rockwell":
        assert plc.rockwell is not None
        assert plc.rockwell.ip
        assert plc.tags
    elif plc.source == "siemens":
        assert plc.opcua_modules or (plc.siemens and plc.siemens.opc_server_url)
    else:
        assert plc.tags or (plc.simulator and plc.simulator.tags)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["plcs"][0]["include"] == "small_sim.yml"
    assert (path.parent / "small_sim.yml").is_file()


def test_central_non_loopback_http_is_rejected() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        CentralConfig(
            health_url="http://central.example/health/ready",
            sync_url="https://central.example/ingest/offline/v1/backfill",
        )


def test_central_private_http_requires_explicit_opt_in() -> None:
    config = CentralConfig(
        health_url="http://192.168.2.100:8090/health/ready",
        sync_url="http://192.168.2.100:8090/ingest/offline/v1/backfill",
        allow_private_http=True,
    )

    assert config.allow_private_http is True


def test_central_public_http_is_rejected_even_with_private_opt_in() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        CentralConfig(
            health_url="http://8.8.8.8/health/ready",
            sync_url="http://8.8.8.8/ingest/offline/v1/backfill",
            allow_private_http=True,
        )


def test_tls_verification_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="verify_tls"):
        CentralConfig(
            health_url="https://central.example/health/ready",
            sync_url="https://central.example/ingest/offline/v1/backfill",
            verify_tls=False,
        )


def test_rockwell_example_uses_central_yaml_contract() -> None:
    path = Path(__file__).parents[2] / "config" / "rockwell-example.yml"
    plc = PlcConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    assert plc.source == "rockwell"
    assert plc.rockwell is not None
    assert plc.rockwell.ip == "192.168.1.10"
    assert plc.rockwell.slot == 0
    assert plc.tags["PT117_R"].address == "PT117_R"
    assert plc.tags["P006_ST"].event is True
    assert plc.tags["P006_ST"].label == "Bomba de filtración"


def test_plc_source_is_case_insensitive_like_central_collector() -> None:
    plc = PlcConfig.model_validate(
        {
            "source": " ROCKWELL ",
            "rockwell": {"ip": "192.168.1.10"},
            "tags": {"PT117_R": "PT117_R"},
        }
    )

    assert plc.source == "rockwell"


def test_rockwell_requires_its_own_tags_even_if_opcua_modules_exist() -> None:
    with pytest.raises(ValueError, match="tags is required for source=rockwell"):
        PlcConfig.model_validate(
            {
                "source": "rockwell",
                "rockwell": {"ip": "192.168.1.10"},
                "opcua_modules": [
                    {
                        "id": "unrelated-siemens-module",
                        "opc_server_url": "opc.tcp://192.168.1.20:4840",
                        "tags": {"TAGS_01_BOOL": "ns=4;i=23"},
                    }
                ],
            }
        )
