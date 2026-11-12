"""Main-runtime adapter for the isolated, network-blocked PaddleOCR worker."""
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from material_policy import digest

SUPPORTED_TYPES = frozenset({"business_license", "tax_credit", "tax_cert", "iso9001",
    "iso14001", "iso45001", "production_license", "transport_license", "after_sales_cert"})
SUPPORTED_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".bmp"})

log = logging.getLogger(__name__)


class LocalOcrWorkerError(RuntimeError):
    """A worker-level failure that is safe to retry in a fresh process."""

    def __init__(self, message, *, reason="worker_failed"):
        super().__init__(message)
        self.reason = reason


def _worker_environment():
    """Return a stable CPU-only environment without oversubscribing Windows."""
    env = os.environ.copy()
    try:
        threads = max(1, min(8, int(env.get("LOCAL_OCR_THREADS", "2"))))
    except ValueError:
        threads = 2
    value = str(threads)
    env.update(
        PYTHONIOENCODING="utf-8",
        PROCESSOR_ARCHITECTURE="AMD64",
        OMP_NUM_THREADS=value,
        MKL_NUM_THREADS=value,
        OPENBLAS_NUM_THREADS=value,
        NUMEXPR_NUM_THREADS=value,
        FLAGS_allocator_strategy="auto_growth",
    )
    return env


_TECHNICAL_ERROR = re.compile(
    r"(?:error|exception|runtime|failed|failure|terminate|memory|alloc|"
    r"paddle|onednn|mkldnn|enforce)", re.I)


def _technical_error_summary(stderr, paths, model_dir):
    """Keep diagnostic categories while excluding OCR text and material paths."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    replacements = [(str(Path(path)), "<material>") for path in paths]
    replacements.append((str(model_dir), "<model>"))
    lines = []
    for raw_line in str(stderr or "").splitlines():
        if not _TECHNICAL_ERROR.search(raw_line):
            continue
        line = raw_line
        for private, placeholder in replacements:
            line = line.replace(private, placeholder)
        line = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<local-path>", line)
        lines.append(line.strip())
    return " | ".join(lines[-3:])[:800] or "no technical stderr"


def _invoke_worker(paths):
    """Run one isolated OCR worker and return its response."""
    import desensitize

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="supplier-local-ocr-") as folder:
        request = Path(folder) / "request.json"
        response = Path(folder) / "response.json"
        request.write_text(json.dumps(paths), encoding="utf-8")
        try:
            proc = subprocess.run(
                [desensitize._PADDLE_PYTHON,
                 str(Path(__file__).parent / "offline_desens" / "material_ocr.py"),
                 str(request), str(response), "--model-dir", desensitize._PADDLE_MODEL_DIR],
                capture_output=True, timeout=max(180, len(paths) * 180),
                env=_worker_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            log.warning("[LocalOCR] worker timeout files=%d elapsed=%.1fs", len(paths), elapsed)
            raise LocalOcrWorkerError("本地 OCR 工作进程超时", reason="timeout") from exc

        elapsed = time.perf_counter() - started
        summary = _technical_error_summary(proc.stderr, paths, desensitize._PADDLE_MODEL_DIR)
        if proc.returncode:
            log.warning(
                "[LocalOCR] worker exit=%d files=%d elapsed=%.1fs stderr=%s",
                proc.returncode, len(paths), elapsed, summary)
            raise LocalOcrWorkerError(
                f"本地 OCR 工作进程异常退出（exit={proc.returncode}）",
                reason=f"exit_{proc.returncode}")
        if not response.is_file():
            log.warning(
                "[LocalOCR] worker produced no response files=%d elapsed=%.1fs stderr=%s",
                len(paths), elapsed, summary)
            raise LocalOcrWorkerError(
                "本地 OCR 工作进程未生成结果", reason="missing_response")
        try:
            result = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[LocalOCR] invalid response files=%d elapsed=%.1fs", len(paths), elapsed)
            raise LocalOcrWorkerError(
                "本地 OCR 工作进程返回结果损坏", reason="invalid_response") from exc
        if not isinstance(result, dict):
            raise LocalOcrWorkerError(
                "本地 OCR 工作进程返回格式异常", reason="invalid_response")
        metrics = result.pop("__worker_meta__", {})
        log.info(
            "[LocalOCR] completed files=%d pages=%s elapsed=%.1fs model_load_ms=%s ocr_ms=%s",
            len(paths), metrics.get("pages", "?"), elapsed,
            metrics.get("model_load_ms", "?"), metrics.get("ocr_ms", "?"))
        return result


def cache_identity():
    import desensitize
    model_dir = Path(desensitize._PADDLE_MODEL_DIR)
    # Bump whenever preprocessing/reading changes so stale OCR cache is rebuilt.
    parts = ["paddleocr-2.9.1/material-v5-iso-orientation/dpi200"]
    for part in ("det", "rec", "cls"):
        for name in ("inference.pdmodel", "inference.pdiparams"):
            path = model_dir / part / name
            parts.append(digest(path) if path.is_file() else "missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def parse_files(paths):
    import desensitize
    paths = list(dict.fromkeys(str(Path(path).resolve()) for path in paths))
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
    try:
        results.update(_invoke_worker(supported_paths))
    except LocalOcrWorkerError as batch_error:
        # Native Paddle failures are often transient. Restart the interpreter so
        # allocator/native-library state is clean, then isolate every document:
        # one bad file must not turn the whole supplier batch into RuntimeError.
        log.warning(
            "[LocalOCR] batch failed reason=%s; restarting as %d isolated job(s)",
            batch_error.reason, len(supported_paths))
        for index, path in enumerate(supported_paths, 1):
            try:
                results.update(_invoke_worker([path]))
            except LocalOcrWorkerError as retry_error:
                log.error(
                    "[LocalOCR] isolated retry failed item=%d/%d reason=%s",
                    index, len(supported_paths), retry_error.reason)
                results[path] = {
                    "error": "本地 OCR 工作进程失败",
                    "error_code": retry_error.reason,
                }
    return results
