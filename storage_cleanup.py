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


class InvalidTodoId(ValueError):
    """Raised before cleanup when a requested ID is unsafe."""


def _validate_todo_id(todo_id: str) -> str:
    value = str(todo_id)
    if not _SAFE_TODO_ID.fullmatch(value):
        raise InvalidTodoId("待办 ID 格式无效")
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
        if item.is_symlink() or not item.is_file():
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


def _company_cache_keys(mappings, todo_id, display_name):
    entry = mappings["cache_v4.json"].get(todo_id) or {}
    supplier = entry.get("supplier") or {}
    keys = {display_name, supplier.get("full_name"), supplier.get("name"),
            supplier.get("credit_code"), supplier.get("social_credit_code")}
    for filename in _KEYED_JSON_FILES:
        if filename.startswith("stage2_results"):
            keys.add((mappings[filename].get(todo_id) or {}).get("name"))
    return {key.strip() for key in keys if isinstance(key, str) and key.strip()}


def list_expired_supplier_caches(base_dir: Path, *, days: int = RETENTION_DAYS,
                                 now: float | None = None) -> list[dict]:
    """List supplier caches whose most recent activity is older than ``days``."""
    if days < 0:
        raise ValueError("保留天数不能为负数")
    return list_supplier_caches(base_dir, days=days, now=now)


def list_supplier_caches(base_dir: Path, *, days: int | None = None,
                         now: float | None = None) -> list[dict]:
    """Include current and report-only caches when no retention filter is set."""
    base_dir = Path(base_dir).resolve()
    mappings = {name: _read_mapping(base_dir / name) for name in _KEYED_JSON_FILES}
    return _inventory(base_dir, mappings, days=days, now=now)


def _inventory(base_dir, mappings, *, days, now):
    now = time.time() if now is None else now
    cutoff = now - days * 86400 if days is not None else None
    cache = mappings["cache_v4.json"]
    stage2 = {}
    todo_ids = set()
    for filename, mapping in mappings.items():
        # QCC's external company keys are not supplier todo IDs.
        if filename != "qcc_results.json":
            todo_ids.update(mapping)
        if filename.startswith("stage2_results"):
            stage2.update(mapping)
    for dirname in _CACHE_DIRS:
        root = _safe_cache_root(base_dir, dirname)
        if root.exists():
            todo_ids.update(item.name for item in root.iterdir() if item.is_dir() or item.is_symlink())

    rows = []
    for todo_id in todo_ids:
        if not _SAFE_TODO_ID.fullmatch(str(todo_id)):
            continue
        entry = cache.get(todo_id, {})
        newest = _timestamp(entry.get("fetched_at") if isinstance(entry, dict) else None)
        file_count = total_bytes = 0
        for dirname in _CACHE_DIRS:
            try:
                target = _safe_supplier_dir(base_dir, dirname, todo_id)
                count, size, modified = _directory_stats(target)
            except (OSError, CacheCleanupError):
                # Keep the row selectable so cleanup can report its failure.
                continue
            file_count += count
            total_bytes += size
            if modified is not None:
                newest = modified if newest is None else max(newest, modified)
        # A JSON record without a usable per-supplier timestamp cannot be aged safely.
        if cutoff is not None and (newest is None or newest >= cutoff):
            continue
        rows.append({
            "todoId": todo_id,
            "name": _supplier_name(entry, stage2.get(todo_id, {}), todo_id),
            "cachedAt": datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest is not None else None,
            "ageDays": max(0, int((now - newest) // 86400)) if newest is not None else None,
            "fileCount": file_count,
            "sizeBytes": total_bytes,
        })
    rows.sort(key=lambda row: (row["cachedAt"] or "", row["name"], row["todoId"]))
    return rows


def _atomic_write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".cleanup.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _safe_cache_root(base_dir: Path, dirname: str) -> Path:
    root = base_dir / dirname
    if root.is_symlink() or root.resolve() != root:
        raise CacheCleanupError("缓存根目录路径校验失败")
    return root


def _safe_supplier_dir(base_dir: Path, dirname: str, todo_id: str) -> Path:
    target = _safe_cache_root(base_dir, dirname) / todo_id
    if target.is_symlink() or target.resolve() != target:
        raise CacheCleanupError("缓存目录路径校验失败")
    return target


def clean_supplier_cache(base_dir: Path, todo_id: str, *, days: int = RETENTION_DAYS,
                         now: float | None = None) -> dict:
    """Delete one expired supplier's files and keyed JSON records."""
    result = clean_supplier_caches(base_dir, [todo_id], days=days, now=now)
    if result["deleted"]:
        return result["deleted"][0]
    failure = result["failed"][0]
    if failure["reason"] == "not_found":
        raise CacheNotExpired("该供应商缓存不存在、未满 14 天，或已被清理")
    raise CacheCleanupError(failure["msg"])


def clean_supplier_caches(base_dir: Path, todo_ids: list[str] | None, *,
                          days: int | None = None, now: float | None = None) -> dict:
    """Delete selected caches, or the entire inventory when todo_ids is None."""
    if days is not None and days < 0:
        raise ValueError("保留天数不能为负数")
    clean_all = todo_ids is None
    selected = None if clean_all else list(dict.fromkeys(_validate_todo_id(tid) for tid in todo_ids))
    base_dir = Path(base_dir).resolve()
    # Read shared metadata before deleting any directories, and update it once per batch.
    mappings = {name: _read_mapping(base_dir / name) for name in _KEYED_JSON_FILES}
    inventory = {row["todoId"]: row for row in _inventory(base_dir, mappings, days=days, now=now)}
    progress = base_dir / "progress_v4.jsonl"
    progress_lines = progress.read_text(encoding="utf-8").splitlines() if progress.exists() else []
    deleted, failed = [], []
    for todo_id in inventory if clean_all else selected:
        row = inventory.get(todo_id)
        if row is None:
            failed.append({"todoId": todo_id, "reason": "not_found", "msg": "缓存不存在或已清理"})
            continue
        try:
            targets = [_safe_supplier_dir(base_dir, name, todo_id) for name in _CACHE_DIRS]
            for target in targets:
                if target.exists():
                    shutil.rmtree(target)
        except (OSError, CacheCleanupError):
            failed.append({"todoId": todo_id, "name": row["name"], "reason": "cleanup_failed",
                           "msg": "缓存目录未能全部删除，请检查文件占用或目录权限"})
        else:
            deleted.append(row)

    deleted_ids = {row["todoId"] for row in deleted}
    clear_shared = clean_all and not failed
    deleted_keys = set()
    if deleted and not clear_shared:
        remaining_keys = set()
        for row in _inventory(base_dir, mappings, days=None, now=now) if days is not None else inventory.values():
            todo_id = row["todoId"]
            keys = _company_cache_keys(mappings, todo_id, row["name"])
            (deleted_keys if todo_id in deleted_ids else remaining_keys).update(keys)
        deleted_keys -= remaining_keys
    for filename, data in mappings.items():
        keys = deleted_ids
        if filename == "qcc_results.json":
            keys = data.keys() if clear_shared else deleted_ids | deleted_keys
        if data.keys() & keys:
            _atomic_write_json(base_dir / filename, {key: value for key, value in data.items() if key not in keys})

    # The QCC API cache is keyed by supplier name rather than todoId. Its quota
    # counters and the append-only audit database are deliberately retained.
    qcc_cache = base_dir / "qcc_api_cache.sqlite3"
    if qcc_cache.exists():
        from state_store import connect, delete_state_prefix

        if clear_shared:
            with connect(qcc_cache) as db:
                db.execute("DELETE FROM state WHERE namespace = ?", ("qcc",))
        else:
            for key in deleted_keys:
                delete_state_prefix(qcc_cache, "qcc", key + "/")

    # Remove resume markers for this task; malformed lines are preserved verbatim.
    kept = []
    for line in progress_lines:
        try:
            todo_id = str(json.loads(line).get("todoId", ""))
            if todo_id in deleted_ids or (clear_shared and _SAFE_TODO_ID.fullmatch(todo_id)):
                continue
        except (json.JSONDecodeError, AttributeError):
            pass
        kept.append(line)
    if kept != progress_lines:
        temp = progress.with_suffix(progress.suffix + ".cleanup.tmp")
        temp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(temp, progress)

    return {"deleted": deleted, "failed": failed}
