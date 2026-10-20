from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sys
from dataclasses import replace

import pytest

from backend.app import database
from backend.app.models import JobInput
from backend.app import processing
from backend.app.processing import (
    JobProcessingError,
    _assert_public_https_url,
    allocate_scene_timing,
    make_srt,
    query_terms,
    rank_score,
    split_script,
)
from backend.app.providers import (
    FootageAsset,
    FootageProviderError,
    PexelsFootageProvider,
    PixabayFootageProvider,
    PublicFootageProvider,
    get_footage_provider,
)
from backend.app import providers


def test_script_splitting_preserves_sentences_and_rejects_empty_text():
    scenes = split_script("A green valley opens. Birds fly above it!\nThen the sun sets.")
    assert [scene["text"] for scene in scenes] == [
        "A green valley opens.",
        "Birds fly above it!",
        "Then the sun sets.",
    ]
    assert [scene["scene_id"] for scene in scenes] == ["scene-001", "scene-002", "scene-003"]


def test_pasted_script_splits_adjacent_sentences_without_reusing_visual_words():
    combined = (
        "A doctor discusses medical information with a patient. "
        "A doctor provides clinical care with a stethoscope. "
        "An ECG display shows heart monitoring. "
        "Surgeons work together in an operating room. "
        "Medical professionals wash their hands."
        "Imaging helps doctors detect and diagnose diseases."
    )
    scenes = split_script(combined)

    assert len(scenes) == 6
    assert scenes[4]["text"] == "Medical professionals wash their hands."
    assert scenes[5]["text"] == "Imaging helps doctors detect and diagnose diseases."
    assert "imaging" not in scenes[4]["search_query"].split()
    assert "hand" not in scenes[5]["search_query"].split()
    assert "hands" not in scenes[5]["search_query"].split()


def test_local_catalogue_fixtures_match_working_script_and_isolate_other_topics():
    working_script = (
        "A doctor discusses medical information with a patient. "
        "A doctor provides clinical care with a stethoscope. "
        "An ECG display shows heart monitoring. "
        "Surgeons work together in an operating room. "
        "Medical professionals wash their hands."
    )
    expected = [
        "doctor-patient-medical-consultation.mp4",
        "doctor-stethoscope-clinical-examination.mp4",
        "ecg-heart-monitor-display.mp4",
        "surgeons-operating-room-care.mp4",
        "medical-professionals-handwashing.mp4",
    ]
    used_ids = set()
    matched_paths = []
    for scene in split_script(working_script):
        match = next(
            (candidate for query in processing.scene_search_queries(scene)
             if (candidate := processing.media_library.match_scene(query, used_ids))),
            None,
        )
        assert match is not None, scene["scene_id"]
        used_ids.add(match["id"])
        matched_paths.append(match["path"])
        assert match["usage_rights_confirmed"] is True
    assert matched_paths == expected

    imaging_scene = split_script("Imaging helps doctors detect and diagnose diseases.")[0]
    assert all("hands" not in query and "wash" not in query for query in processing.scene_search_queries(imaging_scene))
    assert not any(
        processing.media_library.match_scene(query, used_ids)
        for query in processing.scene_search_queries(imaging_scene)
    )

    education_scene = split_script("Students learn fractions with colored shapes.")[0]
    assert all("medical" not in query and "doctor" not in query for query in processing.scene_search_queries(education_scene))
    assert not any(
        processing.media_library.match_scene(query, set())
        for query in processing.scene_search_queries(education_scene)
    )


@pytest.mark.parametrize(
    "script",
    [
        "Imaging helps doctors detect and diagnose diseases using X-rays, CT scans, MRI, and ultrasound.",
        "Students learn fractions with colored shapes.",
        "A satellite travels beyond Earth's orbit.",
    ],
)
def test_empty_public_sources_and_catalogue_fail_honestly_without_output(script, tmp_path, monkeypatch):
    monkeypatch.setenv("BROLL_PROVIDER", "auto")
    monkeypatch.setenv("FOOTAGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.setattr(providers.WikimediaCommonsProvider, "search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(providers.InternetArchiveProvider, "search", lambda *_args, **_kwargs: [])
    monkeypatch.setenv(
        "MEDIA_LIBRARY_DIR",
        str(Path(__file__).resolve().parents[2] / "data" / "media_library"),
    )
    scene = split_script(script)[0]

    with pytest.raises(JobProcessingError) as error:
        processing.select_footage(
            scene, [], set(), tmp_path, "documentary", get_footage_provider(),
            job_id=f"isolated-{scene['scene_id']}",
        )

    assert scene["scene_id"] in error.value.message
    assert "provider returned no candidates" in error.value.message
    assert not list(tmp_path.glob("*.mp4"))


def test_script_title_is_removed_and_scenes_and_queries_come_from_the_script():
    scenes = split_script("**Title: The Future of Smart Healthcare**\n\nTelemedicine allows people to consult doctors remotely.\n\nWearable devices track heart rate.")
    assert [scene["text"] for scene in scenes] == ["Telemedicine allows people to consult doctors remotely.", "Wearable devices track heart rate."]
    assert "telemedicine" in scenes[0]["search_query"]
    assert "consult" in scenes[0]["search_query"]
    assert "doctors remotely" in scenes[0]["visual_description"]
    assert "wearable" in scenes[1]["search_query"]
    assert "heart" in scenes[1]["search_query"]


def test_different_topic_does_not_receive_a_healthcare_scene_or_query():
    scene = split_script("A technology team tests a new satellite communications network.")[0]
    assert "technology" in scene["search_query"]
    assert "satellite" in scene["search_query"]
    assert "network" in scene["search_query"]
    assert "healthcare" not in scene["search_query"]
    assert "medical" not in scene["visual_description"].lower()


def test_visual_fallback_queries_never_merge_broll_prompt_with_narration():
    scene = {
        "scene_id": "scene-005",
        "text": "Medical professionals wash hands before patient procedures.",
        "visual_description": "Medical professionals washing their hands",
        "search_query": "medical professionals washing their hands",
    }
    queries = processing.scene_search_queries(scene)

    assert queries[0] == "medical professionals washing hands"
    assert all("imaging" not in query and "radiologists" not in query and "abnormalities" not in query for query in queries)


def test_local_catalogue_matching_uses_visual_query_not_narration(tmp_path, monkeypatch):
    received = []
    clip_path = tmp_path / "reviewed-handwashing.mp4"
    clip_path.write_bytes(b"reviewed local video")

    def match_scene(query, used_ids):
        received.append(query)
        assert used_ids == set()
        return {
            "id": "reviewed-handwashing",
            "path": clip_path.name,
            "provider": "Pexels",
            "source_url": "https://www.pexels.com/video/reviewed-handwashing/",
            "creator": "Verified creator",
            "caption": "Medical professionals washing their hands",
            "license_name": "Pexels License",
            "license_url": "https://www.pexels.com/license/",
            "attribution_required": "Credit creator when practical",
            "relevance_score": 0.9,
            "validation": {"width": 1280, "height": 720},
        }

    monkeypatch.setattr(processing.media_library, "match_scene", match_scene)
    monkeypatch.setattr(processing.media_library, "root", lambda: tmp_path)
    class EmptyPublicProvider:
        mode = "public"
        matching_method = "source-metadata-keyword-overlap"
        fallback_warnings = []
        @staticmethod
        def search(*_args): return []
    monkeypatch.setenv("FOOTAGE_CACHE_DIR", str(tmp_path / "cache"))
    scene = {
        "scene_id": "scene-005",
        "text": "Medical imaging helps radiologists identify abnormalities.",
        "visual_description": "Medical professionals washing their hands",
        "search_query": "medical professionals washing their hands",
    }
    result = processing.select_footage(scene, [], set(), tmp_path, "documentary", provider=EmptyPublicProvider())

    assert received == ["medical professionals washing hands"]
    assert result["clip"]["asset_id"] == "reviewed-handwashing"


def test_scene_timing_covers_audio_without_gaps_or_overlaps():
    scenes = split_script("Short line. A much longer spoken scene follows.")
    timed = allocate_scene_timing(scenes, 12.345)
    assert timed[0]["start_time"] == 0
    assert timed[-1]["end_time"] == 12.345
    assert all(left["end_time"] == right["start_time"] for left, right in zip(timed, timed[1:]))
    assert all(scene["duration_seconds"] > 0 for scene in timed)


def test_caption_timestamps_are_valid_and_contiguous():
    scenes = allocate_scene_timing(split_script("A short scene. A second scene."), 3.0)
    srt = make_srt(scenes)
    assert "00:00:00,000 --> " in srt
    assert " --> 00:00:03,000" in srt
    assert "\n1\n" in f"\n{srt}"
    assert "\n2\n" in srt


def test_caption_lines_are_wrapped_and_timestamps_do_not_overlap():
    scene = {
        "text": "A thoughtful story can turn an ordinary moment into something people remember and share.",
        "start_time": 0.0,
        "end_time": 5.0,
    }
    blocks = make_srt([scene]).strip().split("\n\n")
    parsed = []
    for block in blocks:
        _, timing, *lines = block.splitlines()
        start, end = timing.split(" --> ")
        parsed.append((start, end))
        assert len(lines) <= 2
        assert all(len(line) <= 42 for line in lines)
    assert parsed[0][0] == "00:00:00,000"
    assert parsed[-1][1] == "00:00:05,000"
    assert all(previous[1] == current[0] for previous, current in zip(parsed, parsed[1:]))


def test_long_script_sentence_is_broken_into_reasonable_scenes():
    scenes = split_script("story " * 700)
    assert len(scenes) > 1
    assert all(len(scene["text"]) <= 600 for scene in scenes)


def test_keyword_matching_is_deterministic():
    assert query_terms("The bright green valley") == ["bright", "green", "valley"]
    assert rank_score("green valley sunrise", "a green valley at sunrise") == 1.0
    assert rank_score("green valley", "ocean waves") == 0.0
    assert rank_score("green valley sunrise", "https://pexels.com/video/green-valley-sunrise-123/") == 1.0


def test_audio_url_policy_requires_https_before_dns_lookup():
    with pytest.raises(JobProcessingError, match="HTTPS"):
        _assert_public_https_url("http://127.0.0.1/audio.wav")
    with pytest.raises(JobProcessingError, match="private"):
        _assert_public_https_url("https://127.0.0.1/audio.wav")


def test_footage_selection_skips_reused_assets_and_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(processing, "probe_video", lambda _path: {"width": 1920, "height": 1080})
    def asset(asset_id, source_url, rank, width):
        return FootageAsset(
            asset_id=asset_id,
            provider="Pexels",
            source_url=source_url,
            download_url=f"https://videos.pexels.com/asset-{asset_id}.mp4",
            creator="Creator",
            width=width,
            height=720,
            matching_text=source_url,
            provider_rank=rank,
            license_name="Pexels License",
            license_url="https://www.pexels.com/license/",
            attribution_required="Review Pexels license.",
            source_description="Pexels API result",
            warnings=[],
        )

    candidates = [
        asset("30", "https://www.pexels.com/video/green-valley-fire-30/", 0, 1920),
        asset("20", "https://www.pexels.com/video/green-valley-sunrise-20/", 1, 1280),
        asset("10", "https://www.pexels.com/video/green-valley-sunrise-10/", 2, 1920),
    ]

    class TestProvider:
        name = "Pexels"
        mode = "production"
        matching_method = "deterministic-keyword-overlap"
        fallback_warnings = []

        @staticmethod
        def search(*_args):
            return candidates

        @staticmethod
        def download(_asset, destination):
            destination.write_bytes(b"test-fixture-video")

    scene = {
        "scene_id": "scene-001",
        "text": "A green valley at sunrise",
        "duration_seconds": 2.0,
    }

    result = processing.select_footage(
        scene, ["fire"], {"20"}, tmp_path, "documentary", TestProvider()
    )

    assert result["clip"]["asset_id"] == "10"
    assert result["clip"]["provider"] == "Pexels"
    assert result["clip"]["license_url"] == "https://www.pexels.com/license/"
    assert result["clip"]["matching_method"] == "deterministic-keyword-overlap"
    assert Path(result["clip"]["file_path"]).is_file()


def test_missing_provider_keys_use_public_sources_without_credentials(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.delenv("BROLL_PROVIDER", raising=False)
    provider = get_footage_provider()
    assert isinstance(provider, PublicFootageProvider)
    assert provider.mode == "public"

    with pytest.raises(FootageProviderError, match="PEXELS_API_KEY"):
        PexelsFootageProvider().search(
            {"text": "A forest near the lake"}, [], "documentary"
        )
    with pytest.raises(FootageProviderError, match="PIXABAY_API_KEY"):
        PixabayFootageProvider().search(
            {"text": "A forest near the lake"}, [], "documentary"
        )


def test_auto_provider_keeps_public_sources_and_reports_no_key_requirement(monkeypatch):
    monkeypatch.setenv("BROLL_PROVIDER", "auto")
    monkeypatch.setenv("PIXABAY_API_KEY", "test-only")
    monkeypatch.setenv("PEXELS_API_KEY", "another-test-key")
    assert isinstance(get_footage_provider(), PublicFootageProvider)
    assert providers.provider_configuration() == {
        "selected": "auto", "active": "public", "credentials": "not_required"
    }

    monkeypatch.delenv("PIXABAY_API_KEY")
    monkeypatch.delenv("PEXELS_API_KEY")
    assert get_footage_provider().mode == "public"
    assert providers.provider_configuration() == {
        "selected": "auto", "active": "public", "credentials": "not_required"
    }


def test_pexels_provider_failure_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-only")

    def unavailable(*_args, **_kwargs):
        raise processing.httpx.ConnectError("offline")

    monkeypatch.setattr(processing.httpx, "get", unavailable)
    with pytest.raises(FootageProviderError) as error:
        PexelsFootageProvider().search(
            {"text": "A forest near the lake"}, [], "documentary"
        )
    assert error.value.code == "pexels_unavailable"


def test_pexels_query_uses_scene_terms_and_filters_avoid_terms(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-only")
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"videos": []}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("backend.app.providers.httpx.get", fake_get)
    PexelsFootageProvider().search(
        {"text": "A forest fire near the lake", "search_query": "forest fire lake"},
        ["fire"],
        "documentary",
    )

    assert captured["params"]["query"] == "forest lake documentary"
    assert captured["headers"]["Authorization"] == "test-only"
    assert captured["url"] == "https://api.pexels.com/v1/videos/search"


def test_pixabay_provider_failure_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-only")
    monkeypatch.setattr("backend.app.providers.database.get_provider_cache", lambda *_args: None)

    class Response:
        status_code = 503

    monkeypatch.setattr("backend.app.providers.httpx.get", lambda *_args, **_kwargs: Response())
    with pytest.raises(FootageProviderError) as error:
        PixabayFootageProvider().search(
            {"text": "A forest near the lake"}, [], "documentary"
        )
    assert error.value.code == "pixabay_request_failed"


def test_pixabay_empty_video_search_is_reported_as_empty(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-only")
    monkeypatch.setattr("backend.app.providers.database.get_provider_cache", lambda *_args: None)
    monkeypatch.setattr("backend.app.providers.database.set_provider_cache", lambda *_args: None)

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"hits": [], "totalHits": 0}

    monkeypatch.setattr("backend.app.providers.httpx.get", lambda *_args, **_kwargs: Response())
    assert PixabayFootageProvider().search(
        {"text": "An imaginary creature in orbit", "search_query": "imaginary creature orbit"},
        [], "documentary",
    ) == []


def test_pixabay_job_search_cache_is_scoped_to_each_generation(monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-only")
    cache_keys = []
    monkeypatch.setattr("backend.app.providers.database.get_provider_cache", lambda *_args: None)
    monkeypatch.setattr(
        "backend.app.providers.database.set_provider_cache",
        lambda key, *_args: cache_keys.append(key),
    )

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"hits": []}

    monkeypatch.setattr("backend.app.providers.httpx.get", lambda *_args, **_kwargs: Response())
    provider = PixabayFootageProvider()
    for job_id in ("generation-one", "generation-two"):
        provider.search(
            {"text": "Satellite communications network", "job_id": job_id},
            [],
            "documentary",
        )
    assert len(cache_keys) == 2
    assert cache_keys[0] != cache_keys[1]


def test_demo_provider_rejects_historical_black_title_card_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("BROLL_PROVIDER", "demo")
    provider = get_footage_provider()
    assets = provider.search({"scene_id": "scene-001", "text": "Earth from orbit"}, [], "documentary")
    assert provider.mode == "demo"
    assert provider.name == "Local media catalogue demo"
    assert assets == []


def test_unrelated_provider_candidate_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)
    class UnrelatedProvider:
        name = "fixture"
        mode = "production"
        matching_method = "test"
        fallback_warnings = []
        def search(self, *_args):
            return [FootageAsset("irrelevant", "fixture", None, "https://example.test/x.mp4", None, 640, 360, "ocean coral reef", 0, "test", None, "", "", [])]
        def download(self, _asset, destination):
            destination.write_bytes(b"would be downloaded")
    with pytest.raises(JobProcessingError, match="No sufficiently relevant"):
        processing.select_footage({"scene_id": "scene-001", "text": "Telemedicine doctor video consultation", "search_query": "telehealth doctor patient"}, [], set(), tmp_path, "documentary", UnrelatedProvider())


def _asset(asset_id: str, matching_text: str) -> FootageAsset:
    return FootageAsset(
        asset_id=asset_id,
        provider="Pexels",
        source_url=f"https://www.pexels.com/video/{asset_id}/",
        download_url=f"https://videos.pexels.com/{asset_id}.mp4",
        creator="Verified creator",
        width=1280,
        height=720,
        matching_text=matching_text,
        provider_rank=0,
        license_name="Pexels License",
        license_url="https://www.pexels.com/license/",
        attribution_required="Pexels attribution is not required; credit is appreciated.",
        source_description=matching_text,
        warnings=[],
    )


def _engineering_scene():
    return {
        "scene_id": "scene-002",
        "text": "The engineering team discusses bridge construction.",
        "visual_description": "Footage illustrating: The engineering team discusses bridge construction.",
        "search_query": "engineering bridge design",
    }


def test_search_tries_scene_derived_fallback_after_no_relevant_results(tmp_path, monkeypatch):
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)
    monkeypatch.setattr(processing, "probe_video", lambda _path: {"width": 1280, "height": 720})

    class FallbackProvider:
        name = "Pexels"
        mode = "production"
        matching_method = "deterministic-keyword-overlap"
        fallback_warnings = []

        def __init__(self):
            self.queries = []

        def search(self, scene, _avoid, _style):
            query = scene["search_query"]
            self.queries.append(query)
            if query == "engineering team discusses bridge construction":
                return [_asset("bridge-construction", "engineering team discusses bridge construction")]
            return [_asset("unrelated", "ocean wildlife reef")]

        @staticmethod
        def download(_asset, destination):
            destination.write_bytes(b"validated by mocked ffprobe")

    provider = FallbackProvider()
    scene = _engineering_scene()
    result = processing.select_footage(scene, [], set(), tmp_path, "documentary", provider)

    assert provider.queries == [
        "engineering bridge design",
        "engineering team discusses bridge construction",
    ]
    assert result["clip"]["asset_id"] == "bridge-construction"
    assert result["clip"]["search_queries_tried"] == provider.queries
    assert "validated" in Path(result["clip"]["file_path"]).read_bytes().decode()


def test_scene_search_falls_back_after_download_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)
    monkeypatch.setattr(processing, "probe_video", lambda _path: {"width": 1280, "height": 720})
    monkeypatch.setenv("FOOTAGE_CACHE_DIR", str(tmp_path / "cache"))

    class DownloadFallbackProvider:
        name = "Pexels"
        mode = "production"
        matching_method = "deterministic-keyword-overlap"
        fallback_warnings = []

        def __init__(self):
            self.queries = []

        def search(self, scene, _avoid, _style):
            query = scene["search_query"]
            self.queries.append(query)
            if query == "engineering bridge design":
                return [_asset("broken-download", "engineering team discusses bridge construction")]
            if query == "engineering team discusses bridge construction":
                return [_asset("valid-fallback", "engineering team discusses bridge construction")]
            return []

        @staticmethod
        def download(asset, destination):
            if asset.asset_id == "broken-download":
                raise FootageProviderError("media_download_failed", "HTTP 503 from footage CDN")
            destination.write_bytes(b"valid video fixture")

    provider = DownloadFallbackProvider()
    result = processing.select_footage(_engineering_scene(), [], set(), tmp_path, "documentary", provider)
    assert result["clip"]["asset_id"] == "valid-fallback"
    assert provider.queries[:2] == [
        "engineering bridge design",
        "engineering team discusses bridge construction",
    ]
    assert Path(result["clip"]["file_path"]).read_bytes() == b"valid video fixture"


def test_candidate_without_license_metadata_is_never_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)

    class MissingLicenseProvider:
        name = "Pexels"
        mode = "production"
        matching_method = "deterministic-keyword-overlap"
        fallback_warnings = []
        download_called = False

        @staticmethod
        def search(*_args):
            return [replace(_asset("missing-license", "engineering bridge construction"), license_name="")]

        def download(self, _asset, _destination):
            self.download_called = True

    provider = MissingLicenseProvider()
    with pytest.raises(JobProcessingError) as error:
        processing.select_footage(_engineering_scene(), [], set(), tmp_path, "documentary", provider)
    assert "missing or invalid rights metadata (license_name" in error.value.message
    assert provider.download_called is False
    assert not list(tmp_path.glob("*.mp4"))


def test_unreadable_download_is_rejected_by_ffprobe_before_fallback_assignment(tmp_path, monkeypatch):
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)
    monkeypatch.setenv("FOOTAGE_CACHE_DIR", str(tmp_path / "cache"))
    probe_calls = 0

    def probe(_path):
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            raise JobProcessingError("output_validation_failed", "FFprobe found no video stream.")
        return {"width": 1280, "height": 720}

    monkeypatch.setattr(processing, "probe_video", probe)

    class InvalidVideoProvider:
        name = "Pexels"
        mode = "production"
        matching_method = "deterministic-keyword-overlap"
        fallback_warnings = []

        def search(self, scene, _avoid, _style):
            query = scene["search_query"]
            if query == "engineering bridge design":
                return [_asset("html-disguised-as-mp4", "engineering team discusses bridge construction")]
            if query == "engineering team discusses bridge construction":
                return [_asset("valid-video", "engineering team discusses bridge construction")]
            return []

        @staticmethod
        def download(asset, destination):
            destination.write_bytes(b"not a playable video" if asset.asset_id == "html-disguised-as-mp4" else b"video bytes")

    result = processing.select_footage(
        _engineering_scene(), [], set(), tmp_path, "documentary", InvalidVideoProvider()
    )
    assert result["clip"]["asset_id"] == "valid-video"
    assert probe_calls == 2
    assert Path(result["clip"]["file_path"]).read_bytes() == b"video bytes"


def test_verified_provider_download_is_cached_after_first_use(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOTAGE_CACHE_DIR", str(tmp_path / "download-cache"))
    monkeypatch.setattr(processing, "probe_video", lambda _path: {"width": 1280, "height": 720})

    class Provider:
        calls = 0
        def download(self, _asset, destination):
            self.calls += 1
            destination.write_bytes(b"verified clip")

    asset = _asset("cache-asset", "spacecraft earth orbit")
    provider = Provider()
    first, _ = processing._get_verified_provider_file(provider, asset)
    second, _ = processing._get_verified_provider_file(provider, asset)
    assert first == second
    assert first.is_relative_to(tmp_path / "download-cache")
    assert provider.calls == 1
    assert first.read_bytes() == b"verified clip"


def test_exhausted_search_error_lists_scene_queries_and_failure_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)

    class FailingProvider:
        name = "Pexels"
        mode = "production"
        matching_method = "deterministic-keyword-overlap"
        fallback_warnings = []

        def search(self, scene, _avoid, _style):
            if scene["search_query"] == "engineering bridge design":
                return [_asset("broken-download", "engineering team discusses bridge construction")]
            return []

        @staticmethod
        def download(_asset, _destination):
            raise FootageProviderError("media_download_failed", "HTTP 503 from footage CDN")

    with pytest.raises(JobProcessingError) as error:
        processing.select_footage(_engineering_scene(), [], set(), tmp_path, "documentary", FailingProvider())
    message = error.value.message
    assert "HTTP 503 from footage CDN" in message
    assert "provider returned no candidates" in message
    for query in processing.scene_search_queries(_engineering_scene())[:3]:
        assert query in message


def test_provider_rejects_non_video_download_content_type(tmp_path, monkeypatch):
    class Response:
        headers = {"content-type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def raise_for_status():
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def stream(*_args, **_kwargs):
            return Response()

    monkeypatch.setattr(providers.httpx, "Client", Client)
    destination = tmp_path / "not-a-video.mp4"
    with pytest.raises(FootageProviderError) as error:
        providers._download_video("https://videos.pexels.com/test.mp4", destination, {"videos.pexels.com"})
    assert error.value.code == "invalid_media_type"
    assert not destination.exists()


def test_provider_reports_http_status_for_failed_video_download(tmp_path, monkeypatch):
    class Response:
        headers = {"content-type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def raise_for_status():
            request = providers.httpx.Request("GET", "https://videos.pexels.com/missing.mp4")
            response = providers.httpx.Response(503, request=request)
            raise providers.httpx.HTTPStatusError("service unavailable", request=request, response=response)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def stream(*_args, **_kwargs):
            return Response()

    monkeypatch.setattr(providers.httpx, "Client", Client)
    with pytest.raises(FootageProviderError, match="HTTP 503") as error:
        providers._download_video(
            "https://videos.pexels.com/missing.mp4", tmp_path / "missing.mp4", {"videos.pexels.com"}
        )
    assert error.value.code == "media_download_failed"


def _install_fake_generation(monkeypatch, tmp_path, candidate_factory=None, fail_search=False):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)
    database.initialize()
    searched = []

    class ScriptProvider:
        name = "Script test provider"
        mode = "production"
        matching_method = "test-keyword-overlap"
        fallback_warnings = []

        def search(self, scene, _avoid, _style):
            searched.append(scene["search_query"])
            if fail_search:
                return [_asset("unrelated", "unrelated ocean reef coral")]
            terms = " ".join(processing.query_terms(scene["text"], limit=30))
            return [candidate_factory(scene, terms) if candidate_factory else _asset(f"asset-{len(searched)}", terms)]

        @staticmethod
        def download(asset, destination):
            destination.write_bytes(asset.asset_id.encode("utf-8"))

    provider = ScriptProvider()
    monkeypatch.setattr(processing, "get_footage_provider", lambda: provider)
    def synthesize(script, path, _voice):
        path.write_bytes(script.encode())
        return "test-voice", "test-tts"

    monkeypatch.setattr(processing, "synthesize_speech", synthesize)
    monkeypatch.setattr(processing, "probe_media", lambda _path: {"duration_seconds": 4.0, "codec": "pcm_s16le"})
    monkeypatch.setattr(processing, "probe_video", lambda _path: {"width": 1280, "height": 720})

    def render(scenes, _audio, folder, ratio, _captions):
        output = folder / f"video_{ratio.replace(':', 'x')}.mp4"
        output.write_bytes(Path(scenes[0]["clip"]["file_path"]).read_bytes())
        return {"file": output.name, "width": 1920, "height": 1080, "duration_seconds": 4.0}

    monkeypatch.setattr(processing, "_render_ratio", render)
    return provider, searched


def _process_test_script(job_id, script, visual_source="footage"):
    data = {
        "script": script,
        "audio_url": None,
        "scenes": None,
        "aspect_ratios": ["16:9"],
        "captions_enabled": True,
        "voice": None,
        "visual_style": "documentary",
        "visual_source": visual_source,
        "avoid": [],
        "title": None,
    }
    database.create_job(job_id, f"hash-{job_id}", data, None)
    processing.process_job(job_id, data)
    return database.get_job(job_id)


def test_medical_imaging_script_renders_exactly_one_topic_matched_mp4(tmp_path, monkeypatch):
    provider, searched = _install_fake_generation(monkeypatch, tmp_path)
    job = _process_test_script("medical-imaging-job", "A radiologist reviews medical imaging from an MRI scan.")

    assert job["status"] == "completed"
    assert "radiologist" in searched[0] and "imaging" in searched[0] and "MRI".lower() in searched[0].lower()
    assert len(job["result"]["videos"]) == 1
    assert job["timeline"]["scenes"][0]["text"] == "A radiologist reviews medical imaging from an MRI scan."
    assert (tmp_path / "outputs" / job["job_id"] / job["result"]["videos"][0]["file"]).read_bytes().startswith(b"asset-")


def test_technology_script_searches_only_its_submitted_topic(tmp_path, monkeypatch):
    _, searched = _install_fake_generation(monkeypatch, tmp_path)
    job = _process_test_script("technology-job", "Engineers test a satellite communications network.")

    assert job["status"] == "completed"
    assert searched
    assert all("healthcare" not in query and "medical" not in query for query in searched)
    assert any("satellite" in query and "network" in query for query in searched)
    assert job["timeline"]["scenes"][0]["text"] == "Engineers test a satellite communications network."
    output_folder = tmp_path / "outputs" / job["job_id"]
    rights = json.loads((output_folder / job["result"]["rights_report"]).read_text(encoding="utf-8"))
    manifest = json.loads((output_folder / job["result"]["manifest"]).read_text(encoding="utf-8"))
    clip = job["timeline"]["scenes"][0]["clip"]
    assert [item["media_id"] for item in rights["items"]] == [clip["asset_id"]]
    assert rights["rights_approved"] == 0
    assert rights["rights_review_required"] == 1
    assert manifest["rights_summary"]["media_items"] == 1


def test_new_scripts_use_separate_scenes_assets_and_output_folders(tmp_path, monkeypatch):
    _install_fake_generation(monkeypatch, tmp_path)
    first = _process_test_script("first-independent-job", "A camera records ocean waves.")
    second = _process_test_script("second-independent-job", "A satellite orbits Earth.")

    assert first["job_id"] != second["job_id"]
    assert first["timeline"]["scenes"][0]["text"] == "A camera records ocean waves."
    assert second["timeline"]["scenes"][0]["text"] == "A satellite orbits Earth."
    first_folder = tmp_path / "outputs" / first["job_id"]
    second_folder = tmp_path / "outputs" / second["job_id"]
    assert first_folder != second_folder
    assert len(first["result"]["videos"]) == len(second["result"]["videos"]) == 1
    assert first_folder.joinpath(first["result"]["videos"][0]["file"]).read_bytes() != second_folder.joinpath(second["result"]["videos"][0]["file"]).read_bytes()


def test_unavailable_scene_fails_without_creating_a_fake_mp4(tmp_path, monkeypatch):
    _install_fake_generation(monkeypatch, tmp_path, fail_search=True)
    monkeypatch.setattr(processing.media_library, "match_scene", lambda *_args: None)
    job = _process_test_script("unavailable-scene-job", "An astronaut explores Europa's ice ocean.")

    assert job["status"] == "failed"
    assert job["result"] is None
    assert "europa" in job["error"]["message"].lower()
    assert "astronaut" in job["error"]["message"]
    assert not (tmp_path / "outputs" / job["job_id"] / "video_16x9.mp4").exists()


@pytest.mark.parametrize("script, expected", [
    ("A doctor reviews a chest X-ray.", "x-ray"),
    ("Students learn fractions in a classroom.", "fractions"),
    ("Farmers irrigate wheat fields using soil sensors.", "irrigate"),
    ("A spacecraft observes Saturn from orbit.", "saturn"),
])
def test_image_generation_jobs_keep_scene_and_job_prompts_separate(tmp_path, monkeypatch, script, expected):
    from PIL import Image

    _install_fake_generation(monkeypatch, tmp_path)
    prompts = []

    class FakeImageGenerator:
        name = "Mock local image generator"
        model = "mock-sd15"
        def prepare(self):
            return self.model
        def generate(self, prompt, destination, *, seed, ratio, avoid):
            prompts.append(prompt.lower())
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (128, 96), (40, 120, 80)).save(destination)
            return {"path": destination, "width": 512, "height": 288, "seed": seed, "model": self.model}

    monkeypatch.setattr(processing, "get_image_generator", FakeImageGenerator)
    job = _process_test_script(f"image-job-{expected.replace(' ', '-')}", script, "generated_images")
    assert job["status"] == "completed"
    assert job["timeline"]["scenes"][0]["text"] == script
    assert expected in prompts[0]
    assert len(job["result"]["videos"]) == 1
    report = job["result"]["image_generation"]
    assert report["model"] == "mock-sd15"
    assert len(report["images"]) == 1
    assert (tmp_path / "outputs" / job["job_id"] / report["images"][0]["file"]).is_file()


def test_image_generation_unconfigured_job_fails_without_fabricated_output(tmp_path, monkeypatch):
    _install_fake_generation(monkeypatch, tmp_path)
    monkeypatch.delenv("IMAGE_GENERATION_BACKEND", raising=False)
    monkeypatch.setattr(processing, "get_image_generator", lambda: __import__("backend.app.image_generation", fromlist=["Automatic1111ImageGenerator"]).Automatic1111ImageGenerator())
    job = _process_test_script("image-backend-not-configured", "A satellite crosses a nebula.", "generated_images")
    assert job["status"] == "failed"
    assert job["error"]["code"] == "image_generator_not_configured"
    assert job["result"] is None
    assert not list((tmp_path / "outputs" / job["job_id"]).glob("video_*.mp4"))


def test_repeated_generation_requests_create_single_mp4_in_distinct_job_folders(tmp_path, monkeypatch):
    _install_fake_generation(monkeypatch, tmp_path)
    first = _process_test_script("repeat-request-one", "A robot sorts packages in a warehouse.")
    second = _process_test_script("repeat-request-two", "A robot sorts packages in a warehouse.")

    assert first["job_id"] != second["job_id"]
    assert len(first["result"]["videos"]) == len(second["result"]["videos"]) == 1
    assert len(list((tmp_path / "outputs" / first["job_id"]).glob("*.mp4"))) == 1
    assert len(list((tmp_path / "outputs" / second["job_id"]).glob("*.mp4"))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"script": "A script.", "scenes": [{"text": "A scene."}]},
        {"script": "   "},
        {"audio_url": "https://example.com/audio.mp3", "audio_upload": True},
        {"aspect_ratios": ["4:3"], "scenes": [{"text": "A scene."}]},
    ],
)
def test_invalid_job_inputs_are_rejected(payload):
    with pytest.raises(ValueError):
        JobInput.model_validate(payload)


def test_generation_requires_one_output_format():
    with pytest.raises(ValueError):
        JobInput.model_validate({
            "script": "One story, one video.",
            "aspect_ratios": ["16:9", "9:16"],
        })


def test_manual_scenes_can_be_combined_with_uploaded_audio():
    request = JobInput.model_validate(
        {"audio_upload": True, "scenes": [{"text": "A manually timed scene."}]}
    )
    assert request.scenes[0].text == "A manually timed scene."


def test_local_tts_falls_back_to_espeak_when_system_voice_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ESPEAK_NG_PATH", "espeak-ng-test-double")

    def fail_init():
        raise RuntimeError("Windows speech voice is unavailable")

    monkeypatch.setitem(sys.modules, "pyttsx3", SimpleNamespace(init=fail_init))

    def fake_espeak(command, **_kwargs):
        destination = Path(command[command.index("-w") + 1])
        destination.write_bytes(b"RIFF test narration")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(processing.subprocess, "run", fake_espeak)
    voice, engine = processing.synthesize_speech("A test sentence.", tmp_path / "voice.wav", None)
    assert voice == "en-us"
    assert engine == "eSpeak NG"
    assert (tmp_path / "voice.wav").stat().st_size > 0


def test_idempotency_key_reuses_same_request_and_rejects_changed_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    database.initialize()
    payload = {"script": "Example video script.", "aspect_ratios": ["16:9"]}

    original, created = database.create_job("job-one", "same-hash", payload, "request-key")
    repeated, repeated_created = database.create_job("job-two", "same-hash", payload, "request-key")

    assert created is True
    assert repeated_created is False
    assert repeated["job_id"] == original["job_id"] == "job-one"
    with pytest.raises(ValueError, match="different request"):
        database.create_job("job-three", "other-hash", payload, "request-key")
    assert original["input"] == payload
