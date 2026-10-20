from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AspectRatio = Literal["16:9", "9:16", "1:1"]


class SceneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    search_query: str | None = Field(default=None, max_length=200)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Scene text must not be blank")
        return value.strip()


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str | None = Field(default=None, max_length=20000)
    audio_url: str | None = Field(default=None, max_length=2048)
    scenes: list[SceneInput] | None = Field(default=None, min_length=1, max_length=100)
    audio_upload: bool = Field(default=False, exclude=True)
    aspect_ratios: list[AspectRatio] = Field(default_factory=lambda: ["16:9"], min_length=1, max_length=1)
    captions_enabled: bool = True
    voice: str | None = Field(default=None, max_length=100)
    visual_style: Literal["documentary", "cinematic", "educational", "business"] = "documentary"
    visual_source: Literal["footage", "generated_images"] = "footage"
    avoid: list[str] = Field(default_factory=list, max_length=30)
    title: str | None = Field(default=None, max_length=200)

    @field_validator("script")
    @classmethod
    def script_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Script must not be blank")
        return value.strip() if value else value

    @field_validator("aspect_ratios")
    @classmethod
    def aspect_ratios_must_be_unique(cls, value: list[AspectRatio]) -> list[AspectRatio]:
        if len(set(value)) != len(value):
            raise ValueError("Aspect ratios must not contain duplicates")
        if len(value) != 1:
            raise ValueError("Choose exactly one output format so each generation creates one MP4")
        return value

    @field_validator("avoid")
    @classmethod
    def avoid_terms_must_be_bounded(cls, value: list[str]) -> list[str]:
        if any(len(term) > 100 for term in value):
            raise ValueError("Avoid terms must be at most 100 characters long")
        return value

    @model_validator(mode="after")
    def require_one_content_source(self) -> JobInput:
        has_audio = bool(self.audio_url or self.audio_upload)
        if self.script and (has_audio or self.scenes):
            raise ValueError("Provide script alone, audio with optional scenes, or manual scenes")
        if not self.script and not has_audio and not self.scenes:
            raise ValueError("Provide script, audio, or manual scenes")
        if self.audio_url and self.audio_upload:
            raise ValueError("Provide only one audio input")
        if self.script and not self.script.strip():
            raise ValueError("Script must not be blank")
        if any(not word.strip() for word in self.avoid):
            raise ValueError("Avoid terms must not be blank")
        return self
