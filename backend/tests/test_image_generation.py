from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from backend.app import image_generation
from backend.app.image_generation import Automatic1111ImageGenerator, ImageGenerationError, image_prompt
from backend.app.processing import JobProcessingError, generate_scene_image, split_script


def _png_bytes(color=(20, 120, 80)):
    output = io.BytesIO()
    Image.new("RGB", (128, 96), color).save(output, format="PNG")
    return output.getvalue()


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            request = image_generation.httpx.Request("POST", "http://127.0.0.1/mock")
            response = image_generation.httpx.Response(self.status_code, request=request)
            raise image_generation.httpx.HTTPStatusError("HTTP error", request=request, response=response)

    def json(self):
        return self.payload


@pytest.mark.parametrize("script, terms, unrelated", [
    ("A doctor reviews a chest X-ray with a patient.", ("doctor", "x-ray"), "irrigation"),
    ("Students learn fractions with colored classroom shapes.", ("students", "fractions"), "satellite"),
    ("Farmers monitor soil moisture while irrigating wheat fields.", ("farmers", "soil moisture"), "classroom"),
    ("A spacecraft sends images of Saturn's rings to Earth.", ("spacecraft", "saturn"), "harvest"),
])
def test_scene_prompts_use_only_their_own_meaning(script, terms, unrelated):
    scenes = split_script(script)
    prompt = image_prompt(scenes[0], "cinematic").lower()
    assert all(term in prompt for term in terms)
    assert unrelated not in prompt
    assert scenes[0]["text"] == script


def test_automatic1111_generates_and_validates_png(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_GENERATION_BACKEND", "automatic1111")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "test-model.safetensors")
    captured = {}
    def fake_get(url, **_kwargs):
        assert url.endswith("/sdapi/v1/options")
        return Response({"sd_model_checkpoint": "loaded-model.safetensors"})
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response({"images": [base64.b64encode(_png_bytes()).decode("ascii")]})
    monkeypatch.setattr(image_generation.httpx, "get", fake_get)
    monkeypatch.setattr(image_generation.httpx, "post", fake_post)
    generator = Automatic1111ImageGenerator()
    assert generator.prepare() == "test-model.safetensors"
    destination = tmp_path / "generated_images" / "scene-001.png"
    result = generator.generate("farmers in a field", destination, seed=12, ratio="16:9", avoid=["text"])
    assert destination.is_file()
    with Image.open(destination) as image:
        assert image.format == "PNG"
        assert image.size == (128, 96)
    assert result["model"] == "test-model.safetensors"
    assert captured["url"].endswith("/sdapi/v1/txt2img")
    assert captured["json"]["seed"] == 12
    assert captured["json"]["negative_prompt"].endswith(", text")


def test_automatic1111_rejects_invalid_image_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_GENERATION_BACKEND", "automatic1111")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "test-model")
    monkeypatch.setattr(image_generation.httpx, "post", lambda *_args, **_kwargs: Response({"images": [base64.b64encode(b"not an image").decode()]}))
    generator = Automatic1111ImageGenerator()
    generator.model = "test-model"
    with pytest.raises(ImageGenerationError, match="valid decodable image"):
        generator.generate("scene", tmp_path / "bad.png", seed=1, ratio="1:1", avoid=[])


def test_unconfigured_image_backend_fails_clearly(monkeypatch):
    monkeypatch.delenv("IMAGE_GENERATION_BACKEND", raising=False)
    with pytest.raises(ImageGenerationError, match="not configured") as error:
        Automatic1111ImageGenerator()
    assert error.value.code == "image_generator_not_configured"


def test_scene_failure_names_scene_and_does_not_substitute(tmp_path):
    class FailingGenerator:
        model = "mock-model"
        def generate(self, *_args, **_kwargs):
            raise ImageGenerationError("backend_error", "mock backend failed")
    scene = split_script("Farmers irrigate the field.")[0]
    with pytest.raises(JobProcessingError, match="scene-001.*mock backend failed"):
        generate_scene_image(FailingGenerator(), scene, tmp_path, job_id="job-one", visual_style="cinematic", ratio="16:9", avoid=[])
    assert not (tmp_path / "generated_images" / "scene-001.png").exists()

