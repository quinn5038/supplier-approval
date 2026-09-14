"""Build an allowlisted source payload and a one-file Windows installer."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".installer-cache"
PAYLOAD = BUILD / "payload.zip"
ROOT_FILES = {"README.md", ".env.example", "requirements.txt", "start_webui.bat", "start_demo.bat", "setup.ps1", "setup_paddle.ps1", "rules.yaml"}
SUBFOLDERS = {"web", "offline_desens"}


def sources():
    for file in sorted(ROOT.iterdir()):
        if file.is_file() and (file.suffix == ".py" or file.name in ROOT_FILES):
            yield file
    for name in sorted(SUBFOLDERS):
        for file in sorted((ROOT / name).rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts and (file.suffix in {".py", ".html", ".css", ".js", ".txt", ".md"}):
                yield file


def build_payload():
    BUILD.mkdir(exist_ok=True)
    selected = list(sources())
    names = [p.relative_to(ROOT).as_posix() for p in selected]
    if any(".env" in name and name != ".env.example" for name in names):
        raise ValueError("拒绝打包用户凭证")
    if not {"web/app.py", "offline_desens/idcard_masker.py", "rules.yaml", "start_webui.bat"} <= set(names):
        raise ValueError("缺少必需项目文件")
    content = {name: file.read_bytes() for name, file in zip(names, selected)}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    with zipfile.ZipFile(PAYLOAD, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in content.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps({"files": manifest}, sort_keys=True))
    print(f"已核对 {len(content)} 个代码/资源文件，无本机凭证和业务缓存。", flush=True)
    return PAYLOAD


if __name__ == "__main__":
    payload = build_payload()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile",
                    "--console", "--name", "SupplierApproval-Setup", "--distpath", str(ROOT / "dist"),
                    "--workpath", str(BUILD / "pyinstaller-work"), "--specpath", str(BUILD),
                    f"--add-data={payload}{';' if sys.platform == 'win32' else ':'}.",
                    str(ROOT / "installer" / "installer.py")], check=True, cwd=ROOT)
