"""BuildStudio's local H3 backend: submit promptly, collect in a later turn."""

from __future__ import annotations

import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.secret_scope import UnscopedSecretError, get_secret
from agent.video_gen_provider import (
    DEFAULT_ASPECT_RATIO, DEFAULT_RESOLUTION, OpenAICompatibleVideoGenProvider,
    _videos_cache_dir, error_response, success_response,
)
from .jobs import current_scope, owned_receipt, save_receipt, validate_job_id


class _H3ResponseError(ValueError):
    """A message safe to expose without transport URLs, headers or body text."""


class _H3SubmissionUnconfirmed(_H3ResponseError):
    """A POST may have been accepted; automatic resubmission could duplicate it."""


class BuildStudioH3VideoGenProvider(OpenAICompatibleVideoGenProvider):
    """One bounded HTTP request per submission/status check; no render polling."""

    name = "buildstudio_h3"
    _env_key = "VIDEO_COORDINATOR_API_KEY"
    _default_base_url = "http://127.0.0.1:8890/v1"
    _max_video_bytes = 200 * 1024 * 1024
    _content_deadline_s = 90.0

    @property
    def display_name(self) -> str:
        return "BuildStudio MiniMax H3"

    def _api_key(self) -> str:
        try:
            return (get_secret(self._env_key) or "").strip()
        except UnscopedSecretError:
            return ""

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": "minimax-h3", "display": "MiniMax H3 (BuildStudio Local)",
            "speed": "background queue on two RTX 3060 workers",
            "strengths": "short text-to-video clips with native dialogue, music, and effects",
            "modalities": ["text"],
        }]

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text"], "aspect_ratios": ["16:9", "9:16", "1:1"],
            "resolutions": ["864x480", "480x864", "640x640", "2560x1440", "1440x2560"],
            "default_resolution": "864x480", "max_duration": 5, "min_duration": 2,
            "supports_audio": False, "audio_always_on": True,
            "supports_negative_prompt": False, "supports_seed": True,
            "supports_upscale": True, "max_reference_images": 0,
            "upscale_description": (
                "Resize delivery to 2560x1440 (landscape) or 1440x2560 (portrait). "
                "This is Lanczos resizing, not native 2K generation; square upscaling is unsupported."
            ),
            "async_jobs": True, "quality_modes": ["turbo", "standard"], "default_quality": "turbo",
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {"name": self.display_name, "badge": "local", "tag": "Background video jobs; short clips with native audio", "env_vars": []}

    @staticmethod
    def _size(resolution: str, aspect_ratio: str, upscale: bool = False) -> str:
        if aspect_ratio not in {"16:9", "9:16", "1:1"}:
            raise ValueError("H3 supports 16:9, 9:16 and 1:1 aspect ratios")
        sizes = {"16:9": "864x480", "9:16": "480x864", "1:1": "640x640"}
        delivery = {"16:9": "2560x1440", "9:16": "1440x2560"}
        if resolution in {DEFAULT_RESOLUTION, "480p"}:
            size = sizes[aspect_ratio]
        elif resolution in set(sizes.values()) | set(delivery.values()) | {"640x384", "384x640"}:
            size = resolution
        else:
            raise ValueError("Unsupported H3 resolution; use an advertised pixel size")
        width, height = map(int, size.split("x"))
        shape = "1:1" if width == height else ("16:9" if width > height else "9:16")
        # A default landscape size from the generic schema follows aspect_ratio.
        if size == "864x480" and aspect_ratio != "16:9":
            size, shape = sizes[aspect_ratio], aspect_ratio
        if shape != aspect_ratio:
            raise ValueError("Resolution orientation does not match aspect_ratio")
        if upscale:
            if aspect_ratio not in delivery:
                raise ValueError("Square upscaling is unsupported; use native 640x640")
            return delivery[aspect_ratio]
        return size

    def _error(self, message: str, kind: str = "invalid_request") -> Dict[str, Any]:
        return error_response(error=message, error_type=kind, provider=self.name, model="minimax-h3")

    @contextmanager
    def _request(self, method: str, path: str, owner: str, *, payload: Optional[dict] = None, stream: bool = False):
        import requests

        # Never follow a coordinator redirect with its bearer key or user id.
        # Disable environment proxies/netrc for the private coordinator route.
        with requests.Session() as client:
            client.trust_env = False
            try:
                response = client.request(
                    method, self._base_url().rstrip("/") + path,
                    headers={"Authorization": "Bearer " + self._api_key(), "X-OpenWebUI-User-Id": owner},
                    json=payload, stream=stream, allow_redirects=False, timeout=(3.0, 20.0),
                )
            except Exception:
                if method == "POST":
                    raise _H3SubmissionUnconfirmed from None
                raise _H3ResponseError("H3 coordinator is temporarily unavailable") from None
            with response:
                if not 200 <= response.status_code < 300:
                    if method == "POST" and not (400 <= response.status_code < 500 and response.status_code != 408):
                        raise _H3SubmissionUnconfirmed
                    if method == "POST":
                        raise _H3ResponseError("H3 coordinator rejected the request; check settings or input before a new submission")
                    raise _H3ResponseError("H3 coordinator request failed; try checking the job later")
                yield response

    def _job_json(self, method: str, path: str, owner: str, payload: Optional[dict] = None) -> dict:
        with self._request(method, path, owner, payload=payload) as response:
            try:
                job = response.json()
            except ValueError:
                if method == "POST":
                    raise _H3SubmissionUnconfirmed from None
                raise _H3ResponseError("H3 coordinator returned invalid JSON") from None
        if not isinstance(job, dict) or job.get("status") not in {"queued", "in_progress", "completed", "failed", "cancelled"}:
            if method == "POST":
                raise _H3SubmissionUnconfirmed
            raise ValueError("H3 coordinator returned an invalid job response")
        try:
            validate_job_id(job.get("id"))
        except ValueError:
            if method == "POST":
                raise _H3SubmissionUnconfirmed from None
            raise
        return job

    def _status_result(self, job: dict, options: dict) -> Dict[str, Any]:
        status = job["status"]
        result = success_response(
            video=None, model="minimax-h3", prompt="", provider=self.name,
            duration=options["duration"], aspect_ratio=options["aspect_ratio"],
            extra={"job_id": job["id"], "status": status, "quality": options["quality"],
                   "resolution": options["size"], "audio": True,
                   "check_args": {"job_id": job["id"]}, "automatic_notification": False},
        )
        progress = job.get("progress")
        if isinstance(progress, (float, int)) and not isinstance(progress, bool) and math.isfinite(progress):
            result["progress"] = max(0.0, min(1.0, float(progress)))
        if status in {"failed", "cancelled"}:
            result.update(success=False, error="Video generation " + status, error_type="generation_" + status)
        else:
            result["message"] = (
                "Rendering has completed. Query this job_id to collect the video."
                if status == "completed" else
                "Video accepted and still rendering/queued. End this turn; query this job_id on a later request."
            )
        return result

    def generate(
        self, prompt: str, *, model: Optional[str] = None,
        image_url: Optional[str] = None, reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None, aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION, negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None, seed: Optional[int] = None,
        quality: str = "turbo", upscale: bool = False, **kwargs: Any,
    ) -> Dict[str, Any]:
        if image_url or reference_image_urls:
            return self._error("H3 currently supports text-to-video only", "unsupported_modality")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            return self._error("A video prompt of 1 to 12000 characters is required")
        if model not in {None, "minimax-h3"}:
            return self._error("Unknown H3 model")
        if audio is False or negative_prompt:
            return self._error("H3 audio is always on and negative_prompt is unsupported")
        if quality not in ("turbo", "standard"):
            return self._error("H3 quality must be turbo or standard")
        seconds = 5 if duration is None else duration
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 2 <= seconds <= 5:
            return self._error("H3 duration must be between 2 and 5 seconds")
        if not self._api_key():
            return self._error("H3 coordinator credentials are unavailable", "missing_credentials")
        try:
            scope, owner = current_scope()
            size = self._size(resolution, aspect_ratio, bool(upscale))
            options = {"duration": seconds, "aspect_ratio": aspect_ratio, "size": size, "quality": quality}
            payload = {"model": "minimax-h3", "prompt": prompt.strip(), "seconds": str(seconds), "size": size, "quality": quality}
            if seed is not None:
                payload["seed"] = seed
            job = self._job_json("POST", "/videos", owner, payload)
            save_receipt(job["id"], scope, options)
            return self._status_result(job, options)
        except _H3SubmissionUnconfirmed:
            return self._error("H3 submission could not be confirmed; do not automatically resubmit", "submission_unconfirmed")
        except ValueError as exc:
            return self._error(str(exc))
        except Exception:
            # HTTP/library exceptions may include URLs, headers or response
            # bodies. Do not send these into model context or ordinary logs.
            return self._error("H3 submission could not be confirmed; do not automatically resubmit", "submission_unconfirmed")

    def _download(self, job_id: str, owner: str) -> Path:
        deadline = time.monotonic() + self._content_deadline_s
        path = None
        try:
            with self._request("GET", "/videos/" + job_id + "/content", owner, stream=True) as response:
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "video/mp4":
                    raise ValueError("H3 result is not an MP4 video")
                descriptor, filename = tempfile.mkstemp(prefix="buildstudio_h3_", suffix=".mp4", dir=_videos_cache_dir())
                path = Path(filename)
                total = 0
                with os.fdopen(descriptor, "wb") as stream:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        total += len(chunk)
                        if total > self._max_video_bytes or time.monotonic() > deadline:
                            raise ValueError("H3 video delivery exceeded its size/time limit; query the job later")
                        stream.write(chunk)
                if not total:
                    raise ValueError("H3 returned an empty video")
                return path
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def get_job(self, job_id: str) -> Dict[str, Any]:
        if not self._api_key():
            return self._error("H3 coordinator credentials are unavailable", "missing_credentials")
        try:
            scope, owner = current_scope()
            options = owned_receipt(job_id, scope)
            job = self._job_json("GET", "/videos/" + validate_job_id(job_id), owner)
            if job["id"] != job_id:
                raise ValueError("H3 returned a different video job")
            result = self._status_result(job, options)
            if job["status"] == "completed":
                path = self._download(job_id, owner)
                result.update(video=str(path), upscaled=options["size"] in {"2560x1440", "1440x2560"},
                              message="Video ready. Deliver the local video through the platform file-delivery convention.")
            return result
        except _H3ResponseError as exc:
            return self._error(str(exc), "job_unavailable")
        except ValueError as exc:
            return self._error(str(exc))
        except Exception:
            return self._error("H3 job status or video delivery is temporarily unavailable; retry this job_id later", "job_unavailable")


def register(ctx) -> None:
    ctx.register_video_gen_provider(BuildStudioH3VideoGenProvider())
