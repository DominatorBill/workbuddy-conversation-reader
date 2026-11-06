"""Read-only discovery of WorkBuddy session index rows and JSONL files."""

from __future__ import annotations

import math
from pathlib import Path
import sqlite3


_FIELDS = (
    "id", "paths", "title", "cwd", "status", "updated_ms", "created_at",
    "automation", "deleted", "indexed", "raw_title", "custom_title",
    "updated_at", "mode", "model", "permission_mode",
)


def _number(value):
    """Return finite numeric metadata, tolerating legacy or unusual SQLite types."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _text(value):
    return value if isinstance(value, str) else None


def _truthy(value):
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def load_catalog(home):
    """Return catalog metadata from ``home`` without opening JSONL contents.

    SQLite is opened with ``mode=ro``. Missing or invalid indexes do not prevent
    discovery of physical JSONL files.
    """
    root = Path(home).expanduser().absolute()
    merged = {}

    def ensure(sid):
        row = merged.get(sid)
        if row is None:
            row = {
                "id": sid, "paths": [], "title": None, "cwd": None,
                "status": None, "updated_ms": None, "created_at": None,
                "automation": False, "deleted": False, "indexed": False,
                "raw_title": None, "custom_title": None, "updated_at": None,
                "mode": None, "model": None, "permission_mode": None,
            }
            merged[sid] = row
        return row

    projects = root / "projects"
    if projects.is_dir():
        try:
            files = projects.glob("*/*.jsonl")
            for path in files:
                if not path.is_file():
                    continue
                sid = path.stem
                if not sid:
                    continue
                ensure(sid)["paths"].append(str(path.absolute()))
        except OSError:
            # A partially unreadable projects tree should not hide index rows.
            pass

    db_path = root / "workbuddy.db"
    index = {"status": "missing", "detail": None}
    if not root.exists() or not db_path.exists():
        index["detail"] = "index database is missing"
    else:
        connection = None
        try:
            connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            if table is None:
                index = {"status": "invalid", "detail": "sessions table is missing"}
            else:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(sessions)")
                }
                if "id" not in columns:
                    index = {"status": "invalid", "detail": "sessions table lacks id column"}
                else:
                    index = {"status": "ok", "detail": None}
                    select_cols = [name for name in (
                        "id", "custom_title", "title", "cwd", "status",
                        "last_activity_at", "updated_at", "created_at",
                        "is_background_automation", "deleted_at", "mode", "model",
                        "permission_mode",
                    ) if name in columns]
                    query = "SELECT " + ", ".join('"' + c + '"' for c in select_cols) + " FROM sessions"
                    for values in connection.execute(query):
                        data = dict(zip(select_cols, values))
                        sid_value = data.get("id")
                        if sid_value is None:
                            continue
                        sid = str(sid_value)
                        row = ensure(sid)
                        row["indexed"] = True
                        raw_title = _text(data.get("title"))
                        custom_title = _text(data.get("custom_title"))
                        row["raw_title"] = raw_title
                        row["custom_title"] = custom_title
                        row["title"] = custom_title or raw_title
                        row["cwd"] = _text(data.get("cwd"))
                        row["status"] = _text(data.get("status"))
                        activity = data.get("last_activity_at")
                        row["updated_ms"] = _number(activity if activity is not None else data.get("updated_at"))
                        row["updated_at"] = _number(data.get("updated_at"))
                        row["created_at"] = _number(data.get("created_at"))
                        row["automation"] = _truthy(data.get("is_background_automation"))
                        row["deleted"] = data.get("deleted_at") is not None
                        row["mode"] = _text(data.get("mode"))
                        row["model"] = _text(data.get("model"))
                        row["permission_mode"] = _text(data.get("permission_mode"))
        except (sqlite3.Error, OSError, ValueError):
            index = {"status": "invalid", "detail": "index database could not be read"}
        finally:
            if connection is not None:
                connection.close()

    sessions = []
    for sid in sorted(merged):
        row = merged[sid]
        row["paths"] = sorted(set(row["paths"]))
        # Enforce the public row contract and avoid accidental extra metadata.
        sessions.append({field: row[field] for field in _FIELDS})
    return {"index": index, "sessions": sessions}
