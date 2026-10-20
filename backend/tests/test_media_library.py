import json
import httpx

from backend.app import media_library
from backend.app import processing


def test_local_scan_catalogues_valid_video_and_skips_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    (tmp_path / "Computer Software").mkdir()
    (tmp_path / "Computer Software" / "bad.mp4").write_bytes(b"not a video")
    monkeypatch.setattr(media_library, "_inspect", lambda path: {"duration_seconds": 4, "width": 640, "height": 360} if path.name == "good.mp4" else None)
    (tmp_path / "Computer Software" / "good.mp4").write_bytes(b"video bytes")
    first = media_library.scan()
    second = media_library.scan()
    assert first["valid_videos"] == 1
    assert second["valid_videos"] == 1
    assert len(second["items"]) == 1


def test_missing_key_keeps_local_library_and_reports_source(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    result = media_library.collect(dry_run=False)
    assert result["downloaded"] == 0
    assert result["source_availability"]["pexels"] == "missing API key"
    assert "not configured" in result["failures"][0]


def test_catalogue_persists_provider_license_and_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    media_library._save({"items": [{"id": "one", "provider": "Pexels", "license_name": media_library.LICENSE_NAME, "license_url": media_library.LICENSE_URL, "attribution_required": "credit recommended"}], "status": "completed", "failures": []})
    persisted = json.loads((tmp_path / "clips.json").read_text(encoding="utf-8"))
    assert persisted["items"][0]["license_name"] == "Pexels License"
    assert persisted["items"][0]["attribution_required"] == "credit recommended"


def test_scene_matching_uses_metadata_and_returns_none_for_unrelated_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    media_library._save({"items": [{"id": "farm-1", "status": "valid", "caption": "tractor harvesting wheat in a field", "category": "Agriculture and farming", "tags": ["farm", "crop", "field"], "source_url": "https://example.org/farm", "provider": "Example archive", "creator": "A. Creator", "license_name": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/", "attribution_required": "Credit creator", "usage_rights_confirmed": True}]})
    match = media_library.match_scene("A tractor works in a wheat field.")
    assert match and match["id"] == "farm-1"
    assert media_library.match_scene("Astronauts explore distant galaxies.") is None


def test_import_is_repeatable_and_keeps_sidecar_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    folder = tmp_path / "Healthcare and medicine"
    folder.mkdir()
    video = folder / "telemedicine-consultation.mp4"
    video.write_bytes(b"validated fixture")
    (folder / "telemedicine-consultation.mp4.json").write_text(json.dumps({
        "category": "Healthcare and medicine",
        "caption": "Patient speaking with a doctor by video call",
        "description": "Telemedicine consultation",
        "tags": ["telemedicine", "doctor", "patient", "video consultation"],
        "source_url": "https://example.org/source/telemedicine",
        "source_id": "telemedicine-123",
        "provider": "Example archive",
        "provider_source": "sidecar_source_page",
        "creator": "A. Creator",
        "license_name": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_required": "Credit A. Creator; link to source and licence.",
        "usage_rights_confirmed": True,
    }), encoding="utf-8")
    monkeypatch.setattr(media_library, "_inspect", lambda path: {"duration_seconds": 8, "width": 1280, "height": 720})

    first = media_library.import_local_media()
    second = media_library.import_local_media()
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert len(second["items"]) == 1
    item = second["items"][0]
    assert item["license_name"] == "CC BY 4.0"
    assert item["source_url"] == "https://example.org/source/telemedicine"
    assert item["source_id"] == "telemedicine-123"
    assert item["provider_source"] == "sidecar_source_page"
    assert item["review_required"] is False
    assert media_library.match_scene("patient doctor telemedicine video consultation")


def test_import_missing_rights_metadata_flags_manual_review_and_is_not_matchable(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    video = tmp_path / "smartwatch-heart-rate.mp4"
    video.write_bytes(b"validated fixture")
    monkeypatch.setattr(media_library, "_inspect", lambda path: {"duration_seconds": 5, "width": 640, "height": 360})
    result = media_library.import_local_media()
    item = result["items"][0]
    assert item["review_required"] is True
    assert "source_url" in item["review_reasons"]
    assert "creator" in item["review_reasons"]
    assert "license_name" in item["review_reasons"]
    assert "license_url" in item["review_reasons"]
    assert "attribution_required" in item["review_reasons"]
    assert item["source_url"] is None and item["license_name"] is None
    assert result["usable_videos"] == 0
    assert media_library.match_scene("smartwatch heart rate") is None


def test_import_skips_invalid_media_and_healthcare_report_names_missing_scenes(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    (tmp_path / "broken.mp4").write_bytes(b"not media")
    monkeypatch.setattr(media_library, "_inspect", lambda path: None)
    result = media_library.import_local_media()
    assert result["invalid"] == 1
    report = media_library.validate_healthcare()
    assert report["usable_videos"] == 0
    assert "telemedicine" in report["missing_scenes"]


def test_healthcare_validator_separates_visual_match_from_rights_clearance(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    media_library._save({"items": [{
        "id": "doctor-consult-1", "path": "doctor.mp4", "status": "valid", "media_type": "video",
        "caption": "Doctor consultation explaining medical information",
        "tags": ["doctor consultation", "medical information", "doctor", "patient", "healthcare"],
        "review_required": True, "rights_review_required": True,
        "rights_review_reasons": ["source_url", "license_name"],
    }]})
    assert media_library.match_scene("doctor consultation patient clinical care") is None
    report = media_library.validate_healthcare()
    clinical = next(scene for scene in report["healthcare_scenes"] if scene["scene"] == "clinical consultation")
    assert report["usable_videos"] == 0
    assert report["visually_relevant_videos"] == 1
    assert clinical["status"] == "blocked_by_review"
    assert clinical["rights_review_required"] is True
    assert "clinical consultation" not in report["missing_scenes"]
    assert "clinical consultation" in report["rights_blocked_scenes"]


def test_healthcare_validator_does_not_call_mri_ai_or_accept_weak_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    media_library._save({"items": [{
        "id": "ordinary-mri", "path": "mri.mp4", "status": "valid", "media_type": "video",
        "caption": "Patient undergoing an MRI scan",
        "description": "A patient lies inside an MRI scanner during medical imaging.",
        "tags": ["MRI", "MRI scanner", "medical imaging", "patient", "clinician"],
        "review_required": True, "rights_review_required": True,
        "rights_review_reasons": ["source_url", "usage_rights_confirmed"],
    }]})
    report = media_library.validate_healthcare()
    mri = next(scene for scene in report["healthcare_scenes"] if scene["scene"] == "MRI scan")
    ai = next(scene for scene in report["healthcare_scenes"] if scene["scene"] == "AI-assisted medical imaging")
    consultation = next(scene for scene in report["healthcare_scenes"] if scene["scene"] == "clinical consultation")
    assert mri["status"] == "blocked_by_review"
    assert ai["status"] == "missing"
    assert consultation["status"] == "missing"


def test_rights_report_lists_existing_values_and_required_checks_without_approving(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    items = [{
        "id": "review-1", "path": "unknown-source.mp4", "media_type": "video",
        "provider": "Pexels", "provider_source": "user_reported",
        "source_url": None, "creator": None, "license_name": None, "license_url": None,
        "attribution_required": None, "usage_rights_confirmed": None,
        "review_required": True, "rights_review_required": True,
        "visual_review_required": True, "visual_review_status": "needs_review",
        "content_review_note": "Review the full clip.",
    }]
    original = json.dumps(items, sort_keys=True)
    media_library._save({"items": items})
    report = media_library.rights_review_report()
    entry = report["items"][0]
    assert set(entry["missing_rights_fields"]) == {
        "source_url", "creator", "license_name", "license_url",
        "attribution_required", "usage_rights_confirmed",
    }
    assert entry["provider"] == "Pexels"
    assert entry["provider_source"] == "user_reported"
    assert entry["usage_rights_confirmed"] is None
    assert entry["usage_rights_status"] == "unknown / not recorded"
    assert entry["visual_review_required"] is True
    assert any("creator/uploader" in step for step in entry["required_verification_steps"])
    assert json.dumps(media_library._load()["items"], sort_keys=True) == original


def test_scene_match_rechecks_required_rights_instead_of_trusting_stale_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    item = {
        "id": "stale-approval", "path": "telehealth.mp4", "status": "valid", "media_type": "video",
        "caption": "Patient and doctor video consultation", "tags": ["telehealth", "doctor", "patient"],
        "source_url": "https://example.org/source", "provider": "Example archive", "creator": "A. Creator",
        "license_name": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_required": "Credit creator", "usage_rights_confirmed": None,
        "rights_review_required": False, "review_required": False,
    }
    media_library._save({"items": [item]})
    assert media_library.match_scene("telehealth doctor patient") is None
    report = media_library.rights_review_report()
    assert report["rights_review_required"] == 1
    assert "usage_rights_confirmed" in report["items"][0]["missing_rights_fields"]


def test_scene_match_rejects_invalid_rights_urls_and_visual_review(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    base = {
        "status": "valid", "media_type": "video", "caption": "Doctor consultation with patient",
        "tags": ["doctor", "consultation", "patient"], "provider": "Example archive",
        "creator": "A. Creator", "license_name": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_required": "Credit creator", "usage_rights_confirmed": True,
    }
    http_source = {**base, "id": "http-source", "source_url": "http://example.org/source"}
    visual_pending = {**base, "id": "visual-pending", "source_url": "https://example.org/source", "visual_review_required": True}
    media_library._save({"items": [http_source, visual_pending]})
    assert media_library.match_scene("doctor consultation patient") is None


def test_import_catalogues_valid_image_but_never_selects_it_as_video(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    image = tmp_path / "doctor-consultation.png"
    image.write_bytes(b"image fixture")
    monkeypatch.setattr(media_library, "_inspect", lambda path: {"width": 800, "height": 600, "aspect_ratio": 1.3333})
    result = media_library.import_local_media()
    assert result["valid_images"] == 1
    assert result["usable_videos"] == 0
    assert media_library.match_scene("doctor consultation") is None


def test_rescan_preserves_visual_review_annotations_and_unverified_rights(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    video = tmp_path / "doctor_patient.mp4"
    video.write_bytes(b"validated fixture")
    media_library._save({"items": [{
        "id": "doctor-patient-1", "path": video.name, "media_type": "video", "category": "Healthcare and medicine",
        "caption": "Doctor using a blood-pressure monitor with a patient",
        "description": "A clinician measures a patient's blood pressure.",
        "tags": ["doctor", "patient", "blood-pressure monitor"], "source_url": None, "provider": None,
        "license_name": None, "license_url": None, "review_required": True,
        "review_reasons": ["source_url", "provider", "license_name", "usage_rights_confirmed", "visual_context_review"],
        "caption_source": "visual_contact_sheet", "tags_source": "visual_contact_sheet",
    }]})
    monkeypatch.setattr(media_library, "_inspect", lambda path: {"duration_seconds": 3, "width": 640, "height": 360})
    item = media_library.import_local_media()["items"][0]
    assert item["caption"] == "Doctor using a blood-pressure monitor with a patient"
    assert item["tags"] == ["doctor", "patient", "blood-pressure monitor"]
    assert item["source_url"] is None and item["license_name"] is None
    assert item["review_required"] is True
    assert "visual_context_review" in item["review_reasons"]
    assert media_library.match_scene("doctor patient blood pressure monitor") is None


def test_temporary_provider_failure_is_reported_and_resumable(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setenv("MEDIA_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(media_library, "QUERIES", {"Test category": "test"})
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(media_library.httpx, "get", fail)
    result = media_library.collect(dry_run=False, max_assets=1)
    assert result["downloaded"] == 0
    assert result["failures"]
    assert media_library._load()["status"] == "completed"


def test_scene_uses_local_catalogue_as_fallback_after_online_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(tmp_path))
    clip = tmp_path / "farm.mp4"
    clip.write_bytes(b"validated fixture")
    media_library._save({"items": [{"id": "farm-1", "path": "farm.mp4", "status": "valid", "media_type": "video", "caption": "tractor harvests wheat", "category": "Agriculture", "tags": ["farming", "tractor", "wheat", "harvest"], "provider": "Pexels", "source_url": "https://www.pexels.com/video/farm-1/", "creator": "A. Creator", "license_name": "Pexels License", "license_url": "https://www.pexels.com/license/", "attribution_required": "Credit creator when practical", "usage_rights_confirmed": True, "validation": {}}]})
    class OnlineProvider:
        mode = "production"
        name = "online"
        matching_method = "test"
        fallback_warnings = []
        def search(self, *args):
            return []
    scene = {"scene_id": "scene-001", "text": "A tractor harvests wheat."}
    matched = processing.select_footage(scene, [], set(), tmp_path, "documentary", OnlineProvider())
    assert matched["clip"]["asset_id"] == "farm-1"
    assert matched["clip"]["provider_mode"] == "local"
