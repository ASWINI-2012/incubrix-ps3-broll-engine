from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from backend.app.processing import _render_ratio


def _available(executable: str) -> bool:
    return bool(shutil.which(executable) or Path(executable).is_file())


def test_renderer_creates_a_probe_validated_mp4_with_captions(tmp_path):
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe = os.getenv("FFPROBE_PATH", "ffprobe")
    if not _available(ffmpeg) or not _available(ffprobe):
        pytest.skip("FFmpeg/FFprobe are required for renderer integration testing.")

    clip = tmp_path / "fixture-source.mp4"
    narration = tmp_path / "narration.wav"
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=2",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
            "-c:a", "pcm_s16le", str(narration),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (tmp_path / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nA test caption.\n",
        encoding="utf-8",
    )
    scenes = [
        {
            "scene_id": "scene-001",
            "text": "A renderer integration fixture.",
            "duration_seconds": 1.5,
            "clip": {"file_path": str(clip)},
        }
    ]

    expected = {
        "16:9": ("video_16x9.mp4", 1920, 1080),
        "9:16": ("video_9x16.mp4", 1080, 1920),
        "1:1": ("video_1x1.mp4", 1080, 1080),
    }
    for ratio, (filename, width, height) in expected.items():
        result = _render_ratio(
            scenes,
            narration,
            tmp_path,
            ratio,
            captions_enabled=ratio != "9:16",
        )
        assert result["file"] == filename
        assert result["width"] == width
        assert result["height"] == height
        assert result["video_codec"] == "h264"
        assert result["audio_codec"] == "aac"
        assert result["duration_seconds"] == pytest.approx(1.5, abs=0.15)
        assert (tmp_path / result["file"]).stat().st_size > 1000


def test_black_only_video_is_rejected(tmp_path):
    from backend.app.processing import JobProcessingError, _assert_video_has_visible_content
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    if not _available(ffmpeg):
        pytest.skip("FFmpeg is required for black-frame regression testing.")
    black = tmp_path / "black.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(black)], check=True, capture_output=True, timeout=60)
    with pytest.raises(JobProcessingError, match="mostly black"):
        _assert_video_has_visible_content(black)


def test_generated_images_render_to_one_video_with_motion_and_crossfade(tmp_path):
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe = os.getenv("FFPROBE_PATH", "ffprobe")
    if not _available(ffmpeg) or not _available(ffprobe):
        pytest.skip("FFmpeg/FFprobe are required for generated-image renderer integration testing.")
    images = []
    scenes = []
    for index, color in enumerate(((190, 80, 30), (25, 110, 190)), start=1):
        path = tmp_path / "generated_images" / f"scene-{index:03d}.png"
        path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (512, 288), color).save(path)
        images.append(path)
        scenes.append({
            "scene_id": f"scene-{index:03d}",
            "text": f"Generated scene {index}.",
            "start_time": (index - 1) * 0.8,
            "end_time": index * 0.8,
            "duration_seconds": 0.8,
            "clip": {"provider_mode": "generated_images", "file_path": str(path)},
        })
    narration = tmp_path / "narration.wav"
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=1.6", "-c:a", "pcm_s16le", str(narration)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    (tmp_path / "captions.srt").write_text(
        "1\n00:00:00,000 --> 00:00:00,800\nGenerated scene one.\n\n2\n00:00:00,800 --> 00:00:01,600\nGenerated scene two.\n",
        encoding="utf-8",
    )
    result = _render_ratio(scenes, narration, tmp_path, "16:9", captions_enabled=False)
    output = tmp_path / result["file"]
    assert output.is_file() and output.stat().st_size > 1000
    assert result["width"] == 1920 and result["height"] == 1080
    assert result["audio_codec"] == "aac"
    assert result["duration_seconds"] == pytest.approx(1.6, abs=0.2)
