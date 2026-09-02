"""Loopback-only, stdlib OpenAPI bridge for BuildStudio There map tools."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


MAPS_SCRIPT = (
    Path(__file__).resolve().parent
    / "skills"
    / "productivity"
    / "maps"
    / "scripts"
    / "maps_client.py"
)
VALID_MODES = {"driving", "walking", "cycling"}

OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "BuildStudio Maps",
        "version": "1.0.0",
        "description": (
            "Server-side Google Maps place search and routing for BuildStudio "
            "There, with an OpenStreetMap fallback."
        ),
    },
    "paths": {
        "/v1/places/search": {
            "post": {
                "operationId": "search_places",
                "summary": "Search for a named place, address, landmark, or business",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["query"],
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Place, address, landmark, or business text.",
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "Matching places"}},
            }
        },
        "/v1/places/nearby": {
            "post": {
                "operationId": "search_nearby_places",
                "summary": "Find real nearby businesses or points of interest",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["near"],
                                "properties": {
                                    "near": {
                                        "type": "string",
                                        "description": "Center place, for example 西船橋駅.",
                                    },
                                    "categories": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "default": ["restaurant"],
                                        "description": (
                                            "Categories such as restaurant, cafe, hotel, "
                                            "pharmacy, hospital, or supermarket."
                                        ),
                                    },
                                    "radius_m": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 50000,
                                        "default": 1000,
                                    },
                                    "limit": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 20,
                                        "default": 5,
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "Nearby places"}},
            }
        },
        "/v1/routes/directions": {
            "post": {
                "operationId": "get_directions",
                "summary": "Get directions, distance, and duration between two places",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["origin", "destination"],
                                "properties": {
                                    "origin": {"type": "string"},
                                    "destination": {"type": "string"},
                                    "mode": {
                                        "type": "string",
                                        "enum": ["driving", "walking", "cycling"],
                                        "default": "driving",
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "Route details"}},
            }
        },
    },
}


class MapsRequestError(RuntimeError):
    """A safe error that may be returned to the loopback API caller."""


def _required_text(request: dict, key: str) -> str:
    value = str(request.get(key) or "").strip()
    if not value:
        raise MapsRequestError(f"{key} is required")
    return value


def _bounded_int(request: dict, key: str, default: int, minimum: int,
                 maximum: int) -> int:
    try:
        value = int(request.get(key, default))
    except (TypeError, ValueError) as exc:
        raise MapsRequestError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise MapsRequestError(
            f"{key} must be between {minimum} and {maximum}"
        )
    return value


def _run_maps(arguments: list[str]) -> dict:
    if not MAPS_SCRIPT.is_file():
        raise MapsRequestError("Maps tool is not installed")
    process = subprocess.run(
        [sys.executable, str(MAPS_SCRIPT), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    output = process.stdout.strip()
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError as exc:
        raise MapsRequestError("Maps provider returned an invalid response") from exc
    if process.returncode != 0 or payload.get("status") == "error":
        detail = str(payload.get("error") or "Maps provider request failed")
        raise MapsRequestError(detail[:500])
    return payload


def search_places(request: dict) -> dict:
    return _run_maps(["search", _required_text(request, "query")])


def search_nearby_places(request: dict) -> dict:
    near = _required_text(request, "near")
    categories = request.get("categories", ["restaurant"])
    if not isinstance(categories, list) or not categories:
        raise MapsRequestError("categories must be a non-empty list")
    clean_categories = [str(value).strip() for value in categories if str(value).strip()]
    if not clean_categories:
        raise MapsRequestError("categories must contain at least one value")
    radius = _bounded_int(request, "radius_m", 1000, 1, 50000)
    limit = _bounded_int(request, "limit", 5, 1, 20)
    arguments = ["nearby", "--near", near]
    for category in clean_categories:
        arguments.extend(["--category", category])
    arguments.extend(["--radius", str(radius), "--limit", str(limit)])
    return _run_maps(arguments)


def get_directions(request: dict) -> dict:
    origin = _required_text(request, "origin")
    destination = _required_text(request, "destination")
    mode = str(request.get("mode") or "driving").strip().lower()
    if mode not in VALID_MODES:
        raise MapsRequestError(
            "mode must be driving, walking, or cycling"
        )
    return _run_maps([
        "directions", origin, "--to", destination, "--mode", mode
    ])


class MapsHandler(BaseHTTPRequestHandler):
    server_version = "BuildStudioMaps/1.0"

    def log_message(self, format_string, *args):
        # Never log request bodies or environment variables. The path/status
        # emitted by BaseHTTPRequestHandler are sufficient operational data.
        super().log_message(format_string, *args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {
                "status": "ok",
                "maps_script": MAPS_SCRIPT.is_file(),
            })
        elif path == "/openapi.json":
            self._send_json(200, OPENAPI_SPEC)
        else:
            self._send_json(404, {"detail": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        handlers = {
            "/v1/places/search": search_places,
            "/v1/places/nearby": search_nearby_places,
            "/v1/routes/directions": get_directions,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send_json(404, {"detail": "Not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 65536:
                raise MapsRequestError("JSON body is required and must be under 64 KiB")
            request = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(request, dict):
                raise MapsRequestError("JSON body must be an object")
            self._send_json(200, handler(request))
        except (MapsRequestError, json.JSONDecodeError) as exc:
            self._send_json(400, {"detail": str(exc)[:500]})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"detail": "Maps provider request timed out"})
        except Exception:
            self._send_json(500, {"detail": "Internal maps service error"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8891)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MapsHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
