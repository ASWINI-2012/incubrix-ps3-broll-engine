from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import html
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import httpx

from . import database


MAX_VIDEO_BYTES = 150 * 1024 * 1024
PROVIDER_TIMEOUT_SECONDS = 20
_API_REQUEST_LOCK = threading.Lock()
_API_REQUEST_TIMES: dict[str, float] = {}
DEMO_WARNING = (
    "Local demo mode uses relevant validated media from the local catalogue. "
    "A scene without a suitable local clip fails clearly instead of using unrelated footage."
)
DEMO_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "demo"
    / "earth-from-iss-preview.mp4"
)
DEMO_SOURCE_URL = (
    "https://images-assets.nasa.gov/video/Earth%20Views%20from%20the%20International%20Space%20Station/collection.json"
)
DEMO_DOWNLOAD_URL = (
    "https://images-assets.nasa.gov/video/Earth%20Views%20from%20the%20International%20Space%20Station/Earth%20Views%20from%20the%20International%20Space%20Station~preview.mp4"
)
NASA_USAGE_WARNING = (
    "NASA says its media is generally not subject to U.S. copyright, but third-party, "
    "privacy, publicity, and endorsement restrictions may apply; review the source guidance."
)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "their", "this", "to",
    "was", "were", "will", "with", "you", "your",
}


class FootageProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FootageAsset:
    asset_id: str
    provider: str
    source_url: str | None
    download_url: str | None
    creator: str | None
    width: int | None
    height: int | None
    matching_text: str
    provider_rank: int
    license_name: str
    license_url: str | None
    attribution_required: str
    source_description: str
    warnings: list[str]


class FootageProvider(Protocol):
    name: str
    mode: str
    matching_method: str
    fallback_warnings: list[str]

    def search(
        self, scene: dict[str, Any], avoid: list[str], visual_style: str
    ) -> list[FootageAsset]: ...

    def download(self, asset: FootageAsset, destination: Path) -> None: ...


def _query_terms(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    terms = [word for word in words if word not in STOP_WORDS and len(word) > 2]
    return list(dict.fromkeys(terms))[:limit]


def _pace_public_api(source: str, interval: float) -> None:
    """Keep API traffic polite across concurrent in-process scene jobs."""
    with _API_REQUEST_LOCK:
        now = time.monotonic()
        wait = interval - (now - _API_REQUEST_TIMES.get(source, 0.0))
        if wait > 0:
            time.sleep(wait)
        _API_REQUEST_TIMES[source] = time.monotonic()


def _search_query(scene: dict[str, Any], avoid: list[str], visual_style: str) -> str:
    avoid_words = {
        word
        for term in avoid
        for word in _query_terms(term, limit=20)
    }
    terms = _query_terms(scene.get("search_query") or scene["text"])
    terms = [
        word for word in terms
        if not any(term in word or word in term for term in avoid_words)
    ]
    if not terms:
        raise FootageProviderError(
            "empty_search_query",
            "Avoid terms removed all useful search terms for a scene.",
        )
    if not avoid_words.intersection(_query_terms(visual_style)):
        terms.extend(_query_terms(visual_style))
    return " ".join(dict.fromkeys(terms))


def _download_video(
    url: str, destination: Path, allowed_hosts: set[str], *, allow_subdomains: bool = False
) -> None:
    parsed = urlparse(url)
    def allowed(host: str | None) -> bool:
        return bool(host) and any(
            host == allowed_host or (allow_subdomains and host.endswith("." + allowed_host))
            for allowed_host in allowed_hosts
        )

    if parsed.scheme != "https" or not allowed(parsed.hostname):
        raise FootageProviderError(
            "unsafe_media_url",
            "The footage provider returned an unsupported download host.",
        )
    total = 0
    try:
        with httpx.Client(timeout=PROVIDER_TIMEOUT_SECONDS, follow_redirects=False) as client:
            current_url = url
            downloaded = False
            for _ in range(6):
                current = urlparse(current_url)
                if current.scheme != "https" or not allowed(current.hostname):
                    raise FootageProviderError("unsafe_media_url", "The footage provider redirected to an unsupported host.")
                redirect_url = None
                with client.stream("GET", current_url) as response:
                    if getattr(response, "status_code", 200) in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise FootageProviderError("media_download_failed", "The footage source returned an invalid redirect.")
                        from urllib.parse import urljoin
                        redirect_url = urljoin(current_url, location)
                    else:
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if not content_type.startswith("video/"):
                            raise FootageProviderError(
                                "invalid_media_type",
                                "The footage download did not return a video content type.",
                            )
                        with destination.open("wb") as output:
                            for chunk in response.iter_bytes(64 * 1024):
                                total += len(chunk)
                                if total > MAX_VIDEO_BYTES:
                                    raise FootageProviderError(
                                        "media_too_large",
                                        "The downloaded video exceeds the configured size limit.",
                                    )
                                output.write(chunk)
                        downloaded = True
                        break
                if redirect_url:
                    current_url = redirect_url
                    continue
                break
            else:
                raise FootageProviderError("media_download_failed", "The footage source redirected too many times.")
            if not downloaded and not total:
                raise FootageProviderError("media_download_failed", "The footage source returned no file.")
    except FootageProviderError:
        destination.unlink(missing_ok=True)
        raise
    except httpx.HTTPStatusError as error:
        destination.unlink(missing_ok=True)
        raise FootageProviderError(
            "media_download_failed",
            f"The footage download returned HTTP {error.response.status_code}.",
        ) from error
    except httpx.HTTPError as error:
        destination.unlink(missing_ok=True)
        raise FootageProviderError(
            "media_download_failed", "The footage download failed."
        ) from error
    if total == 0:
        destination.unlink(missing_ok=True)
        raise FootageProviderError("empty_media", "The downloaded video was empty.")


class PexelsFootageProvider:
    name = "Pexels"
    mode = "production"
    matching_method = "deterministic-keyword-overlap"
    fallback_warnings: list[str] = []
    license_url = "https://www.pexels.com/license/"

    def search(
        self, scene: dict[str, Any], avoid: list[str], visual_style: str
    ) -> list[FootageAsset]:
        api_key = os.getenv("PEXELS_API_KEY", "").strip()
        if not api_key:
            raise FootageProviderError(
                "pexels_key_missing",
                "Set PEXELS_API_KEY in .env or choose BROLL_PROVIDER=demo.",
            )
        query = _search_query(scene, avoid, visual_style)
        try:
            response = httpx.get(
                "https://api.pexels.com/v1/videos/search",
                params={"query": query, "per_page": 15, "orientation": "landscape"},
                headers={"Authorization": api_key},
                timeout=float(os.getenv("PEXELS_TIMEOUT_SECONDS", str(PROVIDER_TIMEOUT_SECONDS))),
            )
        except httpx.HTTPError as error:
            raise FootageProviderError(
                "pexels_unavailable", "Pexels could not be reached."
            ) from error
        if response.status_code == 401:
            raise FootageProviderError(
                "pexels_auth_failed", "Pexels rejected the configured API key."
            )
        if response.status_code == 429:
            raise FootageProviderError(
                "pexels_rate_limited", "Pexels rate limit reached; retry the job later."
            )
        if response.status_code >= 400:
            raise FootageProviderError(
                "pexels_request_failed", "Pexels returned an error while searching for footage."
            )
        try:
            videos = response.json().get("videos", [])
        except (ValueError, AttributeError) as error:
            raise FootageProviderError(
                "pexels_invalid_response", "Pexels returned an invalid response."
            ) from error
        assets = []
        for rank, video in enumerate(videos):
            if not isinstance(video, dict):
                continue
            files = [
                item for item in video.get("video_files", [])
                if isinstance(item, dict)
                and item.get("link")
                and str(item.get("file_type") or "").startswith("video/")
            ]
            if not files or not video.get("id"):
                continue
            source = str(video.get("url", ""))
            creator = str((video.get("user") or {}).get("name") or "")
            selected = max(
                files,
                key=lambda item: (
                    int(item.get("width") or 0) * int(item.get("height") or 0),
                    int(item.get("width") or 0),
                ),
            )
            assets.append(
                FootageAsset(
                    asset_id=str(video["id"]),
                    provider=self.name,
                    source_url=source or None,
                    download_url=str(selected["link"]),
                    creator=creator or None,
                    width=selected.get("width"),
                    height=selected.get("height"),
                    matching_text=f"{source} {creator}",
                    provider_rank=rank,
                    license_name="Pexels License",
                    license_url=self.license_url,
                    attribution_required=(
                        "Pexels API guidelines request a prominent Pexels link and photographer "
                        "credit when possible; the Pexels license does not require attribution."
                    ),
                    source_description="Pexels API video result",
                    warnings=[],
                )
            )
        return assets

    def download(self, asset: FootageAsset, destination: Path) -> None:
        if not asset.download_url:
            raise FootageProviderError(
                "no_footage_found", "Pexels did not return a downloadable video."
            )
        _download_video(
            asset.download_url, destination, {"videos.pexels.com", "player.vimeo.com"}
        )


class PixabayFootageProvider:
    name = "Pixabay"
    mode = "production"
    matching_method = "deterministic-keyword-overlap"
    fallback_warnings: list[str] = []
    license_url = "https://pixabay.com/service/terms/"
    cache_lifetime_seconds = 24 * 60 * 60

    def search(
        self, scene: dict[str, Any], avoid: list[str], visual_style: str
    ) -> list[FootageAsset]:
        api_key = os.getenv("PIXABAY_API_KEY", "").strip()
        if not api_key:
            raise FootageProviderError(
                "pixabay_key_missing",
                "Set PIXABAY_API_KEY in .env or choose BROLL_PROVIDER=demo.",
            )
        query = _search_query(scene, avoid, visual_style)[:100]
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "provider": "pixabay-videos",
                    "query": query,
                    # Search results must not leak across independent script jobs.
                    "job_id": scene.get("job_id"),
                    "api_key_fingerprint": hashlib.sha256(api_key.encode()).hexdigest(),
                    "per_page": 20,
                    "safesearch": True,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        data = database.get_provider_cache(cache_key)
        if data is None:
            try:
                response = httpx.get(
                    "https://pixabay.com/api/videos/",
                    params={
                        "key": api_key,
                        "q": query,
                        "per_page": 20,
                        "safesearch": "true",
                    },
                    timeout=float(os.getenv("PIXABAY_TIMEOUT_SECONDS", str(PROVIDER_TIMEOUT_SECONDS))),
                )
            except httpx.HTTPError as error:
                raise FootageProviderError(
                    "pixabay_unavailable", "Pixabay could not be reached."
                ) from error
            if response.status_code in {401, 403}:
                raise FootageProviderError(
                    "pixabay_auth_failed", "Pixabay rejected the configured API key."
                )
            if response.status_code == 429:
                raise FootageProviderError(
                    "pixabay_rate_limited", "Pixabay rate limit reached; retry the job later."
                )
            if response.status_code >= 400:
                raise FootageProviderError(
                    "pixabay_request_failed", "Pixabay returned an error while searching for footage."
                )
            try:
                data = response.json()
            except ValueError as error:
                raise FootageProviderError(
                    "pixabay_invalid_response", "Pixabay returned an invalid response."
                ) from error
            database.set_provider_cache(cache_key, data, self.cache_lifetime_seconds)
        hits = data.get("hits", []) if isinstance(data, dict) else []
        assets = []
        for rank, hit in enumerate(hits):
            if not isinstance(hit, dict):
                continue
            variants = hit.get("videos") or {}
            files = [
                variant
                for variant in variants.values()
                if isinstance(variant, dict) and variant.get("url")
            ]
            if not files or not hit.get("id"):
                continue
            selected = max(
                files,
                key=lambda item: (
                    int(item.get("width") or 0) * int(item.get("height") or 0),
                    int(item.get("width") or 0),
                ),
            )
            source = str(hit.get("pageURL") or "")
            creator = str(hit.get("user") or "")
            tags = str(hit.get("tags") or "")
            assets.append(
                FootageAsset(
                    asset_id=str(hit["id"]),
                    provider=self.name,
                    source_url=source or None,
                    download_url=str(selected["url"]),
                    creator=creator or None,
                    width=selected.get("width"),
                    height=selected.get("height"),
                    matching_text=f"{tags} {source} {creator}",
                    provider_rank=rank,
                    license_name="Pixabay Content License",
                    license_url=self.license_url,
                    attribution_required=(
                        "Pixabay's Content License does not require contributor attribution; "
                        "review Pixabay API presentation guidance before displaying results."
                    ),
                    source_description=f"Pixabay video result tagged: {tags}",
                    warnings=[],
                )
            )
        return assets

    def download(self, asset: FootageAsset, destination: Path) -> None:
        if not asset.download_url:
            raise FootageProviderError(
                "no_footage_found", "Pixabay did not return a downloadable video."
            )
        _download_video(
            asset.download_url, destination, {"cdn.pixabay.com"}
        )


class LocalDemoFootageProvider:
    name = "Local media catalogue demo"
    mode = "demo"
    matching_method = "local-catalogue-keyword-overlap"
    fallback_warnings = [DEMO_WARNING]

    def search(
        self, scene: dict[str, Any], avoid: list[str], visual_style: str
    ) -> list[FootageAsset]:
        # This historical bundled sample contains black title cards and burned-in unrelated
        # text. It is intentionally not offered as footage; use relevant validated local
        # catalogue items or configure a licensed online provider.
        return []

    def download(self, asset: FootageAsset, destination: Path) -> None:
        if not DEMO_ASSET_PATH.is_file():
            raise FootageProviderError(
                "demo_asset_missing",
                "The bundled NASA demo video is missing from backend/assets/demo.",
            )
        try:
            shutil.copyfile(DEMO_ASSET_PATH, destination)
        except OSError as error:
            raise FootageProviderError(
                "demo_asset_failed", "The bundled NASA demo video could not be copied."
            ) from error


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))).strip()


def _wikimedia_user_agent() -> str:
    value = os.getenv("MEDIA_API_USER_AGENT", "").strip()
    contact = re.search(r"(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|https://[^\s)]+)", value, re.IGNORECASE)
    if not value or not contact:
        raise FootageProviderError(
            "commons_contact_required",
            "Wikimedia Commons requires a descriptive User-Agent with an operator contact. Set MEDIA_API_USER_AGENT in .env; this request was not sent.",
        )
    return value


def _license_metadata(name: str, url: str) -> tuple[str, str] | None:
    """Return an explicit use statement only for known permissive licenses."""
    normalized = url.lower().replace("http://", "https://").rstrip("/") + "/"
    by_versions = {f"https://creativecommons.org/licenses/by/{version}/" for version in ("1.0", "2.0", "2.5", "3.0", "4.0")}
    bysa_versions = {f"https://creativecommons.org/licenses/by-sa/{version}/" for version in ("1.0", "2.0", "2.5", "3.0", "4.0")}
    if normalized in by_versions:
        kind, version = "BY", normalized.rsplit("/", 2)[-2]
    elif normalized in bysa_versions:
        kind, version = "BY-SA", normalized.rsplit("/", 2)[-2]
    elif normalized == "https://creativecommons.org/publicdomain/zero/1.0/":
        kind, version = "CC0", "1.0"
    elif normalized == "https://creativecommons.org/publicdomain/mark/1.0/":
        kind, version = "Public Domain Mark", "1.0"
    else:
        return None
    canonical_name = f"CC {kind} {version}" if kind in {"BY", "BY-SA"} else f"{kind} {version}"
    clean_name = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    canonical_clean = re.sub(r"[^a-z0-9]+", " ", canonical_name.lower()).strip()
    accepted_names = {canonical_clean}
    if kind in {"BY", "BY-SA"}:
        accepted_names.add(re.sub(r"[^a-z0-9]+", " ", f"Creative Commons Attribution{' ShareAlike' if kind == 'BY-SA' else ''} {version} International".lower()).strip())
    if clean_name and clean_name not in accepted_names:
        return None
    if kind == "BY":
        attribution = "Attribution required: credit the named creator and link this licence and source page."
    elif kind == "BY-SA":
        attribution = "Attribution and share-alike required: credit the creator and apply the same licence to adaptations."
    elif kind == "CC0":
        attribution = "No attribution required by CC0; source and creator are recorded for provenance."
    else:
        attribution = "No attribution required by the Public Domain Mark; source and creator are recorded for provenance."
    return canonical_name, attribution


def _license_name_for_archive(name: str, url: str) -> str:
    return (_license_metadata(name.strip(), url) or ("", ""))[0]


class WikimediaCommonsProvider:
    name = "Wikimedia Commons"

    def __init__(self):
        self.last_query = ""
        self.blocked_reason: tuple[str, str] | None = None

    def search(self, query: str, limit: int = 5) -> list[FootageAsset]:
        if self.blocked_reason:
            raise FootageProviderError(*self.blocked_reason)
        self.last_query = query
        try:
            try:
                user_agent = _wikimedia_user_agent()
            except FootageProviderError as error:
                self.blocked_reason = (error.code, error.message)
                raise
            _pace_public_api("wikimedia-commons", 1.0)
            response = httpx.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query", "format": "json", "formatversion": 2,
                    "generator": "search", "gsrsearch": f"filetype:video {query}",
                    "gsrnamespace": 6, "gsrlimit": min(limit, 5),
                    "prop": "info|imageinfo", "inprop": "url", "iiprop": "url|extmetadata|size|mime",
                    "iiextmetadataversion": "latest", "maxlag": 5,
                },
                headers={"User-Agent": user_agent},
                timeout=float(os.getenv("MEDIA_API_TIMEOUT_SECONDS", str(PROVIDER_TIMEOUT_SECONDS))),
            )
            if response.status_code == 429:
                self.blocked_reason = ("commons_rate_limited", "Wikimedia Commons rate limit reached; further requests from this job were paused.")
                raise FootageProviderError("commons_rate_limited", "Wikimedia Commons rate limit reached; retry later.")
            if response.status_code == 503:
                raise FootageProviderError("commons_unavailable", "Wikimedia Commons is temporarily unavailable.")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 403:
                self.blocked_reason = ("commons_access_denied", "Wikimedia Commons denied this request (HTTP 403); further Commons requests for this job were paused. Check that MEDIA_API_USER_AGENT identifies the application and includes a real operator contact, then review Commons access policy before retrying.")
                raise FootageProviderError(*self.blocked_reason) from error
            if error.response.status_code == 429:
                self.blocked_reason = ("commons_rate_limited", "Wikimedia Commons rate limit reached; further requests from this job were paused.")
                raise FootageProviderError(*self.blocked_reason) from error
            raise FootageProviderError("commons_unavailable", f"Wikimedia Commons returned HTTP {error.response.status_code}.") from error
        except httpx.TimeoutException as error:
            raise FootageProviderError("commons_timeout", "Wikimedia Commons search timed out.") from error
        except (httpx.HTTPError, ValueError) as error:
            raise FootageProviderError("commons_unavailable", "Wikimedia Commons search failed or returned invalid data.") from error
        if not isinstance(data, dict) or data.get("error"):
            raise FootageProviderError("commons_invalid_response", "Wikimedia Commons returned an API error or invalid response.")
        pages = data.get("query", {}).get("pages", [])
        if isinstance(pages, dict):
            pages = list(pages.values())
        assets = []
        for rank, page in enumerate(pages[:limit] if isinstance(pages, list) else []):
            if not isinstance(page, dict):
                continue
            info = (page.get("imageinfo") or [{}])[0]
            if not isinstance(info, dict) or not str(info.get("mime", "")).startswith("video/"):
                continue
            metadata = info.get("extmetadata") or {}
            creator = _plain(metadata.get("Artist"))
            license_name = _plain(metadata.get("LicenseShortName"))
            license_url = _plain(metadata.get("LicenseUrl"))
            rights = _license_metadata(license_name, license_url)
            required = _plain(metadata.get("AttributionRequired")).lower()
            # Reject absent or contradictory license/attribution data instead of guessing.
            if not (creator and rights and license_name and license_url and required in {"true", "false"}):
                continue
            if required == "true" and "Attribution required" not in rights[1]:
                attribution = rights[1] + " The file metadata also requests attribution."
            elif required == "false" and "Attribution required" in rights[1]:
                continue
            else:
                attribution = rights[1]
            source = str(page.get("fullurl") or "")
            description = " ".join(filter(None, (
                str(page.get("title") or "").removeprefix("File:"),
                _plain(metadata.get("ObjectName")), _plain(metadata.get("ImageDescription")),
                _plain(metadata.get("UsageTerms")),
            )))
            assets.append(FootageAsset(
                asset_id=str(page.get("title") or info.get("url") or ""), provider=self.name,
                source_url=source or None, download_url=info.get("url"), creator=creator,
                width=info.get("width"), height=info.get("height"), matching_text=description,
                provider_rank=rank, license_name=rights[0], license_url=license_url.replace("http://", "https://", 1),
                attribution_required=attribution, source_description=description, warnings=[],
            ))
        return assets


class InternetArchiveProvider:
    name = "Internet Archive"

    def __init__(self):
        self.last_query = ""
        self.blocked_reason: tuple[str, str] | None = None

    def search(self, query: str, limit: int = 5) -> list[FootageAsset]:
        if self.blocked_reason:
            raise FootageProviderError(*self.blocked_reason)
        self.last_query = query
        terms = _query_terms(query, limit=6)
        if not terms:
            return []
        expression = " AND ".join(f'(title:"{term}" OR description:"{term}" OR subject:"{term}")' for term in terms[:5])
        try:
            _pace_public_api("internet-archive", 0.5)
            response = httpx.get(
                "https://archive.org/advancedsearch.php",
                params={"q": f"mediatype:movies AND licenseurl:* AND creator:* AND {expression}", "fl[]": ["identifier", "title", "creator", "licenseurl", "mediatype", "description", "subject"], "rows": min(limit, 5), "page": 1, "output": "json"},
                headers={"User-Agent": os.getenv("MEDIA_API_USER_AGENT", "").strip() or "IncubixScriptToVideo/1.0 (Internet Archive API client)"},
                timeout=float(os.getenv("MEDIA_API_TIMEOUT_SECONDS", str(PROVIDER_TIMEOUT_SECONDS))),
            )
            if response.status_code == 429:
                self.blocked_reason = ("archive_rate_limited", "Internet Archive rate limit reached; further requests from this job were paused.")
                raise FootageProviderError("archive_rate_limited", "Internet Archive rate limit reached; retry later.")
            if response.status_code == 503:
                raise FootageProviderError("archive_unavailable", "Internet Archive is temporarily unavailable.")
            response.raise_for_status()
            search_data = response.json()
            docs = search_data.get("response", {}).get("docs")
            if not isinstance(docs, list):
                raise ValueError("missing docs")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 403:
                self.blocked_reason = ("archive_access_denied", "Internet Archive denied this request (HTTP 403); further requests from this job were paused.")
                raise FootageProviderError(*self.blocked_reason) from error
            if error.response.status_code == 429:
                self.blocked_reason = ("archive_rate_limited", "Internet Archive rate limit reached; further requests from this job were paused.")
                raise FootageProviderError(*self.blocked_reason) from error
            raise FootageProviderError("archive_unavailable", f"Internet Archive returned HTTP {error.response.status_code}.") from error
        except httpx.TimeoutException as error:
            raise FootageProviderError("archive_timeout", "Internet Archive search timed out.") from error
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            raise FootageProviderError("archive_unavailable", "Internet Archive search failed or returned invalid data.") from error
        assets = []
        for rank, doc in enumerate(docs[:limit] if isinstance(docs, list) else []):
            if not isinstance(doc, dict) or not doc.get("identifier"):
                continue
            identifier = str(doc["identifier"])
            try:
                _pace_public_api("internet-archive", 0.5)
                meta_response = httpx.get(
                    f"https://archive.org/metadata/{quote(identifier, safe='')}",
                    headers={"User-Agent": os.getenv("MEDIA_API_USER_AGENT", "").strip() or "IncubixScriptToVideo/1.0 (Internet Archive API client)"},
                    timeout=float(os.getenv("MEDIA_API_TIMEOUT_SECONDS", str(PROVIDER_TIMEOUT_SECONDS))),
                )
                meta_response.raise_for_status()
                metadata = meta_response.json()
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {403, 429}:
                    code = "archive_access_denied" if error.response.status_code == 403 else "archive_rate_limited"
                    message = f"Internet Archive returned HTTP {error.response.status_code} during item metadata lookup; further requests from this job were paused."
                    self.blocked_reason = (code, message)
                    raise FootageProviderError(*self.blocked_reason) from error
                continue
            except (httpx.HTTPError, ValueError) as error:
                continue
            fields = metadata.get("metadata") or {}
            license_url = str(fields.get("licenseurl") or doc.get("licenseurl") or "").strip()
            license_name = _license_name_for_archive(str(fields.get("license") or ""), license_url)
            rights = _license_metadata(license_name, license_url)
            creator_value = fields.get("creator") or doc.get("creator")
            creator = "; ".join(_plain(x) for x in creator_value if _plain(x)) if isinstance(creator_value, list) else _plain(creator_value)
            if not (rights and creator and str(fields.get("mediatype") or doc.get("mediatype")) == "movies"):
                continue
            files = metadata.get("files") or []
            for file_info in files:
                if not isinstance(file_info, dict):
                    continue
                filename = str(file_info.get("name") or "")
                fmt = str(file_info.get("format") or "").lower()
                if Path(filename).suffix.lower() not in {".mp4", ".m4v", ".mov", ".ogv", ".webm"}:
                    continue
                if fmt and not any(tag in fmt for tag in ("mpeg4", "h.264", "h264", "ogg video", "webm", "quicktime")):
                    continue
                title = str(fields.get("title") or doc.get("title") or "")
                desc = _plain(fields.get("description") or doc.get("description"))
                subject = fields.get("subject") or doc.get("subject") or []
                subjects = " ".join(subject) if isinstance(subject, list) else str(subject)
                description = " ".join(filter(None, (title, desc, subjects)))
                # Subject tags are useful for catalogue retrieval, but too broad
                # to verify that a clip actually depicts the queried scene.
                relevance_text = " ".join(filter(None, (title, desc)))
                assets.append(FootageAsset(
                    asset_id=f"{identifier}/{filename}", provider=self.name,
                    source_url=f"https://archive.org/details/{quote(identifier, safe='')}",
                    download_url=f"https://archive.org/download/{quote(identifier, safe='')}/{quote(filename, safe='')}",
                    creator=creator, width=None, height=None, matching_text=relevance_text,
                    provider_rank=rank, license_name=license_name, license_url=license_url.replace("http://", "https://", 1),
                    attribution_required=rights[1], source_description=description, warnings=[],
                ))
                break
        return assets


class PublicFootageProvider:
    name = "Wikimedia Commons and Internet Archive"
    mode = "public"
    matching_method = "source-metadata-keyword-overlap"

    def __init__(self):
        self.sources = [WikimediaCommonsProvider(), InternetArchiveProvider()]
        if os.getenv("PIXABAY_API_KEY", "").strip():
            self.sources.append(PixabayFootageProvider())
        if os.getenv("PEXELS_API_KEY", "").strip():
            self.sources.append(PexelsFootageProvider())
        self.fallback_warnings: list[str] = []

    def search(self, scene: dict[str, Any], avoid: list[str], visual_style: str) -> list[FootageAsset]:
        query = _search_query(scene, avoid, "")
        results: list[FootageAsset] = []
        failures = []
        had_errors = False
        for source in self.sources:
            try:
                if isinstance(source, (WikimediaCommonsProvider, InternetArchiveProvider)):
                    source_results = source.search(query, limit=3)
                else:
                    source_results = source.search({**scene, "search_query": query}, avoid, visual_style)
                if source_results:
                    results.extend(source_results)
                else:
                    failures.append(f"{source.name}: no eligible rights-documented video candidates")
            except FootageProviderError as error:
                had_errors = True
                failures.append(f"{source.name}: {error.message}")
        self.fallback_warnings = failures
        if not results and had_errors:
            raise FootageProviderError("public_sources_unavailable", "; ".join(failures))
        return results

    def download(self, asset: FootageAsset, destination: Path) -> None:
        if asset.provider == "Wikimedia Commons":
            host = "upload.wikimedia.org"
            subdomains = False
        elif asset.provider == "Internet Archive":
            host = "archive.org"
            subdomains = True
        elif asset.provider == "Pexels":
            PexelsFootageProvider().download(asset, destination)
            return
        elif asset.provider == "Pixabay":
            PixabayFootageProvider().download(asset, destination)
            return
        else:
            raise FootageProviderError("unknown_source", "The selected public source is unsupported.")
        if not asset.download_url:
            raise FootageProviderError("no_download", "The public source did not provide a downloadable video URL.")
        _download_video(asset.download_url, destination, {host}, allow_subdomains=subdomains)


def get_footage_provider() -> FootageProvider:
    selected = os.getenv("BROLL_PROVIDER", "auto").strip().lower()
    if selected == "auto":
        selected = "public"
    providers: dict[str, type[FootageProvider]] = {
        "demo": LocalDemoFootageProvider,
        "pexels": PexelsFootageProvider,
        "pixabay": PixabayFootageProvider,
        "public": PublicFootageProvider,
    }
    provider = providers.get(selected)
    if provider is None:
        raise FootageProviderError(
            "invalid_footage_provider",
            "BROLL_PROVIDER must be one of: auto, public, demo, pexels, pixabay.",
        )
    return provider()


def provider_configuration() -> dict[str, str]:
    selected = os.getenv("BROLL_PROVIDER", "auto").strip().lower()
    if selected == "auto":
        return {"selected": "auto", "active": "public", "credentials": "not_required"}
    if selected == "demo":
        credential_status = "not_required"
    elif selected == "pexels":
        credential_status = "configured" if os.getenv("PEXELS_API_KEY", "").strip() else "missing"
    elif selected == "pixabay":
        credential_status = "configured" if os.getenv("PIXABAY_API_KEY", "").strip() else "missing"
    elif selected == "public":
        credential_status = "not_required"
    else:
        credential_status = "invalid_provider"
    return {"selected": selected or "auto", "active": selected or "auto", "credentials": credential_status}
