from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError


class ImageGenerationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


STYLE_PROMPTS = {
    "cinematic": "Cinematic natural lighting, rich but realistic color grading, composed like a documentary film still.",
    "documentary": "Photorealistic documentary still, natural lighting, candid composition, realistic details.",
    "educational": "Clear educational editorial illustration, accurate visual details, uncluttered composition.",
    "business": "Polished realistic editorial photography, professional natural lighting, restrained colors.",
}


def image_prompt(scene: dict[str, Any], visual_style: str) -> str:
    # Each visual prompt is derived only from its own narration/description.
    meaning = " ".join(str(scene.get("text") or scene.get("visual_description") or "").split())
    if not meaning:
        raise ImageGenerationError("empty_scene", "The scene has no text to describe an image.")
    style = STYLE_PROMPTS.get(str(visual_style).lower(), STYLE_PROMPTS["documentary"])
    return (
        f"Create one coherent image depicting this scene: {meaning}. {style} "
        "Show the described subject and action clearly. No text, letters, logos, borders, or watermarks."
    )


def _dimensions(ratio: str) -> tuple[int, int]:
    try:
        base = int(os.getenv("IMAGE_GENERATION_RESOLUTION", "512"))
    except ValueError as error:
        raise ImageGenerationError("image_configuration_invalid", "IMAGE_GENERATION_RESOLUTION must be a positive integer.") from error
    if base < 256 or base > 1536:
        raise ImageGenerationError("image_configuration_invalid", "IMAGE_GENERATION_RESOLUTION must be between 256 and 1536.")
    base = max(256, base - (base % 8))
    if ratio == "16:9":
        return base, max(8, round((base * 9 / 16) / 8) * 8)
    if ratio == "9:16":
        return max(8, round((base * 9 / 16) / 8) * 8), base
    return base, base


class Automatic1111ImageGenerator:
    """Client for the documented AUTOMATIC1111 Stable Diffusion WebUI API."""

    name = "AUTOMATIC1111 Stable Diffusion WebUI"

    def __init__(self) -> None:
        backend = os.getenv("IMAGE_GENERATION_BACKEND", "").strip().lower()
        if backend not in {"automatic1111", "a1111"}:
            raise ImageGenerationError(
                "image_generator_not_configured",
                "AI image generation is not configured. Install a local image model, set IMAGE_GENERATION_BACKEND=automatic1111, and start the WebUI with its API enabled.",
            )
        self.base_url = os.getenv("IMAGE_GENERATION_URL", "http://127.0.0.1:7860").strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ImageGenerationError("image_configuration_invalid", "IMAGE_GENERATION_URL must be an HTTP(S) URL.")
        self.timeout = float(os.getenv("IMAGE_GENERATION_TIMEOUT_SECONDS", "300"))
        self.api_key = os.getenv("IMAGE_GENERATION_API_KEY", "").strip()
        self.model = os.getenv("IMAGE_GENERATION_MODEL", "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def prepare(self) -> str:
        actual_width = actual_height = 0
        try:
            response = httpx.get(f"{self.base_url}/sdapi/v1/options", headers=self._headers(), timeout=10)
            response.raise_for_status()
            options = response.json()
        except httpx.TimeoutException as error:
            raise ImageGenerationError("image_backend_timeout", "The image-generation backend did not respond in time.") from error
        except (httpx.HTTPError, ValueError) as error:
            raise ImageGenerationError(
                "image_backend_unavailable",
                f"Could not reach the configured image-generation backend at {self.base_url}. Start it with API access enabled and verify IMAGE_GENERATION_URL.",
            ) from error
        if not isinstance(options, dict):
            raise ImageGenerationError("image_backend_invalid_response", "The image backend returned invalid model information.")
        reported = str(options.get("sd_model_checkpoint") or "").strip()
        self.model = self.model or reported
        if not self.model:
            raise ImageGenerationError("image_model_unknown", "The image backend did not report its loaded model. Set IMAGE_GENERATION_MODEL to its exact model name.")
        return self.model

    def generate(
        self,
        prompt: str,
        destination: Path,
        *,
        seed: int,
        ratio: str,
        avoid: list[str],
    ) -> dict[str, Any]:
        if not self.model:
            raise ImageGenerationError("image_model_unknown", "Image backend model information was not checked before generation.")
        width, height = _dimensions(ratio)
        negative = "text, watermark, logo, border, low quality, malformed details"
        if avoid:
            negative += ", " + ", ".join(avoid)
        payload: dict[str, Any] = {
            "prompt": prompt,
            "negative_prompt": negative,
            "width": width,
            "height": height,
            "steps": _generation_steps(),
            "cfg_scale": 7,
            "seed": seed,
            "batch_size": 1,
            "n_iter": 1,
            "send_images": True,
            "save_images": False,
        }
        configured_model = os.getenv("IMAGE_GENERATION_MODEL", "").strip()
        if configured_model:
            payload["override_settings"] = {"sd_model_checkpoint": configured_model}
            payload["override_settings_restore_afterwards"] = True
        try:
            response = httpx.post(
                f"{self.base_url}/sdapi/v1/txt2img", json=payload,
                headers=self._headers(), timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except httpx.TimeoutException as error:
            raise ImageGenerationError("image_generation_timeout", "Image generation timed out for this scene.") from error
        except httpx.HTTPStatusError as error:
            raise ImageGenerationError("image_generation_failed", f"The image backend returned HTTP {error.response.status_code}.") from error
        except (httpx.HTTPError, ValueError) as error:
            raise ImageGenerationError("image_backend_invalid_response", "The image backend failed or returned invalid image data.") from error
        images = result.get("images") if isinstance(result, dict) else None
        if not isinstance(images, list) or not images or not isinstance(images[0], str):
            raise ImageGenerationError("image_generation_empty", "The image backend returned no image for this scene.")
        try:
            raw = base64.b64decode(images[0], validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                if image.width < 64 or image.height < 64:
                    raise ValueError("image dimensions too small")
                actual_width, actual_height = image.size
                converted = image.convert("RGB")
                destination.parent.mkdir(parents=True, exist_ok=True)
                converted.save(destination, format="PNG", optimize=True)
            with Image.open(destination) as verified:
                verified.verify()
        except (ValueError, OSError, UnidentifiedImageError) as error:
            destination.unlink(missing_ok=True)
            raise ImageGenerationError("image_validation_failed", "The generated file was not a valid decodable image.") from error
        return {
            "path": destination,
            "width": actual_width,
            "height": actual_height,
            "seed": seed,
            "model": self.model,
        }


def scene_seed(job_id: str, scene_id: str) -> int:
    digest = hashlib.sha256(f"{job_id}:{scene_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _generation_steps() -> int:
    try:
        steps = int(os.getenv("IMAGE_GENERATION_STEPS", "20"))
    except ValueError as error:
        raise ImageGenerationError("image_configuration_invalid", "IMAGE_GENERATION_STEPS must be a positive integer.") from error
    if not 1 <= steps <= 100:
        raise ImageGenerationError("image_configuration_invalid", "IMAGE_GENERATION_STEPS must be between 1 and 100.")
    return steps
