"""Tests for the OpenCode collector's schema handling.

opencode 1.19 moved sessions and messages into ``session_v2`` /
``session_message`` and froze the legacy tables. The collector has to detect
which layout a database uses and read the right one.
"""

import json
import sqlite3
import tempfile
import time

import pytest

from agentic_metric.collectors import opencode as opencode_mod
from agentic_metric.collectors.opencode import (
    OpenCodeCollector,
    _detect_schema,
    _model_id,
)
from agentic_metric.store.database import Database


# ── Fixtures ────────────────────────────────────────────────────────


def _make_db() -> Database:
    """Create a temporary database for the collector to write into."""
    tmp = tempfile.mktemp(suffix=".db")
    return Database(db_path=tmp)


def _make_opencode_db(tmp_path) -> sqlite3.Connection:
    """Create an opencode database using the 1.19+ (v2) layout."""
    path = tmp_path / "opencode.db"
    src = sqlite3.connect(path)
    src.executescript(
        """
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            model TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            time_created INTEGER,
            time_updated INTEGER,
            time_archived INTEGER
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            type TEXT,
            seq INTEGER,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        """
    )
    return src


def _add_v2_session(
    src: sqlite3.Connection,
    session_id: str,
    *,
    title: str = "a session",
    directory: str = "/home/user/project",
    model: str = '{"id": "glm-5.3-flash", "providerID": "zhipuai-coding-plan"}',
    tokens_input: int = 1000,
    tokens_output: int = 500,
    tokens_reasoning: int = 200,
    tokens_cache_read: int = 3000,
    tokens_cache_write: int = 400,
    user_messages: int = 1,
    assistant_messages: int = 2,
) -> None:
    # Recent enough to fall inside the collector's 30-minute live window.
    now_ms = int(time.time() * 1000)
    created_ms = now_ms - 60_000
    src.execute(
        """INSERT INTO session_v2
           (id, title, directory, model, cost, tokens_input, tokens_output,
            tokens_reasoning, tokens_cache_read, tokens_cache_write,
            time_created, time_updated, time_archived)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            session_id,
            title,
            directory,
            model,
            tokens_input,
            tokens_output,
            tokens_reasoning,
            tokens_cache_read,
            tokens_cache_write,
            created_ms,
            now_ms,
        ),
    )
    seq = 0
    for i in range(user_messages):
        seq += 1
        src.execute(
            """INSERT INTO session_message
               (id, session_id, type, seq, time_created, time_updated, data)
               VALUES (?, ?, 'user', ?, ?, ?, ?)""",
            (
                f"{session_id}-u{i}",
                session_id,
                seq,
                created_ms,
                now_ms,
                json.dumps({"time": {"created": created_ms}, "text": f"prompt {i}"}),
            ),
        )
    for i in range(assistant_messages):
        seq += 1
        src.execute(
            """INSERT INTO session_message
               (id, session_id, type, seq, time_created, time_updated, data)
               VALUES (?, ?, 'assistant', ?, ?, ?, ?)""",
            (
                f"{session_id}-a{i}",
                session_id,
                seq,
                created_ms,
                now_ms,
                json.dumps({"time": {"created": created_ms}, "tokens": {}}),
            ),
        )
    src.commit()


# ── Schema detection ────────────────────────────────────────────────


def test_detect_schema_v2(tmp_path):
    src = _make_opencode_db(tmp_path)
    assert _detect_schema(src) == "v2"
    src.close()


def test_detect_schema_legacy(tmp_path):
    path = tmp_path / "old.db"
    src = sqlite3.connect(path)
    src.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    src.commit()
    assert _detect_schema(src) == "legacy"
    src.close()


def test_detect_schema_empty_database(tmp_path):
    path = tmp_path / "empty.db"
    src = sqlite3.connect(path)
    assert _detect_schema(src) == "legacy"
    src.close()


# ── Model normalization ─────────────────────────────────────────────


def test_model_id_from_json_object():
    raw = '{"id": "glm-5.3-flash", "providerID": "zhipuai-coding-plan"}'
    assert _model_id(raw) == "glm-5.3-flash"


def test_model_id_from_plain_string():
    assert _model_id("glm-5.3-flash") == "glm-5.3-flash"


def test_model_id_none_and_empty():
    assert _model_id(None) == ""
    assert _model_id("") == ""
    assert _model_id("   ") == ""


def test_model_id_malformed_json_is_kept_verbatim():
    assert _model_id('{"id": "broken') == '{"id": "broken'


# ── History sync ────────────────────────────────────────────────────


def test_sync_v2_reads_token_counters(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc")
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")

    db = _make_db()
    collector = OpenCodeCollector()
    collector._sync_sessions(db)
    db.commit()

    row = db.conn.execute(
        "SELECT * FROM sessions WHERE session_id = 'opencode-ses_abc'"
    ).fetchone()
    assert row is not None
    assert row["agent_type"] == "opencode"
    assert row["input_tokens"] == 1000
    # reasoning tokens are billed as output
    assert row["output_tokens"] == 700
    assert row["cache_read_tokens"] == 3000
    assert row["cache_creation_tokens"] == 400
    assert row["model"] == "glm-5.3-flash"
    assert row["project_path"] == "/home/user/project"
    db.close()


def test_sync_v2_counts_messages_and_user_turns(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc", user_messages=3, assistant_messages=5)
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")

    db = _make_db()
    collector = OpenCodeCollector()
    collector._sync_sessions(db)
    db.commit()

    row = db.conn.execute(
        "SELECT * FROM sessions WHERE session_id = 'opencode-ses_abc'"
    ).fetchone()
    assert row["message_count"] == 8
    assert row["user_turns"] == 3
    db.close()


def test_sync_v2_keeps_first_and_last_prompt(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc", user_messages=2)
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")

    db = _make_db()
    collector = OpenCodeCollector()
    collector._sync_sessions(db)
    db.commit()

    row = db.conn.execute(
        "SELECT * FROM sessions WHERE session_id = 'opencode-ses_abc'"
    ).fetchone()
    assert row["first_prompt"] == "prompt 0"
    assert row["last_prompt"] == "prompt 1"
    db.close()


def test_sync_v2_skips_messages_with_malformed_json(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc", user_messages=1)
    src.execute(
        """INSERT INTO session_message
           (id, session_id, type, seq, time_created, time_updated, data)
           VALUES ('bad', 'ses_abc', 'user', 99, 1000, 2000, 'not json')"""
    )
    src.commit()
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")

    db = _make_db()
    collector = OpenCodeCollector()
    collector._sync_sessions(db)
    db.commit()

    row = db.conn.execute(
        "SELECT * FROM sessions WHERE session_id = 'opencode-ses_abc'"
    ).fetchone()
    assert row is not None
    assert row["first_prompt"] == "prompt 0"
    db.close()


def test_sync_v2_upserts_all_sessions(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    for i in range(5):
        _add_v2_session(src, f"ses_{i}")
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")

    db = _make_db()
    collector = OpenCodeCollector()
    collector._sync_sessions(db)
    db.commit()

    count = db.conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE agent_type = 'opencode'"
    ).fetchone()[0]
    assert count == 5
    db.close()


# ── Live sessions ───────────────────────────────────────────────────


def test_live_sessions_v2(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc")
    src.commit()
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")
    monkeypatch.setattr(
        opencode_mod, "get_running_cwds", lambda name, exact=True: {123: "/home/user/project"}
    )

    collector = OpenCodeCollector()
    sessions = collector.get_live_sessions()

    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "opencode-ses_abc"
    assert s.agent_type == "opencode"
    assert s.pid == 123
    assert s.project_path == "/home/user/project"
    assert s.model == "glm-5.3-flash"
    assert s.input_tokens == 1000
    assert s.output_tokens == 700
    assert s.cache_read_tokens == 3000
    assert s.cache_creation_tokens == 400
    assert s.user_turns == 1
    assert s.message_count == 3


def test_live_sessions_v2_skips_archived(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc")
    src.execute("UPDATE session_v2 SET time_archived = 3000 WHERE id = 'ses_abc'")
    src.commit()
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")
    monkeypatch.setattr(
        opencode_mod, "get_running_cwds", lambda name, exact=True: {123: "/home/user/project"}
    )

    assert OpenCodeCollector().get_live_sessions() == []


def test_live_sessions_v2_skips_other_directories(tmp_path, monkeypatch):
    src = _make_opencode_db(tmp_path)
    _add_v2_session(src, "ses_abc")
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", tmp_path / "opencode.db")
    monkeypatch.setattr(
        opencode_mod, "get_running_cwds", lambda name, exact=True: {123: "/somewhere/else"}
    )

    assert OpenCodeCollector().get_live_sessions() == []


# ── Legacy layout still works ───────────────────────────────────────


def test_sync_legacy_layout_still_works(tmp_path, monkeypatch):
    """The pre-1.19 tables must keep working after the v2 change."""
    path = tmp_path / "opencode.db"
    src = sqlite3.connect(path)
    src.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            time_archived INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        """
    )
    src.execute(
        "INSERT INTO session VALUES ('ses_old', 'old session', '/home/user/old', 1000, 2000, NULL)"
    )
    src.execute(
        """INSERT INTO message VALUES ('m1', 'ses_old', 1000, 1100, ?)""",
        (
            json.dumps(
                {
                    "role": "assistant",
                    "modelID": "glm-5.3-flash",
                    "tokens": {"input": 900, "output": 300, "reasoning": 100},
                }
            ),
        ),
    )
    src.execute(
        """INSERT INTO message VALUES ('m2', 'ses_old', 1100, 1200, ?)""",
        (json.dumps({"role": "user"}),),
    )
    src.execute(
        """INSERT INTO part VALUES ('p1', 'm2', 'ses_old', 1100, 1200, ?)""",
        (json.dumps({"type": "text", "text": "hello"}),),
    )
    src.commit()
    src.close()

    monkeypatch.setattr(opencode_mod, "OPENCODE_DB", path)

    db = _make_db()
    collector = OpenCodeCollector()
    collector._sync_sessions(db)
    db.commit()

    row = db.conn.execute(
        "SELECT * FROM sessions WHERE session_id = 'opencode-ses_old'"
    ).fetchone()
    assert row is not None
    assert row["model"] == "glm-5.3-flash"
    assert row["input_tokens"] == 900
    assert row["output_tokens"] == 400
    assert row["user_turns"] == 1
    assert row["first_prompt"] == "hello"
    db.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
