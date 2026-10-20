from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import database
from . import media_library
from .models import JobInput
from .processing import audio_size_limit, process_job
from .providers import provider_configuration


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a"}


@asynccontextmanager
async def lifespan(application: FastAPI):
    database.initialize()
    pool = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("JOB_WORKERS", "2"))))
    application.state.executor = pool
    yield
    pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="IncuBrix Script-to-B-roll Video Engine",
    version="0.1.0",
    description="Create narrated, captioned videos from scripts, audio, or manually planned scenes.",
    lifespan=lifespan,
)
app.openapi_version = "3.1.0"
origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def assign_request_id(request: Request, call_next):
    provided_id = request.headers.get("X-Request-ID", "").strip()
    request_id = provided_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", provided_id) else str(uuid.uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled request error (%s)", request_id)
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_server_error",
                    "message": "The request could not be completed.",
                    "request_id": request_id,
                }
            },
        )
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, error: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "The request is invalid.",
                "details": [
                    {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
                    for item in error.errors()
                ],
                "request_id": request.state.request_id,
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException):
    code = {
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
    }.get(error.status_code, "validation_error" if error.status_code == 422 else "request_error")
    message = "The request is invalid." if error.status_code == 422 else error.detail
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request.state.request_id,
                **(
                    {"details": [{"message": error.detail}]}
                    if error.status_code == 422
                    else {}
                ),
            }
        },
    )


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = job["result"]
    if result:
        result = {
            **result,
            "videos": [
                {
                    **video,
                    "url": f"/api/v1/jobs/{job['job_id']}/files/{video['file']}",
                }
                for video in result["videos"]
            ],
            "captions_url": f"/api/v1/jobs/{job['job_id']}/files/captions.srt",
            "timeline_url": f"/api/v1/jobs/{job['job_id']}/timeline",
            "manifest_url": f"/api/v1/jobs/{job['job_id']}/files/manifest.json",
            "narration_url": f"/api/v1/jobs/{job['job_id']}/files/{result['narration']}",
        }
        if result.get("image_generation_report"):
            result["image_generation_report_url"] = f"/api/v1/jobs/{job['job_id']}/files/image_generation_report.json"
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job["progress"],
        "current_stage": job["current_stage"],
        "error": job["error"],
        "result": result,
        "created_at": job["created_at"],
        "completed_at": job["completed_at"],
    }


async def parse_create_request(request: Request) -> tuple[dict[str, Any], StarletteUploadFile | None]:
    content_type = request.headers.get("content-type", "").lower()
    upload: StarletteUploadFile | None = None
    if content_type.startswith("application/json"):
        try:
            data = await request.json()
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="Request body must contain valid JSON.") from error
        if isinstance(data, dict) and data.get("audio_upload"):
            raise HTTPException(status_code=400, detail="Audio uploads must use multipart/form-data.")
    elif content_type.startswith("multipart/form-data"):
        form = await request.form()
        data: dict[str, Any] = {}
        for field in ("script", "audio_url", "voice", "visual_style", "title"):
            value = form.get(field)
            if value is not None and value != "":
                data[field] = value
        for field in ("scenes", "aspect_ratios", "avoid"):
            value = form.get(field)
            if value not in (None, ""):
                try:
                    data[field] = json.loads(str(value))
                except json.JSONDecodeError as error:
                    raise HTTPException(status_code=400, detail=f"Multipart field '{field}' must contain valid JSON.") from error
        captions = form.get("captions_enabled")
        if captions is not None:
            if str(captions).lower() not in {"true", "false"}:
                raise HTTPException(status_code=400, detail="captions_enabled must be true or false.")
            data["captions_enabled"] = str(captions).lower() == "true"
        candidate = form.get("audio")
        if isinstance(candidate, StarletteUploadFile):
            upload = candidate
            extension = Path(candidate.filename or "").suffix.lower()
            if extension not in ALLOWED_AUDIO_EXTENSIONS:
                raise HTTPException(status_code=415, detail="Audio uploads must be MP3, WAV, or M4A.")
            data["audio_upload"] = True
    else:
        raise HTTPException(status_code=415, detail="Use application/json or multipart/form-data.")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    try:
        validated = JobInput.model_validate(data)
    except ValidationError as error:
        raise HTTPException(
            status_code=422,
            detail="; ".join(item["msg"] for item in error.errors()),
        ) from error
    normalized = validated.model_dump(mode="json", exclude={"audio_upload"})
    if upload:
        normalized["audio_upload"] = True
    return normalized, upload


@app.post(
    "/api/v1/broll/jobs",
    status_code=202,
    tags=["jobs"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "script": {"type": "string", "maxLength": 20000},
                            "audio_url": {"type": "string", "format": "uri"},
                            "scenes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["text"],
                                    "properties": {
                                        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                                        "duration_seconds": {"type": "number", "exclusiveMinimum": 0},
                                        "search_query": {"type": "string", "maxLength": 200},
                                    },
                                },
                            },
                            "aspect_ratios": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 1,
                                "items": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
                            },
                            "captions_enabled": {"type": "boolean", "default": True},
                            "voice": {"type": "string"},
                            "visual_style": {
                                "type": "string",
                                "enum": ["documentary", "cinematic", "educational", "business"],
                            },
                            "avoid": {"type": "array", "items": {"type": "string"}},
                            "title": {"type": "string"},
                        },
                        "anyOf": [
                            {"required": ["script"]},
                            {"required": ["audio_url"]},
                            {"required": ["scenes"]},
                        ],
                        "allOf": [
                            {"not": {"required": ["script", "audio_url"]}},
                            {"not": {"required": ["script", "scenes"]}},
                        ],
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "audio": {"type": "string", "format": "binary"},
                            "audio_url": {"type": "string", "format": "uri"},
                            "script": {"type": "string"},
                            "scenes": {"type": "string", "description": "JSON array of scene objects"},
                            "aspect_ratios": {"type": "string", "description": "JSON array of aspect ratios"},
                            "captions_enabled": {"type": "boolean"},
                            "voice": {"type": "string"},
                            "visual_style": {"type": "string"},
                            "avoid": {"type": "string", "description": "JSON array of terms"},
                            "title": {"type": "string"},
                        },
                    }
                },
            },
        }
    },
)
async def create_job(request: Request):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdecimal() and int(content_length) > audio_size_limit() + 1024 * 1024:
        raise HTTPException(status_code=413, detail="Request exceeds the configured upload size limit.")
    data, upload = await parse_create_request(request)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    temporary_path: str | None = None
    upload_hash = ""
    if upload:
        extension = Path(upload.filename or "").suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as temporary:
            temporary_path = temporary.name
            total = 0
            while True:
                chunk = await upload.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > audio_size_limit():
                    Path(temporary_path).unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Audio upload exceeds the configured size limit.")
                digest.update(chunk)
                temporary.write(chunk)
        if total == 0:
            Path(temporary_path).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Audio upload is empty.")
        upload_hash = digest.hexdigest()

    request_hash = hashlib.sha256(digest.digest() + upload_hash.encode("ascii")).hexdigest()
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key and (len(idempotency_key) > 200 or not idempotency_key.strip()):
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Idempotency-Key must be between 1 and 200 characters.")
    if idempotency_key:
        idempotency_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    job_id = str(uuid.uuid4())
    try:
        job, is_new = database.create_job(job_id, request_hash, data, idempotency_key)
    except ValueError as error:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
        raise
    if not is_new:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
        return JSONResponse(status_code=202, content=public_job(job))
    request.app.state.executor.submit(process_job, job_id, data, temporary_path)
    response = public_job(job)
    response["request_id"] = request.state.request_id
    return response


@app.get("/api/v1/jobs/{job_id}", tags=["jobs"])
async def get_job(job_id: str):
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job was not found.")
    return public_job(job)


@app.get("/api/v1/jobs/{job_id}/result", tags=["jobs"])
async def get_result(job_id: str):
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job was not found.")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job has not completed successfully.")
    return public_job(job)["result"]


@app.get("/api/v1/jobs/{job_id}/timeline", tags=["jobs"])
async def get_timeline(job_id: str):
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job was not found.")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Timeline is available when the job completes.")
    return job["timeline"]


@app.get("/api/v1/jobs/{job_id}/files/{filename}", tags=["jobs"])
async def get_job_file(job_id: str, filename: str):
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job was not found.")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job files are available when the job completes.")
    allowed = {"captions.srt", "manifest.json", "timeline.json", "rights_review_report.json", "image_generation_report.json", job["result"]["narration"]}
    allowed.update(video["file"] for video in job["result"]["videos"])
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="Job file was not found.")
    path = Path(os.getenv("OUTPUT_DIR", "./outputs")).resolve() / job_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Job file was not found.")
    return FileResponse(path)


@app.get("/api/v1/jobs/{job_id}/images/{filename}", tags=["jobs"])
async def get_job_generated_image(job_id: str, filename: str):
    job = database.get_job(job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(status_code=404, detail="Generated image was not found.")
    report = job["result"].get("image_generation") or {}
    entry = next((item for item in report.get("images", []) if Path(item.get("file", "")).name == filename), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Generated image was not found.")
    path = Path(os.getenv("OUTPUT_DIR", "./outputs")).resolve() / job_id / "generated_images" / filename
    root = (Path(os.getenv("OUTPUT_DIR", "./outputs")).resolve() / job_id / "generated_images").resolve()
    if root not in path.resolve().parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Generated image was not found.")
    return FileResponse(path, media_type="image/png")


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok"}


@app.get("/api/v1/media", tags=["media"])
async def get_media_library():
    return media_library.scan()


@app.post("/api/v1/media/scan", tags=["media"])
async def scan_media_library():
    return media_library.scan()


@app.get("/api/v1/media/{media_id}/file", tags=["media"])
async def get_media_file(media_id: str):
    item = next((entry for entry in media_library._load().get("items", []) if entry.get("id") == media_id), None)
    if not item or item.get("status") != "valid":
        raise HTTPException(status_code=404, detail="Media item was not found.")
    path = (media_library.root() / item["path"]).resolve()
    if media_library.root() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Media item was not found.")
    return FileResponse(path)


@app.post("/api/v1/media/download", tags=["media"])
async def download_media_library(dry_run: bool = True, max_assets: int = 20):
    if max_assets < 0 or max_assets > 100:
        raise HTTPException(status_code=422, detail="max_assets must be between 0 and 100.")
    return media_library.collect(dry_run=dry_run, max_assets=max_assets)


@app.get("/api/v1/voices", tags=["system"])
async def available_voices():
    try:
        import pyttsx3

        engine = pyttsx3.init()
        voices = [
            {"id": str(voice.id), "name": str(voice.name)}
            for voice in engine.getProperty("voices")
        ]
        engine.stop()
        return {"voices": voices}
    except Exception as error:
        logger.exception("Local voice enumeration failed")
        raise HTTPException(status_code=503, detail="Local text-to-speech voices are unavailable.") from error


@app.get("/ready", tags=["system"])
async def ready():
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe = os.getenv("FFPROBE_PATH", "ffprobe")

    def available(value: str) -> bool:
        if shutil.which(value):
            return True
        try:
            return Path(value).is_file()
        except OSError:
            # A configured binary may exist behind a Windows ACL boundary.
            return False

    missing = [
        name
        for name, value in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
        if not available(value)
    ]
    footage_provider = provider_configuration()
    checks = {
        "database": "ready",
        "ffmpeg": "ready" if "ffmpeg" not in missing else "missing",
        "ffprobe": "ready" if "ffprobe" not in missing else "missing",
        "footage_provider": footage_provider,
        "image_generation": {
            "configured": os.getenv("IMAGE_GENERATION_BACKEND", "").strip().lower() in {"automatic1111", "a1111"},
            "backend": os.getenv("IMAGE_GENERATION_BACKEND", "").strip() or None,
            "model": os.getenv("IMAGE_GENERATION_MODEL", "").strip() or None,
        },
    }
    return JSONResponse(
        status_code=200 if not missing else 503,
        content={"status": "ready" if not missing else "not_ready", "checks": checks},
    )
