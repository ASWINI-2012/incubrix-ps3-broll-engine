from __future__ import annotations

from pathlib import Path
import pytest

from backend.app import providers
from backend.app.providers import InternetArchiveProvider, WikimediaCommonsProvider
from backend.app.processing import split_script


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise providers.httpx.HTTPStatusError("HTTP error", request=None, response=self)

    def json(self):
        return self.payload


def test_commons_search_requires_video_and_clear_rights(monkeypatch):
    monkeypatch.setenv("MEDIA_API_USER_AGENT", "IncubixTests/1.0 (tests@example.org)")
    payload = {"query": {"pages": [{
        "title": "File:Students_in_class.mp4", "fullurl": "https://commons.wikimedia.org/wiki/File:Students_in_class.mp4",
        "imageinfo": [{"mime": "video/mp4", "url": "https://upload.wikimedia.org/a.mp4", "width": 640, "height": 360,
                       "extmetadata": {"Artist": {"value": "Alex Example"}, "LicenseShortName": {"value": "CC BY 4.0"},
                                       "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "AttributionRequired": {"value": "true"},
                                       "ObjectName": {"value": "Students in a classroom"}}}]
    }]}}
    captured = {}
    monkeypatch.setattr(providers.httpx, "get", lambda url, **kwargs: (captured.update(url=url, **kwargs) or Response(payload)))
    result = WikimediaCommonsProvider().search("students classroom")
    assert len(result) == 1
    assert result[0].creator == "Alex Example"
    assert result[0].license_name == "CC BY 4.0"
    assert "Attribution required" in result[0].attribution_required
    assert captured["url"] == "https://commons.wikimedia.org/w/api.php"
    assert captured["params"]["gsrnamespace"] == 6
    assert captured["headers"]["User-Agent"] == "IncubixTests/1.0 (tests@example.org)"


def test_commons_rejects_unknown_license_and_non_video(monkeypatch):
    monkeypatch.setenv("MEDIA_API_USER_AGENT", "IncubixTests/1.0 (tests@example.org)")
    pages = []
    for title, mime, license_url in [
        ("File:unknown.mp4", "video/mp4", ""),
        ("File:image.jpg", "image/jpeg", "https://creativecommons.org/licenses/by/4.0/"),
    ]:
        pages.append({"title": title, "fullurl": "https://commons.wikimedia.org/wiki/" + title,
                      "imageinfo": [{"mime": mime, "url": "https://upload.wikimedia.org/file", "extmetadata": {
                          "Artist": {"value": "A Creator"}, "LicenseShortName": {"value": "Unknown"},
                          "LicenseUrl": {"value": license_url}, "AttributionRequired": {"value": "true"}}}]})
    monkeypatch.setattr(providers.httpx, "get", lambda *_args, **_kwargs: Response({"query": {"pages": pages}}))
    assert WikimediaCommonsProvider().search("classroom") == []


def test_commons_rejects_conflicting_license_name_and_url(monkeypatch):
    monkeypatch.setenv("MEDIA_API_USER_AGENT", "IncubixTests/1.0 (tests@example.org)")
    page = {"title": "File:licensed.mp4", "fullurl": "https://commons.wikimedia.org/wiki/File:licensed.mp4",
            "imageinfo": [{"mime": "video/mp4", "url": "https://upload.wikimedia.org/file.mp4", "extmetadata": {
                "Artist": {"value": "Creator"}, "LicenseShortName": {"value": "CC0 1.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                "AttributionRequired": {"value": "true"}}}]}
    monkeypatch.setattr(providers.httpx, "get", lambda *_args, **_kwargs: Response({"query": {"pages": [page]}}))
    assert WikimediaCommonsProvider().search("licensed video") == []


def test_commons_rate_limit_is_reported_without_retry_or_bypass(monkeypatch):
    monkeypatch.setenv("MEDIA_API_USER_AGENT", "IncubixTests/1.0 (tests@example.org)")
    monkeypatch.setattr(providers.httpx, "get", lambda *_args, **_kwargs: Response({}, status=429))
    with pytest.raises(providers.FootageProviderError) as error:
        WikimediaCommonsProvider().search("space video")
    assert error.value.code == "commons_rate_limited"


def test_commons_without_operator_contact_skips_network(monkeypatch):
    monkeypatch.delenv("MEDIA_API_USER_AGENT", raising=False)
    monkeypatch.setattr(providers.httpx, "get", lambda *_args, **_kwargs: pytest.fail("Commons request must not be sent"))
    with pytest.raises(providers.FootageProviderError) as error:
        WikimediaCommonsProvider().search("doctor patient")
    assert error.value.code == "commons_contact_required"


def test_archive_advanced_search_and_metadata_yield_eligible_video(monkeypatch):
    calls = []
    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("advancedsearch.php"):
            return Response({"response": {"docs": [{"identifier": "classroom-film", "title": "Classroom film", "mediatype": "movies"}]}})
        return Response({"metadata": {"identifier": "classroom-film", "mediatype": "movies", "title": "Students in class", "creator": "Taylor Creator",
                                       "licenseurl": "https://creativecommons.org/licenses/by/4.0/", "files": []},
                         "files": [{"name": "lesson.mp4", "format": "MPEG4"}]})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    assets = InternetArchiveProvider().search("students classroom", limit=3)
    assert len(assets) == 1
    assert assets[0].provider == "Internet Archive"
    assert assets[0].source_url == "https://archive.org/details/classroom-film"
    assert assets[0].download_url == "https://archive.org/download/classroom-film/lesson.mp4"
    assert assets[0].license_name == "CC BY 4.0"
    assert "Attribution required" in assets[0].attribution_required
    assert calls[0][0] == "https://archive.org/advancedsearch.php"
    assert "mediatype:movies" in calls[0][1]["params"]["q"]
    assert "licenseurl:*" in calls[0][1]["params"]["q"]
    assert "creator:*" in calls[0][1]["params"]["q"]
    assert calls[1][0] == "https://archive.org/metadata/classroom-film"


def test_archive_subject_tags_do_not_establish_scene_relevance(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("advancedsearch.php"):
            return Response({"response": {"docs": [{"identifier": "clinic-software", "title": "Clinic online diary", "mediatype": "movies"}]}})
        return Response({"metadata": {"mediatype": "movies", "title": "Clinic online diary", "description": "", "subject": ["doctor", "patient"],
                          "creator": "Creator", "licenseurl": "https://creativecommons.org/licenses/by/4.0/"},
                         "files": [{"name": "clip.mp4", "format": "MPEG4"}]})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    asset = InternetArchiveProvider().search("doctor patient")[0]
    assert "doctor" not in asset.matching_text.lower()
    assert "patient" not in asset.matching_text.lower()
    assert "doctor patient" in asset.source_description.lower()


def test_archive_empty_results_and_missing_license_are_not_eligible(monkeypatch):
    monkeypatch.setattr(providers.httpx, "get", lambda *_args, **_kwargs: Response({"response": {"docs": []}}))
    assert InternetArchiveProvider().search("orchard harvesting") == []

    def no_license(url, **kwargs):
        if url.endswith("advancedsearch.php"):
            return Response({"response": {"docs": [{"identifier": "unlicensed", "mediatype": "movies"}]}})
        return Response({"metadata": {"mediatype": "movies", "creator": "Creator", "files": [{"name": "clip.mp4", "format": "MPEG4"}]}})
    monkeypatch.setattr(providers.httpx, "get", no_license)
    assert InternetArchiveProvider().search("space launch") == []


def test_archive_rejects_audio_document_and_unsupported_files(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("advancedsearch.php"):
            return Response({"response": {"docs": [{"identifier": "non-video", "mediatype": "movies"}]}})
        return Response({"metadata": {"mediatype": "movies", "creator": "Creator", "licenseurl": "https://creativecommons.org/licenses/zero/1.0/",
                                       "files": [{"name": "readme.txt", "format": "Text"}, {"name": "audio.mp3", "format": "MP3"}]}})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    assert InternetArchiveProvider().search("satellite orbit") == []


def test_valid_download_bytes_are_written_and_bad_content_type_is_rejected(monkeypatch, tmp_path):
    class StreamResponse:
        status_code = 200
        headers = {"content-type": "video/mp4"}
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): pass
        def iter_bytes(self, _size): yield b"video bytes"
    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def stream(self, *_args, **_kwargs): return StreamResponse()
    monkeypatch.setattr(providers.httpx, "Client", Client)
    destination = tmp_path / "clip.mp4"
    providers._download_video("https://upload.wikimedia.org/clip.mp4", destination, {"upload.wikimedia.org"})
    assert destination.read_bytes() == b"video bytes"

    class BadClient(Client):
        def stream(self, *_args, **_kwargs):
            response = StreamResponse()
            response.headers = {"content-type": "text/html"}
            return response
    monkeypatch.setattr(providers.httpx, "Client", BadClient)
    try:
        providers._download_video("https://upload.wikimedia.org/clip.mp4", destination, {"upload.wikimedia.org"})
    except providers.FootageProviderError as error:
        assert error.code == "invalid_media_type"
    else:
        raise AssertionError("HTML response must not be accepted as video")
    assert not destination.exists()


@pytest.mark.parametrize("script, expected", [
    ("Medical imaging helps doctors diagnose illness.", "imaging"),
    ("Teachers help students learn algebra in a classroom.", "teachers"),
    ("Farmers harvest wheat in a field.", "farmers"),
    ("A spacecraft orbits the planet Mars.", "spacecraft"),
])
def test_topic_search_is_derived_only_from_submitted_scene(script, expected, monkeypatch):
    queries = []
    def capture(_self, query, limit=5):
        queries.append(query)
        return []
    monkeypatch.setattr(WikimediaCommonsProvider, "search", capture)
    monkeypatch.setattr(InternetArchiveProvider, "search", capture)
    scene = split_script(script)[0]
    provider = providers.PublicFootageProvider()
    assert provider.search(scene, [], "documentary") == []
    assert len(queries) == 2
    assert all(expected in query for query in queries)
    assert all("healthcare" not in query and "patient" not in query for query in queries)
