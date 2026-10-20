"""Extract middle frames for videos listed in data/media_library/clips.json.

This read-only catalogue inspection utility is independent of the render pipeline.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import math
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEDIA_ROOT = PROJECT_ROOT / "data" / "media_library"
DEFAULT_CATALOGUE = DEFAULT_MEDIA_ROOT / "clips.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "media_inspection"


def executable(configured: str, fallback: str) -> str:
    found = shutil.which(configured) or shutil.which(fallback)
    if found:
        return found
    candidate = Path(configured)
    try:
        if candidate.is_file():
            return str(candidate)
    except OSError:
        raise FileNotFoundError(f"Configured executable is inaccessible: {candidate}") from None
    raise FileNotFoundError(
        f"Could not find {fallback}. Put it on PATH or configure {fallback.upper()} "
        "in .env / the command line."
    )


def probe_video(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise ValueError((result.stderr or "FFprobe rejected the file").strip())
    try:
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("FFprobe returned invalid metadata") from error
    if duration <= 0 or not any(stream.get("codec_type") == "video" for stream in info.get("streams", [])):
        raise ValueError("file has no usable video stream or duration")
    return duration


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "video"


def extract_frames(
    catalogue: Path,
    media_root: Path,
    output_dir: Path,
    ffmpeg: str,
    ffprobe: str,
) -> tuple[list[tuple[Path, str]], list[str]]:
    try:
        data: dict[str, Any] = json.loads(catalogue.read_text(encoding="utf-8"))
        items = data["items"]
        if not isinstance(items, list):
            raise ValueError("catalogue 'items' must be a list")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"Cannot read media catalogue {catalogue}: {error}") from error

    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, str]] = []
    errors: list[str] = []
    videos = [item for item in items if isinstance(item, dict) and item.get("media_type") == "video"]
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Pillow is required to validate cached frames and draw contact-sheet labels. "
            "Install it with: .\\.venv\\Scripts\\python.exe -m pip install Pillow"
        ) from error

    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    for index, item in enumerate(videos, start=1):
        relative = item.get("path")
        if not isinstance(relative, str) or not relative.strip():
            errors.append(f"Catalogue item {item.get('id', index)}: skipped; missing video path.")
            continue
        source = (media_root / relative).resolve()
        try:
            source.relative_to(media_root.resolve())
        except ValueError:
            errors.append(f"{relative}: skipped; catalogue path escapes the media directory.")
            continue
        if not source.is_file():
            errors.append(f"{relative}: skipped; file not found: {source}")
            continue
        output = output_dir / f"{index:02d}_{_safe_stem(source.name)}.jpg"
        try:
            if output.is_file() and output.stat().st_size > 0:
                try:
                    with Image.open(output) as cached:
                        cached.verify()
                    frames.append((output, source.name))
                    print(f"Reusing existing frame for {source.name} -> {output}")
                    continue
                except (OSError, ValueError):
                    output.unlink(missing_ok=True)
            if ffmpeg_path is None:
                ffmpeg_path = executable(ffmpeg, "ffmpeg")
            if ffprobe_path is None:
                ffprobe_path = executable(ffprobe, "ffprobe")
            duration = probe_video(ffprobe_path, source)
            timestamp = duration / 2
            result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1", "-vf", "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2", "-q:v", "2", str(output)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode or not output.is_file() or output.stat().st_size == 0:
                output.unlink(missing_ok=True)
                raise ValueError((result.stderr or "FFmpeg produced no frame").strip())
            frames.append((output, source.name))
            print(f"Extracted {source.name} at {timestamp:.2f}s -> {output}")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            output.unlink(missing_ok=True)
            errors.append(f"{relative}: skipped; {error}")
    return frames, errors


def create_contact_sheet(frames: list[tuple[Path, str]], output: Path) -> None:
    if not frames:
        raise RuntimeError("No valid video frames were extracted; contact sheet was not created.")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as error:
        raise RuntimeError(
            "Pillow is required to create the labelled contact sheet. "
            "Install it with: .\\.venv\\Scripts\\python.exe -m pip install Pillow"
        ) from error

    columns = 2
    cell_width, cell_height = 660, 420
    rows = math.ceil(len(frames) / columns)
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), color=(28, 31, 36))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:  # Support older Pillow releases.
        font = ImageFont.load_default()

    for index, (frame, filename) in enumerate(frames):
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        try:
            with Image.open(frame) as source:
                thumbnail = source.convert("RGB")
                thumbnail.thumbnail((cell_width - 24, 350), Image.Resampling.LANCZOS)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"Could not open extracted thumbnail {frame}: {error}") from error
        image_x = x + (cell_width - thumbnail.width) // 2
        image_y = y + 8 + (350 - thumbnail.height) // 2
        sheet.paste(thumbnail, (image_x, image_y))
        draw.text((x + 12, y + 370), filename, fill=(255, 255, 255), font=font)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        sheet.save(output, format="JPEG", quality=92, optimize=True)
    except OSError as error:
        raise RuntimeError(f"Could not save contact sheet to {output}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)
    parser.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ffmpeg", default=os.getenv("FFMPEG_PATH", "ffmpeg"))
    parser.add_argument("--ffprobe", default=os.getenv("FFPROBE_PATH", "ffprobe"))
    args = parser.parse_args(argv)
    try:
        frames, errors = extract_frames(args.catalogue.resolve(), args.media_root.resolve(), args.output_dir.resolve(), args.ffmpeg, args.ffprobe)
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        contact_sheet = args.output_dir.resolve() / "contact_sheet.jpg"
        if frames:
            create_contact_sheet(frames, contact_sheet)
            print(f"Contact sheet: {contact_sheet}")
        print(f"Summary: {len(frames)} frame(s) extracted; {len(errors)} video(s) skipped.")
        return 1 if errors or not frames else 0
    except (FileNotFoundError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
