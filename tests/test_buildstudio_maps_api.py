"""Tests for the loopback BuildStudio Maps OpenAPI bridge."""

import json
from types import SimpleNamespace

import buildstudio_maps_api as maps_api


def test_openapi_exposes_customer_map_tools():
    operations = {
        operation["operationId"]
        for path in maps_api.OPENAPI_SPEC["paths"].values()
        for operation in path.values()
    }
    assert {
        "search_places",
        "search_nearby_places",
        "get_directions",
    } <= operations


def test_nearby_builds_cli_request(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "data_source": "Google Maps Platform",
                "count": 1,
                "results": [{"name": "Restaurant"}],
            }),
        )

    monkeypatch.setattr(maps_api.subprocess, "run", fake_run)

    payload = maps_api.search_nearby_places(
        {
            "near": "西船橋駅",
            "categories": ["restaurant", "cafe"],
            "radius_m": 1500,
            "limit": 5,
        }
    )

    assert payload["data_source"] == "Google Maps Platform"
    assert captured["command"][2:5] == ["nearby", "--near", "西船橋駅"]
    assert captured["command"].count("--category") == 2
