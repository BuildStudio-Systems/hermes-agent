"""BuildStudio's local distributed MiniMax H3 video backend."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.video_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    OpenAICompatibleVideoGenProvider,
    error_response,
)


class BuildStudioH3VideoGenProvider(OpenAICompatibleVideoGenProvider):
    """Generate native-audio clips through the AI-server H3 coordinator."""

    name = "buildstudio_h3"
    _env_key = "VIDEO_COORDINATOR_API_KEY"
    _default_base_url = "http://127.0.0.1:8890/v1"
    _poll_interval_s = 4.0
    _poll_deadline_s = 3600.0

    @property
    def display_name(self) -> str:
        return "BuildStudio MiniMax H3"

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "minimax-h3",
                "display": "MiniMax H3 (BuildStudio Local)",
                "speed": "queued on two RTX 3060 workers",
                "strengths": "short text-to-video clips with native dialogue, music, and effects",
                "modalities": ["text"],
            }
        ]

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "resolutions": ["864x480", "480x864", "640x640"],
            "max_duration": 5,
            "min_duration": 2,
            "supports_audio": True,
            "supports_negative_prompt": False,
            "supports_seed": True,
            "supports_upscale": False,
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "local",
            "tag": "Two BuildStudio RTX 3060 workers; 0.4 MP, short clips, native audio",
            "env_vars": [],
        }

    @staticmethod
    def _size(resolution: str, aspect_ratio: str) -> str:
        allowed = {"864x480", "480x864", "640x640", "640x384", "384x640"}
        if resolution in allowed:
            return resolution
        return {
            "9:16": "480x864",
            "1:1": "640x640",
        }.get(aspect_ratio, "864x480")

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if image_url or reference_image_urls:
            return error_response(
                error="BuildStudio MiniMax H3 currently supports text-to-video only",
                error_type="unsupported_modality",
                provider=self.name,
                model=model or "minimax-h3",
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
        seconds = duration or 5
        if not 2 <= seconds <= 5:
            return error_response(
                error="BuildStudio MiniMax H3 duration must be between 2 and 5 seconds",
                error_type="invalid_request",
                provider=self.name,
                model=model or "minimax-h3",
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
        return super().generate(
            prompt,
            model=model or "minimax-h3",
            duration=seconds,
            aspect_ratio=aspect_ratio,
            resolution=self._size(resolution, aspect_ratio),
            audio=audio,
            seed=seed,
            **kwargs,
        )


def register(ctx) -> None:
    ctx.register_video_gen_provider(BuildStudioH3VideoGenProvider())
