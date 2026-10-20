from __future__ import annotations

import os
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.app import database, processing


def _available(executable: str) -> bool:
    return bool(shutil.which(executable) or Path(executable).is_file())


def test_pipeline_reports_missing_relevant_demo_media_instead_of_rendering_unrelated_video(tmp_path, monkeypatch):
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe = os.getenv("FFPROBE_PATH", "ffprobe")
    if not _available(ffmpeg) or not _available(ffprobe):
        pytest.skip("FFmpeg/FFprobe are required for end-to-end render testing.")

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("BROLL_PROVIDER", "demo")
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    database.initialize()

    source_audio = tmp_path / "voice.wav"
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
            "-c:a", "pcm_s16le", str(source_audio),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    request_data = {
        "audio_upload": True,
        "scenes": [{"text": "Telemedicine lets patients consult a doctor remotely."}],
        "aspect_ratios": ["16:9"],
        "captions_enabled": True,
        "visual_style": "documentary",
        "avoid": [],
    }
    database.create_job("pipeline-test-job", "fixture-hash", request_data, None)

    processing.process_job("pipeline-test-job", request_data, str(source_audio))

    job = database.get_job("pipeline-test-job")
    assert job["status"] == "failed"
    assert job["error"]["code"] == "no_footage_found"
    result_folder = Path(os.getenv("OUTPUT_DIR")) / "pipeline-test-job"
    video = result_folder / "video_16x9.mp4"
    assert not video.exists()
    assert not (result_folder / "scene-001.source.mp4").exists()
    assert "telemedicine" in job["error"]["message"].lower()
    assert "remotely" in job["error"]["message"].lower()
