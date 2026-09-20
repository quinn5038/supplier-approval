"""Benchmark local OCR against legacy TextIn cache and optionally live TextIn.

The output contains aggregate metrics only. Supplier names, paths and OCR text are
never written to the report.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import local_ocr  # noqa: E402
import material_policy  # noqa: E402
import textin_pipeline as pipeline  # noqa: E402


def norm(value):
    return re.sub(r"[\s|_*#:：，,。；;（）()]", "", str(value or "")).lower()


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def select_samples(per_type):
    cache = material_policy.load_cache(ROOT)
    groups = defaultdict(list)
    root = ROOT / "files_cache_desens"
    for path in root.rglob("*") if root.exists() else []:
        if not path.is_file() or path.suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".bmp"}:
            continue
        types = material_policy.file_types(path, cache)
        if len(types) != 1:
            continue
        kind = next(iter(types))
        legacy = path.with_suffix(path.suffix + ".md")
        if kind in local_ocr.SUPPORTED_TYPES and legacy.is_file() and path.stat().st_size <= 12 * 1024 * 1024:
            groups[kind].append(path)
    chosen = []
    for kind in sorted(groups):
        # Stable selection, alternating small and large files for varied scans.
        ordered = sorted(groups[kind], key=lambda p: (p.stat().st_size, str(p)))
        picks = ordered[::max(1, len(ordered) // per_type)][:per_type]
        chosen.extend((path, kind) for path in picks)
    return chosen


def legacy_result(path, kind, supplier):
    text = path.with_suffix(path.suffix + ".md").read_text(encoding="utf-8")
    detail_path = path.with_suffix(path.suffix + ".detail.json")
    detail = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.exists() else None
    return pipeline.extract(kind, text, supplier, detail)


def score(samples, local_results):
    cache = material_policy.load_cache(ROOT)
    totals = defaultdict(int)
    by_type = defaultdict(lambda: defaultdict(int))
    minimum_confidences = []
    field_mismatches = defaultdict(int)
    check_mismatches = defaultdict(int)
    for path, kind in samples:
        bucket = by_type[kind]
        bucket["documents"] += 1
        parsed = local_results.get(str(path.resolve()), {})
        if parsed.get("error") or not parsed.get("markdown"):
            bucket["local_ocr_failures"] += 1
            continue
        supplier = cache.get(path.parent.name, {}).get("supplier", {})
        local = pipeline.extract(kind, parsed["markdown"], supplier, parsed.get("detail"))
        legacy = legacy_result(path, kind, supplier)
        confidences = [float(row.get("confidence", 0)) for row in parsed.get("detail", [])]
        minimum_confidences.append(min(confidences) if confidences else 0.0)
        if not confidences or min(confidences) < .9:
            local.setdefault("checks", {})["OCR文字置信度充足"] = None
            local.setdefault("issues", []).append("存在低置信度或缺失置信度文字，转人工核验")
        local_fields, legacy_fields = local.get("fields", {}), legacy.get("fields", {})
        keys = [key for key in local_fields.keys() & legacy_fields.keys()
                if local_fields.get(key) not in (None, "") and legacy_fields.get(key) not in (None, "")]
        bucket["comparable_fields"] += len(keys)
        bucket["matching_fields"] += sum(norm(local_fields[key]) == norm(legacy_fields[key]) for key in keys)
        for key in keys:
            if norm(local_fields[key]) != norm(legacy_fields[key]):
                field_mismatches[f"{kind}:{key}"] += 1
        check_keys = local.get("checks", {}).keys() & legacy.get("checks", {}).keys()
        bucket["comparable_checks"] += len(check_keys)
        bucket["matching_checks"] += sum(local["checks"][key] == legacy["checks"][key] for key in check_keys)
        for key in check_keys:
            if local["checks"][key] != legacy["checks"][key]:
                check_mismatches[f"{kind}:{key}"] += 1
        local_pass = bool(local.get("checks")) and all(v is True for v in local["checks"].values()) and not local.get("issues")
        legacy_pass = bool(legacy.get("checks")) and all(v is True for v in legacy["checks"].values()) and not legacy.get("issues")
        bucket["same_pass_decision"] += local_pass == legacy_pass
        bucket["local_auto_pass"] += local_pass
        bucket["legacy_auto_pass"] += legacy_pass
        bucket["low_confidence_documents"] += not confidences or min(confidences) < .9
    for bucket in by_type.values():
        for key, value in bucket.items():
            totals[key] += value
    ordered = sorted(minimum_confidences)
    confidence = {
        "document_minimum_median": round(ordered[len(ordered) // 2], 4) if ordered else None,
        "documents_below_0_70": sum(value < .7 for value in ordered),
        "documents_below_0_80": sum(value < .8 for value in ordered),
        "documents_below_0_90": sum(value < .9 for value in ordered),
    }
    differences = {"field_mismatches": dict(sorted(field_mismatches.items())),
                   "check_mismatches": dict(sorted(check_mismatches.items()))}
    return dict(totals), {key: dict(value) for key, value in by_type.items()}, confidence, differences


def load_main_module():
    load_env()
    with tempfile.TemporaryDirectory(prefix="main-ocr-source-") as folder:
        source = Path(folder) / "main_textin_pipeline.py"
        content = subprocess.check_output(["git", "show", "main:textin_pipeline.py"], cwd=ROOT)
        source.write_bytes(content)
        spec = importlib.util.spec_from_file_location("main_textin_pipeline", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.BASE = ROOT
        module.FILES_DESENS_DIR = ROOT / "files_cache_desens"
        return module


def live_main_speed(samples):
    module = load_main_module()
    times, failures = [], 0
    for path, kind in samples:
        started = time.perf_counter()
        try:
            module.parse_file_textin(path, kind)
        except Exception:
            failures += 1
        times.append(time.perf_counter() - started)
    return {"documents": len(samples), "failures": failures,
            "seconds": round(sum(times), 3),
            "seconds_per_document": round(sum(times) / len(times), 3) if times else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-type", type=int, default=2)
    parser.add_argument("--live-main", type=int, default=0,
                        help="Call TextIn for this many sampled documents (may consume quota).")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "offline_ocr_benchmark.json")
    args = parser.parse_args()
    samples = select_samples(args.per_type)
    started = time.perf_counter()
    local_results = local_ocr.parse_files([path for path, _ in samples])
    local_seconds = time.perf_counter() - started
    totals, by_type, confidence, differences = score(samples, local_results)
    report = {
        "method": "deterministic stratified sample; historical TextIn cache is a comparator, not ground truth",
        "sample_documents": len(samples),
        "sample_by_type": dict(sorted((kind, sum(1 for _, value in samples if value == kind))
                                      for kind in {value for _, value in samples})),
        "local_speed": {"seconds": round(local_seconds, 3),
                        "seconds_per_document": round(local_seconds / len(samples), 3)},
        "comparison_totals": totals,
        "comparison_by_type": by_type,
        "confidence_distribution": confidence,
        "difference_categories": differences,
    }
    if args.live_main:
        report["main_live_speed"] = live_main_speed(samples[:args.live_main])
        report["main_live_speed"]["note"] = "network/API latency on a smaller prefix sample"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
