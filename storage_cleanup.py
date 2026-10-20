"""Per-supplier cache inventory and safe cleanup for the Web UI."""

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

RETENTION_DAYS = 14
_SAFE_TODO_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_CACHE_DIRS = ("files_cache", "files_cache_desens")
_KEYED_JSON_FILES = (
    "cache_v4.json",
    "textin_results.json",
    "textin_results.online.json",
    "textin_results.offline.json",
    "stage2_results.json",
    "stage2_results.online.json",
    "stage2_results.offline.json",
    "qcc_results.json",
)


class CacheCleanupError(RuntimeError):
    """Base error for cache cleanup operations."""


class CacheNotExpired(CacheCleanupError):
    """Raised when a supplier is absent or no longer eligible for cleanup."""


def _validate_todo_id(todo_id: str) -> str:
    value = str(todo_id)
    if not _SAFE_TODO_ID.fullmatch(value):
        raise ValueError("待办 ID 格式无效")
    return value


def _read_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CacheCleanupError(f"缓存文件格式异常：{path.name}")
    return data


def _timestamp(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def _directory_stats(path: Path) -> tuple[int, int, float | None]:
    """Return file count, bytes and newest mtime without following outside links."""
    if not path.exists() or not path.is_dir():
        return 0, 0, None
    count = size = 0
    newest = path.stat().st_mtime
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        stat = item.stat()
        count += 1
        size += stat.st_size
        newest = max(newest, stat.st_mtime)
    return count, size, newest


def _supplier_name(entry: dict, stage2_entry: dict, todo_id: str) -> str:
    supplier = entry.get("supplier", {}) if isinstance(entry, dict) else {}
    todo = entry.get("todo", {}) if isinstance(entry, dict) else {}
    candidates = (
        supplier.get("name"),
        stage2_entry.get("name") if isinstance(stage2_entry, dict) else None,
        todo.get("applyUnitName"),
        todo.get("title"),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate).split("/")[0].strip()
    return f"未知供应商（{todo_id}）"


def list_expired_supplier_caches(base_dir: Path, *, days: int = RETENTION_DAYS,
                                 now: float | None = None) -> list[dict]:
    """List supplier caches whose most recent activity is older than ``days``."""
    if days < 0:
        raise ValueError("保留天数不能为负数")
    base_dir = Path(base_dir).resolve()
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    cache = _read_mapping(base_dir / "cache_v4.json")
    stage2 = _read_mapping(base_dir / "stage2_results.json")
    todo_ids = set(cache)
    for dirname in _CACHE_DIRS:
        root = base_dir / dirname
        if root.exists():
            todo_ids.update(item.name for item in root.iterdir()
                            if item.is_dir() and _SAFE_TODO_ID.fullmatch(item.name))

    rows = []
    for todo_id in todo_ids:
        if not _SAFE_TODO_ID.fullmatch(str(todo_id)):
            continue
        entry = cache.get(todo_id, {})
        newest = _timestamp(entry.get("fetched_at") if isinstance(entry, dict) else None)
        file_count = total_bytes = 0
        for dirname in _CACHE_DIRS:
            count, size, modified = _directory_stats(base_dir / dirname / todo_id)
            file_count += count
            total_bytes += size
            if modified is not None:
                newest = modified if newest is None else max(newest, modified)
        # A JSON record without a usable per-supplier timestamp cannot be aged safely.
        if newest is None or newest >= cutoff:
            continue
        rows.append({
            "todoId": todo_id,
            "name": _supplier_name(entry, stage2.get(todo_id, {}), todo_id),
            "cachedAt": datetime.fromtimestamp(newest).isoformat(timespec="seconds"),
            "ageDays": int((now - newest) // 86400),
            "fileCount": file_count,
            "sizeBytes": total_bytes,
        })
    rows.sort(key=lambda row: (row["cachedAt"], row["name"]))
    return rows


def _atomic_write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".cleanup.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _safe_supplier_dir(base_dir: Path, dirname: str, todo_id: str) -> Path:
    root = (base_dir / dirname).resolve()
    target = base_dir / dirname / todo_id
    if target.exists() and (target.resolve().parent != root or target.is_symlink()):
        raise CacheCleanupError("缓存目录路径校验失败")
    return target


def clean_supplier_cache(base_dir: Path, todo_id: str, *, days: int = RETENTION_DAYS,
                         now: float | None = None) -> dict:
    """Delete one expired supplier's files and keyed JSON records."""
    todo_id = _validate_todo_id(todo_id)
    base_dir = Path(base_dir).resolve()
    eligible = {row["todoId"]: row for row in
                list_expired_supplier_caches(base_dir, days=days, now=now)}
    if todo_id not in eligible:
        raise CacheNotExpired("该供应商缓存不存在、未满 14 天，或已被清理")
    row = eligible[todo_id]

    # Parse every mutable JSON file before deleting anything, so corruption causes
    # a clean abort rather than a half-finished cleanup.
    json_updates = []
    for filename in _KEYED_JSON_FILES:
        path = base_dir / filename
        if not path.exists():
            continue
        data = _read_mapping(path)
        if todo_id in data:
            data.pop(todo_id)
            json_updates.append((path, data))

    targets = [_safe_supplier_dir(base_dir, dirname, todo_id) for dirname in _CACHE_DIRS]
    for target in targets:
        if target.exists():
            shutil.rmtree(target)
    for path, data in json_updates:
        _atomic_write_json(path, data)

    # The QCC API cache is keyed by supplier name rather than todoId. Its quota
    # counters and the append-only audit database are deliberately retained.
    qcc_cache = base_dir / "qcc_api_cache.sqlite3"
    if qcc_cache.exists():
        from state_store import delete_state_prefix

        delete_state_prefix(qcc_cache, "qcc", row["name"] + "/")

    # Remove resume markers for this task; malformed lines are preserved verbatim.
    progress = base_dir / "progress_v4.jsonl"
    if progress.exists():
        kept = []
        for line in progress.read_text(encoding="utf-8").splitlines():
            try:
                if str(json.loads(line).get("todoId")) == todo_id:
                    continue
            except (json.JSONDecodeError, AttributeError):
                pass
            kept.append(line)
        temp = progress.with_suffix(progress.suffix + ".cleanup.tmp")
        temp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(temp, progress)

    return row
