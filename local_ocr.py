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


def cache_identity():
    import desensitize
    model_dir = Path(desensitize._PADDLE_MODEL_DIR)
    parts = ["paddleocr-2.9.1/material-v1/dpi200"]
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
    if not Path(desensitize._PADDLE_PYTHON).is_file():
        raise RuntimeError("Paddle 本地解释器缺失，请运行 setup_paddle.ps1")
    with tempfile.TemporaryDirectory(prefix="supplier-local-ocr-") as folder:
        request = Path(folder) / "request.json"
        response = Path(folder) / "response.json"
        request.write_text(json.dumps(paths), encoding="utf-8")
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PROCESSOR_ARCHITECTURE="AMD64")
        proc = subprocess.run([desensitize._PADDLE_PYTHON,
            str(Path(__file__).parent / "offline_desens" / "material_ocr.py"),
            str(request), str(response), "--model-dir", desensitize._PADDLE_MODEL_DIR],
            capture_output=True, timeout=max(180, len(paths) * 180), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode or not response.is_file():
            raise RuntimeError("本地 OCR 工作进程失败，请检查 Paddle 环境和本地模型")
        return json.loads(response.read_text(encoding="utf-8"))
