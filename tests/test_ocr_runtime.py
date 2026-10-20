import json

import pytest

from ocr_runtime import (
    get_ocr_mode,
    mode_result_path,
    readable_result_path,
    set_ocr_mode,
)


def test_mode_results_are_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_MODE", raising=False)
    online = mode_result_path(tmp_path, "textin_results.json", "online")
    offline = mode_result_path(tmp_path, "textin_results.json", "offline")
    assert online.name == "textin_results.online.json"
    assert offline.name == "textin_results.offline.json"
    assert online != offline


def test_persisted_mode_and_worker_override(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_MODE", raising=False)
    assert get_ocr_mode(tmp_path) == "online"
    set_ocr_mode(tmp_path, "offline")
    assert get_ocr_mode(tmp_path) == "offline"
    monkeypatch.setenv("OCR_MODE", "online")
    assert get_ocr_mode(tmp_path) == "online"
    with pytest.raises(ValueError):
        set_ocr_mode(tmp_path, "cloud")


def test_legacy_results_are_read_only_online_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_MODE", raising=False)
    legacy = tmp_path / "stage2_results.json"
    legacy.write_text(json.dumps({"1": {"decision": "manual"}}), encoding="utf-8")
    assert readable_result_path(tmp_path, "stage2_results.json", "online") == legacy
    assert readable_result_path(tmp_path, "stage2_results.json", "offline") != legacy

    isolated = mode_result_path(tmp_path, "stage2_results.json", "online")
    isolated.write_text("{}", encoding="utf-8")
    assert readable_result_path(tmp_path, "stage2_results.json", "online") == isolated
