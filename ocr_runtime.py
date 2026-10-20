"""Runtime selection and isolated result paths for the two OCR backends."""

from __future__ import annotations

import json
import os
from pathlib import Path

OCR_MODES = ("online", "offline")
MODE_LABELS = {
    "online": "在线 OCR（TextIn）",
    "offline": "离线 OCR（PaddleOCR）",
}


def _normalise_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    if mode not in OCR_MODES:
        raise ValueError("OCR 模式必须为 online 或 offline")
    return mode


def mode_state_path(base_dir: Path) -> Path:
    override = os.getenv("OCR_MODE_STATE_FILE", "").strip()
    return Path(override) if override else Path(base_dir) / ".runtime" / "ocr_mode.json"


def get_ocr_mode(base_dir: Path | None = None) -> str:
    """Return the active mode; an explicit worker environment always wins."""
    explicit = os.getenv("OCR_MODE", "").strip()
    if explicit:
        return _normalise_mode(explicit)
    if base_dir is not None:
        path = mode_state_path(Path(base_dir))
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("mode")
            return _normalise_mode(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return "online"


def set_ocr_mode(base_dir: Path, mode: str) -> str:
    """Persist the UI selection atomically without storing it in the repository."""
    mode = _normalise_mode(mode)
    path = mode_state_path(Path(base_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps({"mode": mode}, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return mode


def mode_result_path(base_dir: Path, filename: str, mode: str | None = None) -> Path:
    """Return a backend-specific structured-result path."""
    mode = _normalise_mode(mode) if mode else get_ocr_mode(base_dir)
    source = Path(filename)
    return Path(base_dir) / f"{source.stem}.{mode}{source.suffix}"


def readable_result_path(base_dir: Path, filename: str, mode: str | None = None) -> Path:
    """Read legacy online results until that supplier is rerun in unified mode."""
    mode = _normalise_mode(mode) if mode else get_ocr_mode(base_dir)
    isolated = mode_result_path(base_dir, filename, mode)
    legacy = Path(base_dir) / filename
    if not isolated.exists() and mode == "online" and legacy.exists():
        return legacy
    return isolated


def cache_suffix(mode: str | None = None, base_dir: Path | None = None) -> str:
    mode = _normalise_mode(mode) if mode else get_ocr_mode(base_dir)
    return ".online" if mode == "online" else ".offline"
