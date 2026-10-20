"""Bounded local media catalogue and opt-in Pexels dataset collector."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


CATEGORIES = [
    "Healthcare and medicine", "Education and students", "Artificial intelligence and technology",
    "Computers and software", "Business and finance", "Agriculture and farming",
    "Nature and environment", "Renewable energy", "Transportation and electric vehicles",
    "Cities and infrastructure", "Construction and engineering", "Food and cooking",
    "Sports and fitness", "Science and research", "Space and astronomy", "Travel and tourism",
    "Water and oceans", "Manufacturing and industry", "Family and everyday life", "General-purpose B-roll",
]
QUERIES = {
    "Healthcare and medicine": "healthcare doctor hospital", "Education and students": "students classroom",
    "Artificial intelligence and technology": "artificial intelligence technology", "Computers and software": "computer software coding",
    "Business and finance": "business finance office", "Agriculture and farming": "agriculture farming",
    "Nature and environment": "nature environment forest", "Renewable energy": "renewable energy solar wind",
    "Transportation and electric vehicles": "electric vehicle transportation", "Cities and infrastructure": "city infrastructure",
    "Construction and engineering": "construction engineering", "Food and cooking": "food cooking kitchen",
    "Sports and fitness": "sports fitness", "Science and research": "science research laboratory",
    "Space and astronomy": "space astronomy", "Travel and tourism": "travel tourism",
    "Water and oceans": "ocean water sea", "Manufacturing and industry": "manufacturing industry",
    "Family and everyday life": "family everyday life", "General-purpose B-roll": "people working nature",
}
LICENSE_NAME = "Pexels License"
LICENSE_URL = "https://www.pexels.com/license/"


def _provider_search(query: str, key: str, per_page: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = httpx.get("https://api.pexels.com/v1/videos/search", params={"query": query, "per_page": per_page}, headers={"Authorization": key}, timeout=20)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
    assert last_error is not None
    raise last_error


def root() -> Path:
    return Path(os.getenv("MEDIA_LIBRARY_DIR", "./data/media_library")).resolve()


def _catalog_path() -> Path:
    return root() / "clips.json"


def _load() -> dict[str, Any]:
    try:
        return json.loads(_catalog_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "last_run": None, "failures": [], "status": "idle"}


def _save(data: dict[str, Any]) -> None:
    root().mkdir(parents=True, exist_ok=True)
    temp = _catalog_path().with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(_catalog_path())


def _inspect(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".webm"}:
        exe = os.getenv("FFPROBE_PATH", "ffprobe")
        if not shutil.which(exe) and not Path(exe).is_file():
            return None
        result = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            return None
        data = json.loads(result.stdout)
        stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        duration = float(data.get("format", {}).get("duration", 0))
        if not stream or duration <= 0:
            return None
        return {"duration_seconds": duration, "width": stream.get("width"), "height": stream.get("height"), "aspect_ratio": round(stream["width"] / stream["height"], 4) if stream.get("width") and stream.get("height") else None}
    signatures = {".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".png": (b"\x89PNG\r\n\x1a\n",), ".webp": (b"RIFF",)}
    if suffix not in signatures:
        return None
    with path.open("rb") as f:
        head = f.read(12)
    if not any(head.startswith(magic) for magic in signatures[suffix]):
        return None
    exe = os.getenv("FFPROBE_PATH", "ffprobe")
    if not shutil.which(exe) and not Path(exe).is_file():
        return None
    try:
        result = subprocess.run([exe, "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            return None
        stream = next((s for s in json.loads(result.stdout).get("streams", []) if s.get("codec_type") == "video"), None)
        if not stream or not stream.get("width") or not stream.get("height"):
            return None
        return {"width": stream["width"], "height": stream["height"], "aspect_ratio": round(stream["width"] / stream["height"], 4)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def scan() -> dict[str, Any]:
    return import_local_media()


def _sidecar_for(path: Path) -> Path | None:
    candidates = (path.with_name(path.name + ".json"), path.with_suffix(".json"))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _words(text: str) -> list[str]:
    import re
    ignored = {"the", "and", "for", "with", "from", "clip", "video", "image"}
    return list(dict.fromkeys(word for word in re.findall(r"[a-z0-9]{3,}", text.lower()) if word not in ignored))


def import_local_media() -> dict[str, Any]:
    """Validate files under MEDIA_LIBRARY_DIR and register them in clips.json.

    Optional `<filename>.<ext>.json` sidecars provide source and rights facts. Missing
    rights details are kept unknown and the asset is excluded from scene matching.
    """
    library = root()
    library.mkdir(parents=True, exist_ok=True)
    data = _load()
    known = {item.get("path"): item for item in data.get("items", []) if item.get("path")}
    by_hash = {item.get("sha256"): item for item in known.values() if item.get("sha256")}
    imported = invalid = duplicates = reviewed = 0
    failures: list[str] = []
    for path in library.rglob("*"):
        if not path.is_file() or path.name == "clips.json" or path.name.endswith(".json") or path.suffix.lower() not in {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}:
            continue
        relative = str(path.relative_to(library)).replace("\\", "/")
        try:
            facts = _inspect(path)
            if facts is None:
                invalid += 1
                failures.append(f"{relative}: invalid or unsupported media")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            existing = known.get(relative)
            duplicate = by_hash.get(digest)
            if duplicate and duplicate.get("path") != relative:
                duplicates += 1
                continue
            sidecar = _sidecar_for(path)
            metadata: dict[str, Any] = {}
            if sidecar:
                try:
                    parsed = json.loads(sidecar.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        metadata = parsed
                    else:
                        failures.append(f"{relative}: sidecar must contain a JSON object")
                except (OSError, json.JSONDecodeError):
                    failures.append(f"{relative}: sidecar could not be read as JSON")
            previous = existing or {}
            inferred_category = path.parent.name.replace("-", " ").replace("_", " ") if path.parent != library else "General-purpose B-roll"
            category = str(metadata.get("category") or previous.get("category") or inferred_category)
            title = str(metadata.get("caption") or previous.get("caption") or path.stem.replace("-", " ").replace("_", " "))
            supplied_tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else None
            old_tags = previous.get("tags") if isinstance(previous.get("tags"), list) else []
            tags = supplied_tags if supplied_tags is not None else old_tags or _words(path.stem)
            if supplied_tags is not None:
                tags = tags + _words(path.stem)
            tags = list(dict.fromkeys(str(tag) for tag in tags if str(tag).strip()))
            description = metadata.get("description", previous.get("description"))
            source_url = metadata.get("source_url", previous.get("source_url"))
            provider = metadata.get("provider", previous.get("provider"))
            provider_source = metadata.get("provider_source", previous.get("provider_source"))
            source_id = metadata.get("source_id", previous.get("source_id"))
            creator = metadata.get("creator", previous.get("creator"))
            license_name = metadata.get("license_name", previous.get("license_name"))
            license_url = metadata.get("license_url", previous.get("license_url"))
            attribution_required = metadata.get("attribution_required", previous.get("attribution_required"))
            download_date = metadata.get("download_date", previous.get("download_date"))
            rights_value = metadata.get("usage_rights_confirmed", previous.get("usage_rights_confirmed"))
            missing = _missing_rights_fields({
                "source_url": source_url, "provider": provider, "creator": creator,
                "license_name": license_name, "license_url": license_url,
                "attribution_required": attribution_required,
                "usage_rights_confirmed": rights_value,
            })
            provenance_reasons = {"source_url", "provider", "creator", "license_name", "license_url", "attribution_required", "usage_rights_confirmed"}
            prior_reasons = previous.get("review_reasons") if isinstance(previous.get("review_reasons"), list) else []
            review_reasons = list(dict.fromkeys(missing + [reason for reason in prior_reasons if reason not in provenance_reasons and reason not in missing]))
            visual_review_required = bool(previous.get("visual_review_required") or "visual_context_review" in review_reasons)
            item = {
                **(existing or {}), "id": (existing or {}).get("id") or str(uuid.uuid4()),
                "path": relative, "media_type": "video" if path.suffix.lower() in {".mp4", ".mov", ".webm"} else "image",
                "category": category, "caption": title, "description": description, "tags": tags,
                "source_url": source_url, "provider": provider, "provider_source": provider_source,
                "source_id": source_id, "creator": creator,
                "license_name": license_name, "license_url": license_url,
                "attribution_required": attribution_required, "usage_rights_confirmed": rights_value,
                "download_date": download_date, "imported_at": datetime.now(timezone.utc).isoformat(),
                "sha256": digest, "status": "valid", "validation": facts,
                "rights_review_required": bool(missing), "rights_review_reasons": missing,
                "visual_review_required": visual_review_required,
                "review_required": bool(missing) or visual_review_required, "review_reasons": review_reasons,
                "caption_source": "sidecar" if metadata.get("caption") else previous.get("caption_source", "filename"),
                "tags_source": "sidecar+filename" if supplied_tags is not None else previous.get("tags_source", "filename-derived"),
            }
            if existing:
                known[relative] = item
            else:
                known[relative] = item
                imported += 1
            by_hash[digest] = item
            reviewed += int(bool(missing))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            invalid += 1
            failures.append(f"{relative}: validation failed ({type(error).__name__})")
    data["items"] = list(known.values())
    data["last_import"] = datetime.now(timezone.utc).isoformat()
    data["import_failures"] = failures
    _save(data)
    return {**summary(data), "imported": imported, "invalid": invalid, "duplicates_skipped": duplicates, "review_required": reviewed, "import_failures": failures}


def summary(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or _load()
    items = data.get("items", [])
    videos = [item for item in items if item.get("media_type") == "video"]
    approved = lambda item: item.get("status") == "valid" and _rights_approved(item) and not item.get("visual_review_required", False)
    return {"items": items, "valid_videos": sum(x.get("status") == "valid" and x.get("media_type") == "video" for x in items), "valid_images": sum(x.get("status") == "valid" and x.get("media_type") == "image" for x in items), "usable_videos": sum(approved(x) for x in videos), "review_required_count": sum(not approved(x) for x in videos), "rights_approved_count": sum(_rights_approved(x) for x in videos), "rights_review_required_count": sum(not _rights_approved(x) for x in videos), "rights_rejected_count": sum(x.get("usage_rights_confirmed") is False for x in videos), "visual_review_required_count": sum(bool(x.get("visual_review_required")) for x in videos), "categories": sorted({x.get("category", "Uncategorized") for x in items} | set(CATEGORIES)), "storage_bytes": sum((root() / x["path"]).stat().st_size for x in items if x.get("path") and (root() / x["path"]).is_file()), "status": data.get("status", "idle"), "last_run": data.get("last_run"), "failures": data.get("failures", []), "source_availability": {"pexels": "configured" if os.getenv("PEXELS_API_KEY", "").strip() else "missing API key", "local": "available"}}


def match_scene(text: str, used_ids: set[str] | None = None, relevance_text: str | None = None) -> dict[str, Any] | None:
    """Return a local media record only when its catalogue metadata overlaps meaningfully."""
    return _rank_catalogue_scene(text, used_ids=used_ids, allow_review=False, relevance_text=relevance_text)


def match_visual_candidate(text: str) -> dict[str, Any] | None:
    """Return a metadata-relevant visual candidate for library diagnostics only.

    Rights and content review flags are intentionally ignored here; this result must
    never be used to select production footage. Production selection uses match_scene.
    """
    candidate = _rank_catalogue_scene(text, allow_review=True)
    if candidate:
        candidate["visual_candidate_only"] = True
    return candidate


def _rank_catalogue_scene(
    text: str,
    used_ids: set[str] | None = None,
    allow_review: bool = False,
    relevance_text: str | None = None,
) -> dict[str, Any] | None:
    import re
    stop = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "into", "their"}
    terms = {word for word in re.findall(r"[a-z0-9]{3,}", text.lower()) if word not in stop}
    if not terms:
        return None
    relevance_terms = {
        word for word in re.findall(r"[a-z0-9]{3,}", (relevance_text or text).lower())
        if word not in stop
    }
    ranked = []
    for item in _load().get("items", []):
        if item.get("status") != "valid" or item.get("media_type", "video") != "video" or (not allow_review and (not _rights_approved(item) or item.get("visual_review_required", False))) or (used_ids and item.get("id") in used_ids):
            continue
        # A folder/category is organizational metadata, not evidence that the footage
        # depicts that topic. Match against descriptions and searchable tags only.
        # Use the descriptive media filename as an additional search hint. Imported
        # filenames are curated from the reviewed caption/description; they do not
        # replace the source, licence, or visual review checks above.
        filename_terms = Path(item.get("path", "")).stem.replace("_", " ").replace("-", " ")
        searchable = " ".join([
            item.get("caption", ""),
            item.get("description") or "",
            " ".join(item.get("tags", [])),
            filename_terms,
        ]).lower()
        words = set(re.findall(r"[a-z0-9]{3,}", searchable))
        score = len(relevance_terms & words) / len(relevance_terms) if relevance_terms else 0.0
        ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].get("id", "")))
    if not ranked or ranked[0][0] < float(os.getenv("MEDIA_MIN_MATCH_SCORE", "0.70")):
        return None
    return {**ranked[0][1], "relevance_score": round(ranked[0][0], 4)}


HEALTHCARE_SCENES = {
    "clinical consultation": "doctor consultation patient medical information",
    "clinical examination with stethoscope": "doctor stethoscope clinical care medical examination",
    "telemedicine": "telemedicine video consultation doctor",
    "wearable health monitoring": "wearable device smartwatch health monitoring",
    "ECG and heart monitoring": "ECG heart-monitor display medical monitoring",
    "MRI scan": "MRI scanner patient medical imaging scan",
    "AI-assisted medical imaging": "artificial intelligence machine learning algorithm radiology diagnosis",
    "hospital and surgical care": "surgeons operating room surgical care",
    "patient care": "healthcare professional patient care clinical care",
    "medical paperwork": "medical paperwork patient records medical chart",
    "pulse check": "healthcare worker checking patient pulse wrist",
    "medical research": "medical research laboratory scientists",
}


def validate_healthcare() -> dict[str, Any]:
    data = _load()
    report = []
    for scene, query in HEALTHCARE_SCENES.items():
        production_match = match_scene(query)
        visual_match = production_match or match_visual_candidate(query)
        rights_blocked = bool(visual_match and visual_match.get("rights_review_required", visual_match.get("review_required", False)))
        content_blocked = bool(visual_match and visual_match.get("visual_review_required", False))
        status = "usable" if production_match else "blocked_by_review" if visual_match else "missing"
        report.append({
            "scene": scene, "status": status,
            "media_id": visual_match.get("id") if visual_match else None,
            "path": visual_match.get("path") if visual_match else None,
            "relevance_score": visual_match.get("relevance_score") if visual_match else None,
            "rights_review_required": rights_blocked,
            "visual_review_required": content_blocked,
        })
    stats = summary(data)
    visually_relevant_ids = {item["media_id"] for item in report if item["media_id"]}
    return {
        "usable_videos": stats["usable_videos"], "valid_images": stats["valid_images"],
        "review_required": stats["review_required_count"],
        "rights_review_required": stats["rights_review_required_count"],
        "visual_review_required": stats["visual_review_required_count"],
        "visually_relevant_videos": len(visually_relevant_ids),
        "healthcare_scenes": report,
        "missing_scenes": [item["scene"] for item in report if item["status"] == "missing"],
        "rights_blocked_scenes": [item["scene"] for item in report if item["rights_review_required"]],
        "visual_review_blocked_scenes": [item["scene"] for item in report if item["visual_review_required"]],
    }


RIGHTS_FIELD_LABELS = {
    "source_url": "original source video page URL",
    "provider": "media provider",
    "creator": "creator/uploader name",
    "license_name": "licence name",
    "license_url": "licence URL",
    "attribution_required": "attribution requirement",
    "usage_rights_confirmed": "explicit usage-rights confirmation",
}


def _missing_rights_fields(item: dict[str, Any]) -> list[str]:
    """Re-evaluate approval from required evidence instead of trusting stale flags."""
    missing: list[str] = []
    for field in RIGHTS_FIELD_LABELS:
        value = item.get(field)
        if field == "usage_rights_confirmed":
            if value is not True:
                missing.append(field)
        elif not isinstance(value, str) or not value.strip():
            missing.append(field)
        elif field in {"source_url", "license_url"}:
            try:
                parsed = urlsplit(value.strip())
                if parsed.scheme != "https" or not parsed.hostname:
                    missing.append(field)
            except ValueError:
                missing.append(field)
    return list(dict.fromkeys(missing))


def _rights_approved(item: dict[str, Any]) -> bool:
    return not _missing_rights_fields(item)


def rights_review_report(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a read-only report of catalogue rights gaps and concrete follow-ups."""
    data = data or _load()
    report_items = []
    for item in data.get("items", []):
        if not isinstance(item, dict) or item.get("media_type") != "video":
            continue
        missing = _missing_rights_fields(item)
        steps = []
        if "source_url" in missing:
            steps.append("Find and record the original Pexels video page URL for this exact downloaded file.")
        if item.get("provider_source") == "user_reported":
            steps.append("Confirm the exact source page identifies Pexels as the provider; current provider value is user-reported.")
        if "creator" in missing:
            steps.append("Read the creator/uploader name from the original video page and record it exactly.")
        if "license_name" in missing or "license_url" in missing:
            steps.append("Check the licence applicable to the exact source asset and record its name and official URL.")
        if "attribution_required" in missing:
            steps.append("Check the applicable licence/source page and record its attribution requirement; do not assume attribution is or is not required.")
        if "usage_rights_confirmed" in missing:
            steps.append("After reviewing the source and applicable terms for the intended use, explicitly confirm rights only if supported.")
        if item.get("visual_review_required"):
            steps.append("Review the full clip and resolve the existing visual-context review note before relying on the depicted context.")
        report_items.append({
            "media_id": item.get("id"),
            "filename": item.get("path"),
            "source_url": item.get("source_url"),
            "provider": item.get("provider"),
            "provider_source": item.get("provider_source"),
            "creator": item.get("creator"),
            "license_name": item.get("license_name"),
            "license_url": item.get("license_url"),
            "attribution_required": item.get("attribution_required"),
            "usage_rights_confirmed": item.get("usage_rights_confirmed"),
            "usage_rights_status": (
                "confirmed" if item.get("usage_rights_confirmed") is True
                else "not confirmed" if item.get("usage_rights_confirmed") is False
                else "unknown / not recorded"
            ),
            "rights_status": (
                "approved" if not missing
                else "rejected" if item.get("usage_rights_confirmed") is False
                else "pending_review"
            ),
            "rights_review_required": bool(missing) or bool(item.get("rights_review_required", False)),
            "rights_review_reasons": list(dict.fromkeys(missing + [
                reason for reason in item.get("rights_review_reasons", item.get("review_reasons", []))
                if reason in missing
            ])),
            "missing_rights_fields": missing,
            "required_verification_steps": steps,
            "visual_review_required": bool(item.get("visual_review_required", False)),
            "visual_review_status": item.get("visual_review_status"),
            "content_review_note": item.get("content_review_note"),
        })
    return {
        "catalogue_path": str(_catalog_path()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "media_items": len(report_items),
        "rights_approved": sum(entry["rights_status"] == "approved" for entry in report_items),
        "rights_review_required": sum(entry["rights_review_required"] for entry in report_items),
        "rights_rejected": sum(entry["rights_status"] == "rejected" for entry in report_items),
        "items": report_items,
    }


def collect(dry_run: bool = True, max_assets: int = 20) -> dict[str, Any]:
    max_assets = max(0, min(int(max_assets), int(os.getenv("MEDIA_MAX_FILES_PER_RUN", "20")), 100))
    data = _load()
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        data.update(status="unavailable", failures=["Pexels unavailable: PEXELS_API_KEY is not configured; local media remains available."])
        _save(data)
        return {**summary(data), "dry_run": dry_run, "planned": 0, "downloaded": 0}
    initial_count = len(data["items"])
    used = {item.get("source_id") for item in data["items"]}
    candidates = []
    failures = []
    for category, query in QUERIES.items():
        if len(candidates) >= max_assets:
            break
        try:
            for video in _provider_search(query, key, min(15, max_assets)).get("videos", []):
                if str(video.get("id")) in used:
                    continue
                eligible = [f for f in video.get("video_files", []) if f.get("link") and f.get("file_type") == "video/mp4"]
                if eligible:
                    selected = min(eligible, key=lambda f: abs(int(f.get("width", 1280)) - 1280))
                    candidates.append((category, video, selected))
                    used.add(str(video["id"]))
                    if len(candidates) >= max_assets:
                        break
            time.sleep(float(os.getenv("MEDIA_REQUEST_INTERVAL_SECONDS", "0.5")))
        except (httpx.HTTPError, ValueError, KeyError) as error:
            failures.append(f"{category}: provider search failed ({type(error).__name__}).")
    if dry_run:
        return {**summary(data), "dry_run": True, "planned": len(candidates), "downloaded": 0, "failures": failures}
    max_bytes = int(os.getenv("MEDIA_MAX_FILE_BYTES", str(50 * 1024 * 1024)))
    max_duration = int(os.getenv("MEDIA_MAX_CLIP_SECONDS", "90"))
    storage_limit = int(os.getenv("MEDIA_MAX_STORAGE_BYTES", str(2 * 1024 * 1024 * 1024)))
    current_bytes = summary(data)["storage_bytes"]
    for category, video, selected in candidates:
        duration = float(video.get("duration") or 0)
        if duration <= 0 or duration > max_duration:
            continue
        target = root() / category.lower().replace(" and ", "-").replace(" ", "-") / f"pexels-{video['id']}.mp4"
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".part")
        try:
            total = 0
            with httpx.stream("GET", selected["link"], timeout=30, follow_redirects=False) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";")[0].lower() not in {"video/mp4", "application/octet-stream"}:
                    raise ValueError("unsupported content type")
                with temp.open("wb") as output:
                    for chunk in response.iter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes or current_bytes + total > storage_limit:
                            raise ValueError("configured size/storage limit reached")
                        output.write(chunk)
            facts = _inspect(temp)
            if not facts or facts.get("duration_seconds", max_duration + 1) > max_duration:
                raise ValueError("FFprobe validation failed or duration exceeded limit")
            temp.replace(target)
            current_bytes += total
            data["items"].append({"id": str(uuid.uuid4()), "source_id": str(video["id"]), "path": str(target.relative_to(root())).replace("\\", "/"), "media_type": "video", "category": category, "caption": video.get("url", "Pexels video").rsplit("/", 1)[-1].replace("-", " "), "tags": [category.lower(), "pexels"], "source_url": video.get("url"), "provider": "Pexels", "creator": (video.get("user") or {}).get("name"), "license_name": LICENSE_NAME, "license_url": LICENSE_URL, "attribution_required": "Pexels License does not require attribution; credit the creator and Pexels when practical.", "download_date": datetime.now(timezone.utc).isoformat(), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "status": "valid", "validation": facts})
        except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError) as error:
            temp.unlink(missing_ok=True)
            failures.append(f"Pexels video {video.get('id')}: download/validation failed ({type(error).__name__}).")
    data.update(status="completed", last_run=datetime.now(timezone.utc).isoformat(), failures=failures)
    _save(data)
    return {**summary(data), "dry_run": False, "planned": len(candidates), "downloaded": max(0, len(data["items"]) - initial_count), "failures": failures}
