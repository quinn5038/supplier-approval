"""First-use Windows setup; source and credentials stay in separate folders."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
# SHA-256 from the matching python.org .sigstore messageDigest.
PYTHON_SHA256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"


def payload_path():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload.zip"


def inspect_payload(archive_path: Path):
    with zipfile.ZipFile(archive_path) as archive:
        metadata = json.loads(archive.read("manifest.json"))
        expected = metadata["files"]
        actual = set(archive.namelist())
        if actual != set(expected) | {"manifest.json"}:
            raise ValueError("安装包文件清单不一致")
        for name, digest in expected.items():
            pieces = PurePosixPath(name).parts
            if not pieces or any(part in (".", "..") for part in pieces) or name.startswith("/") or "\\" in name:
                raise ValueError("安装包包含非法路径")
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError(f"安装包文件校验失败：{name}")
        return expected


def unpack_payload(archive_path: Path, destination: Path):
    expected = inspect_payload(archive_path)
    destination.mkdir(parents=True, exist_ok=True)
    for name in expected:
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            data = archive.read(name)
        # Never touch user credentials, caches or prior results.
        target.write_bytes(data)
    (destination / ".installer-managed").write_text("SupplierApproval installer v1\n", encoding="utf-8")


def download_python(target: Path):
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == PYTHON_SHA256:
        return
    print("正在从 python.org 下载 Python 3.12.10（约 26 MB）...", flush=True)
    temp = target.with_suffix(".partial")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(PYTHON_URL, timeout=120) as response, temp.open("wb") as output:
                received = 0
                while block := response.read(1024 * 1024):
                    output.write(block)
                    received += len(block)
                    if received // (5 * 1024 * 1024) != (received - len(block)) // (5 * 1024 * 1024):
                        print(f"Python 下载已完成 {received // (1024 * 1024)} MB", flush=True)
            if hashlib.sha256(temp.read_bytes()).hexdigest() != PYTHON_SHA256:
                raise ValueError("Python 官方安装包 SHA-256 不匹配，已拒绝运行")
            temp.replace(target)
            return
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2)


def run(command, *, cwd=None):
    subprocess.run([str(value) for value in command], cwd=cwd, check=True)


def install(destination: Path, *, launch=True, python_exe: Path | None = None):
    archive = payload_path()
    inspect_payload(archive)
    if destination.exists() and any(destination.iterdir()) and not (destination / ".installer-managed").exists():
        raise RuntimeError("目标目录已有其他文件，请选择空目录，避免覆盖原有项目")
    destination.mkdir(parents=True, exist_ok=True)
    # Mark ownership before the first network step so interrupted installations can resume.
    (destination / ".installer-managed").write_text("SupplierApproval installer v1\n", encoding="utf-8")
    if not python_exe:
        runtime = destination / "runtime" / "python312"
        python_exe = runtime / "python.exe"
        if not python_exe.is_file():
            destination.mkdir(parents=True, exist_ok=True)
            installer = destination / "runtime" / "python-3.12.10-amd64.exe"
            installer.parent.mkdir(parents=True, exist_ok=True)
            download_python(installer)
            print("正在将 Python 安装到本项目目录，不修改系统 PATH...", flush=True)
            run([installer, "/quiet", "InstallAllUsers=0", f"TargetDir={runtime}",
                 "Include_pip=1", "Include_launcher=0", "PrependPath=0",
                 "AssociateFiles=0", "Shortcuts=0", "Include_test=0"])
            if not python_exe.is_file():
                raise RuntimeError("Python 安装后未找到 python.exe")
    version = subprocess.check_output([str(python_exe), "-c", "import sys;print(sys.version_info[:2])"], text=True).strip()
    if version != "(3, 12)":
        raise RuntimeError("主项目需要 64 位 Python 3.12")
    print("正在核对并部署项目文件...", flush=True)
    unpack_payload(archive, destination)
    config_file = destination / ".env"
    if not config_file.exists():
        config_file.write_bytes((destination / ".env.example").read_bytes())
    main = destination / ".venv" / "Scripts" / "python.exe"
    ocr = destination / ".paddle-venv" / "Scripts" / "python.exe"
    if not main.is_file():
        run([python_exe, "-m", "venv", destination / ".venv"])
    if not ocr.is_file():
        run([python_exe, "-m", "venv", destination / ".paddle-venv"])
    print("正在安装网页和规则依赖...", flush=True)
    run([main, "-m", "pip", "install", "-r", "requirements.txt"], cwd=destination)
    print("正在安装隔离的 PaddleOCR CPU 环境（首次需下载较多文件）...", flush=True)
    run([ocr, "-m", "pip", "install", "paddlepaddle", "-r", "offline_desens/requirements.txt",
         "-c", "offline_desens/constraints-cpu.txt"], cwd=destination)
    print("正在下载三组官方中文 OCR 模型...", flush=True)
    run([main, "offline_desens/download_models.py"], cwd=destination)
    run([main, "-m", "pip", "check"], cwd=destination)
    run([ocr, "-m", "pip", "check"], cwd=destination)
    print("正在验证真实 OCR 推理（仅用合成文字，禁止联网）...", flush=True)
    run([ocr, "offline_desens/smoke_test.py"], cwd=destination)
    print(f"安装完成。以后双击 {destination / 'start_webui.bat'} 即可。", flush=True)
    if launch:
        os.startfile(destination / "start_webui.bat")


def main():
    parser = argparse.ArgumentParser(description="供应商辅助审批系统首次安装")
    parser.add_argument("--install-dir", type=Path, help="安装目录，默认当前用户 LocalAppData/SupplierApproval")
    parser.add_argument("--check-bundle", action="store_true", help="只校验 EXE 内的项目文件，不安装")
    parser.add_argument("--no-launch", action="store_true", help="安装完不启动网页")
    parser.add_argument("--python-executable", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_bundle:
        files = inspect_payload(payload_path())
        print(f"安装包验证通过：{len(files)} 个项目文件；未包含本机 .env 和缓存。")
        return
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("安装程序仅支持 Windows x64")
    local = os.environ.get("LOCALAPPDATA")
    if not args.install_dir and not local:
        raise RuntimeError("未找到 LOCALAPPDATA，请使用 --install-dir 指定安装目录")
    destination = (args.install_dir or Path(local) / "SupplierApproval").resolve()
    try:
        install(destination, launch=not args.no_launch, python_exe=args.python_executable)
    except Exception as exc:
        print(f"安装未完成：{exc}", file=sys.stderr)
        print("请检查网络和磁盘空间后重新运行 EXE；原有配置不会删除。", file=sys.stderr)
        if sys.stdin.isatty():
            input("按回车退出...")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
