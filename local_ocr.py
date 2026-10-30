"""Main-runtime adapter for the isolated, network-blocked PaddleOCR worker."""
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from material_policy import digest

SUPPORTED_TYPES = frozenset({"business_license", "tax_credit", "tax_cert", "iso9001",
    "iso14001", "iso45001", "production_license", "transport_license", "after_sales_cert"})
SUPPORTED_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".bmp"})


def cache_identity():
    import desensitize
    model_dir = Path(desensitize._PADDLE_MODEL_DIR)
    # Bump whenever preprocessing/reading changes so stale OCR cache is rebuilt.
    parts = ["paddleocr-2.9.1/material-v3-page-orientation/dpi200"]
    for part in ("det", "rec", "cls"):
        for name in ("inference.pdmodel", "inference.pdiparams"):
            path = model_dir / part / name
            parts.append(digest(path) if path.is_file() else "missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def parse_files(paths):
    import desensitize
    paths = [str(Path(path).resolve()) for path in paths]
    if not paths:
        return {}
    results = {}
    supported_paths = []
    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            label = suffix.removeprefix(".").upper() or "未知"
            results[path] = {
                "error": f"离线 OCR 暂不支持 {label} 格式，已转人工核验",
                "error_code": "unsupported_format",
            }
        else:
            supported_paths.append(path)
    if not supported_paths:
        return results
    if not Path(desensitize._PADDLE_PYTHON).is_file():
        raise RuntimeError("Paddle 本地解释器缺失，请运行 setup_paddle.ps1")
    with tempfile.TemporaryDirectory(prefix="supplier-local-ocr-") as folder:
        request = Path(folder) / "request.json"
        response = Path(folder) / "response.json"
        request.write_text(json.dumps(supported_paths), encoding="utf-8")
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PROCESSOR_ARCHITECTURE="AMD64")
        try:
            proc = subprocess.run([desensitize._PADDLE_PYTHON,
                str(Path(__file__).parent / "offline_desens" / "material_ocr.py"),
                str(request), str(response), "--model-dir", desensitize._PADDLE_MODEL_DIR],
                capture_output=True, timeout=max(180, len(supported_paths) * 180), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("本地 OCR 工作进程超时，已转人工核验") from exc
        if proc.returncode or not response.is_file():
            raise RuntimeError(
                f"本地 OCR 工作进程异常退出（exit={proc.returncode}），"
                "请检查 Paddle 环境和本地模型")
        try:
            worker_results = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("本地 OCR 工作进程返回了无效结果，已转人工核验") from exc
        results.update(worker_results)
        return results
