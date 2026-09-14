import json
import zipfile

import pytest

from installer.build_installer import build_payload
from installer.installer import inspect_payload, unpack_payload


def test_installer_bundle_contains_runtime_sources_but_no_private_data(tmp_path):
    archive = build_payload()
    files = inspect_payload(archive)
    assert {"requirements.txt", "offline_desens/requirements.txt", "offline_desens/download_models.py",
            "web/app.py", "start_webui.bat"} <= set(files)
    assert not any(".env" in name and name != ".env.example" for name in files)
    assert not any("cache" in name or "models/" in name or ".venv" in name or "audit" in name
                   for name in files)
    target = tmp_path / "clean-install"
    unpack_payload(archive, target)
    assert (target / "start_webui.bat").exists()
    assert not (target / ".env").exists()


def test_installer_rejects_tampered_or_traversal_payload(tmp_path):
    bundle = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"files": {"../escape.py": "0" * 64}}))
        archive.writestr("../escape.py", b"code")
    with pytest.raises(ValueError):
        unpack_payload(bundle, tmp_path / "target")
    assert not (tmp_path / "escape.py").exists()


def test_installer_refuses_an_existing_nonmanaged_directory(tmp_path, monkeypatch):
    from installer import installer

    target = tmp_path / "work"
    target.mkdir()
    (target / "user-data.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(installer, "payload_path", build_payload)
    with pytest.raises(RuntimeError, match="目标目录已有其他文件"):
        installer.install(target, python_exe=tmp_path / "python.exe")
    assert (target / "user-data.txt").read_text(encoding="utf-8") == "keep"


def test_python_download_refuses_wrong_digest(tmp_path, monkeypatch):
    import io

    from installer import installer

    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"invalid"))
    monkeypatch.setattr(installer.time, "sleep", lambda *args: None)
    target = tmp_path / "python.exe"
    with pytest.raises(ValueError, match="SHA-256"):
        installer.download_python(target)
    assert not target.exists()
