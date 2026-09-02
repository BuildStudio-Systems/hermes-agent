"""Tests for the BuildStudio Google Maps provider."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


SCRIPT = (
    Path(__file__).parents[2]
    / "skills"
    / "productivity"
    / "maps"
    / "scripts"
    / "maps_client.py"
)


def load_maps(monkeypatch):
    monkeypatch.setenv("MAPS_PROVIDER", "google")
    monkeypatch.setenv("MAPS_FALLBACK_PROVIDER", "osm")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza-test-buildstudio-maps-key")
    monkeypatch.setenv("GOOGLE_MAPS_REGION", "JP")
    monkeypatch.setenv("GOOGLE_MAPS_LANGUAGE", "zh-CN")
    spec = importlib.util.spec_from_file_location("maps_client_google_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def test_places_key_is_sent_in_header_not_url(monkeypatch):
    maps = load_maps(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({
            "places": [{
                "id": "place-1",
                "displayName": {"text": "西船橋駅"},
                "formattedAddress": "日本、千葉県船橋市西船",
                "location": {"latitude": 35.7074, "longitude": 139.9591},
                "googleMapsUri": "https://maps.google.com/example",
                "types": ["train_station"],
            }]
        })

    monkeypatch.setattr(maps.urllib.request, "urlopen", fake_urlopen)

    places = maps.google_places_text_search("西船橋駅", limit=1)

    request = captured["request"]
    headers = dict(request.header_items())
    assert "AIza-test-buildstudio-maps-key" not in request.full_url
    assert "AIza-test-buildstudio-maps-key" in headers.values()
    assert request.full_url == maps.GOOGLE_PLACES_TEXT_SEARCH
    assert places[0]["id"] == "place-1"
    assert json.loads(request.data)["regionCode"] == "JP"


def test_nearby_uses_google_and_returns_normalized_results(
    monkeypatch, capsys
):
    maps = load_maps(monkeypatch)
    monkeypatch.setattr(
        maps,
        "google_geocode_single",
        lambda query: (35.7074, 139.9591, "西船橋駅"),
    )
    monkeypatch.setattr(
        maps,
        "google_places_nearby",
        lambda lat, lon, categories, radius, limit: [{
            "id": "restaurant-1",
            "displayName": {"text": "Test Restaurant"},
            "formattedAddress": "船橋市西船4丁目",
            "location": {"latitude": 35.7080, "longitude": 139.9598},
            "googleMapsUri": "https://maps.google.com/restaurant-1",
            "types": ["restaurant"],
        }],
    )
    args = SimpleNamespace(
        near=["西船橋駅"],
        lat=None,
        lon=None,
        category=None,
        category_list=["restaurant"],
        radius=500,
        limit=5,
    )

    maps.cmd_nearby(args)

    output = json.loads(capsys.readouterr().out)
    assert output["data_source"] == "Google Maps Platform"
    assert output["count"] == 1
    assert output["results"][0]["name"] == "Test Restaurant"
    assert output["results"][0]["category"] == "restaurant"
    assert output["results"][0]["distance_m"] > 0


def test_routes_request_uses_server_key_header(monkeypatch):
    maps = load_maps(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return FakeResponse({
            "routes": [{"distanceMeters": 1200, "duration": "420s"}]
        })

    monkeypatch.setattr(maps.urllib.request, "urlopen", fake_urlopen)

    route = maps.google_compute_route(
        35.7074, 139.9591, 35.7100, 139.9700, "walking"
    )

    request = captured["request"]
    headers = dict(request.header_items())
    assert request.full_url == maps.GOOGLE_ROUTES
    assert "AIza-test-buildstudio-maps-key" not in request.full_url
    assert "AIza-test-buildstudio-maps-key" in headers.values()
    assert route["distanceMeters"] == 1200
