"""Transactional local state and append-only, hash-chained audit events.

Database owners can replace files: an external immutable archive is required for
protection against a privileged host administrator.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state (namespace TEXT, key TEXT, value TEXT NOT NULL,
            PRIMARY KEY(namespace, key));
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def record_event(path, kind, payload):
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        at = datetime.now(timezone.utc).isoformat()
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        value = hashlib.sha256((previous + at + kind + data).encode()).hexdigest()
        db.execute("INSERT INTO events(at,kind,payload,previous,hash) VALUES(?,?,?,?,?)",
                   (at, kind, data, previous, value))


def claim_operation(path, key):
    with connect(path) as db:
        try:
            db.execute("INSERT INTO operations VALUES (?, 'attempted')", (key,))
        except sqlite3.IntegrityError:
            raise ValueError("该审批已尝试提交；请先人工核实平台状态，禁止自动重试") from None


def put_state(path, namespace, key, value):
    with connect(path) as db:
        db.execute("INSERT OR REPLACE INTO state VALUES(?,?,?)",
                   (namespace, str(key), json.dumps(value, ensure_ascii=False)))


def get_state(path, namespace):
    with connect(path) as db:
        return {key: json.loads(value) for key, value in
                db.execute("SELECT key,value FROM state WHERE namespace=?", (namespace,))}
