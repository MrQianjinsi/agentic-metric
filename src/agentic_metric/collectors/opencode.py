"""OpenCode agent collector."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from . import BaseCollector
from ..config import OPENCODE_DB
from ..models import LiveSession
from ..pricing import estimate_cost
from ._process import get_running_cwds

# Sessions updated more than this many seconds ago are not considered active.
_ACTIVE_THRESHOLD_SECS = 1800  # 30 minutes


def _ms_to_iso(ms: int | None) -> str:
    """Convert millisecond timestamp to ISO 8601 string."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _detect_schema(src: sqlite3.Connection) -> str:
    """Return "v2" when opencode uses the session_v2 tables, "legacy" otherwise.

    opencode 1.19 moved sessions and messages into ``session_v2`` and
    ``session_message`` and left the old ``session`` / ``message`` / ``part``
    tables frozen at the migration point. Reading only the old tables silently
    drops every session created after the migration.
    """
    try:
        row = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_v2'"
        ).fetchone()
    except sqlite3.Error:
        return "legacy"
    return "v2" if row else "legacy"


def _model_id(raw) -> str:
    """Normalize the model column into a bare model id.

    The v2 schema stores a JSON object (``{"id": ..., "providerID": ...}``)
    while the legacy layout stored a plain string. Anything that is not a JSON
    object is returned verbatim so custom model names survive.
    """
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    text = str(raw).strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return text
        if isinstance(obj, dict):
            return str(obj.get("id") or obj.get("modelID") or "")
    return text


class OpenCodeCollector(BaseCollector):
    """Collect session data from OpenCode's SQLite database."""

    @property
    def agent_type(self) -> str:
        return "opencode"

    def get_live_sessions(self) -> list[LiveSession]:
        """Detect running opencode processes and return live sessions.

        A session is considered active only when:
        1. Its directory matches the CWD of a running opencode process.
        2. It has been updated within the last 30 minutes.
        """
        pid_cwds = get_running_cwds(".opencode", exact=False)
        if not pid_cwds or not OPENCODE_DB.exists():
            return []

        # Build {cwd: pid} mapping for directory lookups
        cwd_to_pid: dict[str, int] = {}
        for pid, cwd in pid_cwds.items():
            cwd_to_pid[cwd] = pid

        try:
            src = sqlite3.connect(
                f"file:{OPENCODE_DB}?mode=ro", uri=True
            )
        except sqlite3.OperationalError:
            return []

        sessions: list[LiveSession] = []
        try:
            src.row_factory = sqlite3.Row
            cutoff_ms = int((time.time() - _ACTIVE_THRESHOLD_SECS) * 1000)
            if _detect_schema(src) == "v2":
                sessions = self._live_sessions_v2(src, cwd_to_pid, cutoff_ms)
            else:
                sessions = self._live_sessions_legacy(src, cwd_to_pid, cutoff_ms)
        finally:
            src.close()

        return sessions

    def _live_sessions_legacy(
        self,
        src: sqlite3.Connection,
        cwd_to_pid: dict[str, int],
        cutoff_ms: int,
    ) -> list[LiveSession]:
        """Live sessions from the pre-1.19 `session` / `message` / `part` tables."""
        sessions: list[LiveSession] = []
        rows = src.execute(
            """SELECT s.id, s.title, s.directory, s.time_created, s.time_updated
               FROM session s
               WHERE s.time_archived IS NULL
                 AND s.time_updated >= ?
               ORDER BY s.time_updated DESC""",
            (cutoff_ms,),
        ).fetchall()

        seen_dirs: set[str] = set()
        for row in rows:
            directory = row["directory"] or ""
            if not directory or directory in seen_dirs:
                continue

            # Match session directory against running process CWDs
            pid = cwd_to_pid.get(directory)
            if pid is None:
                continue
            seen_dirs.add(directory)

            # Get latest model from assistant messages
            msg = src.execute(
                """SELECT json_extract(data, '$.modelID') AS model
                   FROM message
                   WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
                   ORDER BY time_created DESC LIMIT 1""",
                (row["id"],),
            ).fetchone()

            model = (msg["model"] or "") if msg else ""

            # Count user turns and total messages
            user_turns = src.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ? AND json_extract(data, '$.role') = 'user'",
                (row["id"],),
            ).fetchone()[0]
            message_count = src.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?",
                (row["id"],),
            ).fetchone()[0]

            # Aggregate tokens across all assistant messages
            agg = src.execute(
                """SELECT
                       SUM(json_extract(data, '$.tokens.input')) AS inp,
                       SUM(json_extract(data, '$.tokens.output')
                           + json_extract(data, '$.tokens.reasoning')) AS outp,
                       SUM(json_extract(data, '$.tokens.cache.read')) AS cread,
                       SUM(json_extract(data, '$.tokens.cache.write')) AS cwrite
                   FROM message
                   WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'""",
                (row["id"],),
            ).fetchone()

            input_tokens = (agg["inp"] or 0) if agg else 0
            output_tokens = (agg["outp"] or 0) if agg else 0
            cache_read = (agg["cread"] or 0) if agg else 0
            cache_write = (agg["cwrite"] or 0) if agg else 0

            # Get first and last user prompt text
            prompt_rows = src.execute(
                """SELECT json_extract(p.data, '$.text') AS text
                   FROM part p
                   JOIN message m ON p.message_id = m.id
                   WHERE m.session_id = ? AND json_extract(m.data, '$.role') = 'user'
                         AND json_extract(p.data, '$.type') = 'text'
                   ORDER BY m.time_created""",
                (row["id"],),
            ).fetchall()
            first_prompt = ""
            last_prompt = ""
            for pr in prompt_rows:
                text = (pr["text"] or "").strip()
                if not text:
                    continue
                if not first_prompt:
                    first_prompt = text[:80]
                last_prompt = text[:80]

            sessions.append(
                LiveSession(
                    session_id=f"opencode-{row['id']}",
                    agent_type="opencode",
                    pid=pid,
                    project_path=directory,
                    model=model,
                    message_count=message_count,
                    user_turns=user_turns,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_write,
                    started=_ms_to_iso(row["time_created"]),
                    last_active=_ms_to_iso(row["time_updated"]),
                    first_prompt=first_prompt,
                    last_prompt=last_prompt,
                )
            )

        return sessions

    def _live_sessions_v2(
        self,
        src: sqlite3.Connection,
        cwd_to_pid: dict[str, int],
        cutoff_ms: int,
    ) -> list[LiveSession]:
        """Live sessions from the opencode >= 1.19 `session_v2` tables."""
        sessions: list[LiveSession] = []
        rows = src.execute(
            """SELECT id, title, directory, time_created, time_updated, model,
                      tokens_input, tokens_output, tokens_reasoning,
                      tokens_cache_read, tokens_cache_write
               FROM session_v2
               WHERE time_archived IS NULL
                 AND time_updated >= ?
               ORDER BY time_updated DESC""",
            (cutoff_ms,),
        ).fetchall()

        seen_dirs: set[str] = set()
        for row in rows:
            directory = row["directory"] or ""
            if not directory or directory in seen_dirs:
                continue

            # Match session directory against running process CWDs
            pid = cwd_to_pid.get(directory)
            if pid is None:
                continue
            seen_dirs.add(directory)

            message_count, user_turns, first_prompt, last_prompt = self._v2_message_stats(
                src, row["id"]
            )

            sessions.append(
                LiveSession(
                    session_id=f"opencode-{row['id']}",
                    agent_type="opencode",
                    pid=pid,
                    project_path=directory,
                    model=_model_id(row["model"]),
                    message_count=message_count,
                    user_turns=user_turns,
                    input_tokens=row["tokens_input"] or 0,
                    # reasoning tokens are billed as output
                    output_tokens=(row["tokens_output"] or 0)
                    + (row["tokens_reasoning"] or 0),
                    cache_read_tokens=row["tokens_cache_read"] or 0,
                    cache_creation_tokens=row["tokens_cache_write"] or 0,
                    started=_ms_to_iso(row["time_created"]),
                    last_active=_ms_to_iso(row["time_updated"]),
                    first_prompt=first_prompt,
                    last_prompt=last_prompt,
                )
            )

        return sessions

    @staticmethod
    def _v2_message_stats(
        src: sqlite3.Connection, session_id: str
    ) -> tuple[int, int, str, str]:
        """Return (message_count, user_turns, first_prompt, last_prompt)."""
        counts = src.execute(
            "SELECT type, COUNT(*) AS n FROM session_message WHERE session_id = ? GROUP BY type",
            (session_id,),
        ).fetchall()
        message_count = sum(r["n"] for r in counts)
        user_turns = next((r["n"] for r in counts if r["type"] == "user"), 0)

        first_prompt = ""
        last_prompt = ""
        for r in src.execute(
            """SELECT CASE WHEN json_valid(data) THEN json_extract(data, '$.text') END AS text
               FROM session_message
               WHERE session_id = ? AND type = 'user'
               ORDER BY time_created""",
            (session_id,),
        ):
            text = (r["text"] or "").strip()
            if not text:
                continue
            if not first_prompt:
                first_prompt = text[:80]
            last_prompt = text[:80]

        return message_count, user_turns, first_prompt, last_prompt

    def sync_history(self, db) -> None:
        """Sync OpenCode sessions from opencode.db into our database."""
        if not OPENCODE_DB.exists():
            return

        try:
            mtime = str(OPENCODE_DB.stat().st_mtime)
        except OSError:
            return
        prev_mtime = db.get_sync_state("opencode_db_mtime")
        if prev_mtime == mtime:
            return

        self._sync_sessions(db)
        db.commit()
        db.set_sync_state("opencode_db_mtime", mtime)

    def _sync_sessions(self, db) -> None:
        """Read sessions + messages from opencode.db and upsert."""
        try:
            src = sqlite3.connect(
                f"file:{OPENCODE_DB}?mode=ro", uri=True
            )
        except sqlite3.OperationalError:
            return

        try:
            src.row_factory = sqlite3.Row
            if _detect_schema(src) == "v2":
                self._sync_sessions_v2(db, src)
            else:
                self._sync_sessions_legacy(db, src)
        finally:
            src.close()

    def _sync_sessions_legacy(self, db, src: sqlite3.Connection) -> None:
        """Sync sessions using the pre-1.19 `session` / `message` / `part` tables."""
        # Fetch all sessions
        sessions = src.execute(
            """SELECT s.id, s.title, s.directory, s.time_created, s.time_updated
               FROM session s"""
        ).fetchall()

        # Fetch all assistant messages aggregated per session
        # (model, tokens, cost, timestamps)
        msg_rows = src.execute(
            """SELECT
                   session_id,
                   json_extract(data, '$.role') AS role,
                   json_extract(data, '$.modelID') AS model,
                   json_extract(data, '$.tokens.input') AS inp,
                   json_extract(data, '$.tokens.output') AS outp,
                   json_extract(data, '$.tokens.reasoning') AS reasoning,
                   json_extract(data, '$.tokens.cache.read') AS cread,
                   json_extract(data, '$.tokens.cache.write') AS cwrite,
                   json_extract(data, '$.cost') AS cost,
                   json_extract(data, '$.time.created') AS tcreated,
                   json_extract(data, '$.time.completed') AS tcompleted
               FROM message"""
        ).fetchall()

        # Fetch user prompt text from parts
        part_rows = src.execute(
            """SELECT p.session_id, m.time_created AS msg_time,
                   json_extract(p.data, '$.text') AS text
               FROM part p
               JOIN message m ON p.message_id = m.id
               WHERE json_extract(m.data, '$.role') = 'user'
                     AND json_extract(p.data, '$.type') = 'text'
               ORDER BY m.time_created"""
        ).fetchall()

        # Build per-session message aggregates
        session_agg: dict[str, dict] = {}
        for mr in msg_rows:
            sid = mr["session_id"]
            agg = session_agg.get(sid)
            if agg is None:
                agg = {
                    "models": set(),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cost": 0.0,
                    "msg_count": 0,
                    "user_turns": 0,
                    "first_ts": None,
                    "last_ts": None,
                }
                session_agg[sid] = agg

            role = mr["role"]
            if role == "user":
                agg["user_turns"] += 1
                agg["msg_count"] += 1
            elif role == "assistant":
                agg["msg_count"] += 1
                model = mr["model"] or ""
                if model:
                    agg["models"].add(model)
                agg["input_tokens"] += mr["inp"] or 0
                # reasoning tokens are billed as output
                agg["output_tokens"] += (mr["outp"] or 0) + (mr["reasoning"] or 0)
                agg["cache_read"] += mr["cread"] or 0
                agg["cache_write"] += mr["cwrite"] or 0
                agg["cost"] += mr["cost"] or 0.0

            ts = mr["tcreated"]
            if ts:
                if agg["first_ts"] is None or ts < agg["first_ts"]:
                    agg["first_ts"] = ts
                if agg["last_ts"] is None or ts > agg["last_ts"]:
                    agg["last_ts"] = ts

        # Build per-session prompt text
        session_prompts: dict[str, dict] = {}
        for pr in part_rows:
            sid = pr["session_id"]
            text = (pr["text"] or "").strip()
            if not text:
                continue
            sp = session_prompts.get(sid)
            if sp is None:
                sp = {"first": "", "last": ""}
                session_prompts[sid] = sp
            if not sp["first"]:
                sp["first"] = text[:80]
            sp["last"] = text[:80]

        for sess in sessions:
            sid = sess["id"]
            session_id = f"opencode-{sid}"
            agg = session_agg.get(sid, {})

            started_at = _ms_to_iso(sess["time_created"])
            ended_at = _ms_to_iso(sess["time_updated"])

            models = agg.get("models", set())
            model = next(iter(models)) if models else ""

            input_tokens = agg.get("input_tokens", 0)
            output_tokens = agg.get("output_tokens", 0)
            cache_read = agg.get("cache_read", 0)
            cache_write = agg.get("cache_write", 0)
            raw_cost = agg.get("cost", 0.0)

            # Use reported cost if available, otherwise estimate
            cost = raw_cost if raw_cost > 0 else estimate_cost(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_write,
            )

            sp = session_prompts.get(sid, {})
            first_prompt = sp.get("first", "")
            last_prompt = sp.get("last", "")
            title = sess["title"] or ""

            db.upsert_session(
                session_id,
                self.agent_type,
                project_path=sess["directory"] or "",
                model=model,
                message_count=agg.get("msg_count", 0),
                user_turns=agg.get("user_turns", 0),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_write,
                estimated_cost_usd=cost,
                started_at=started_at,
                ended_at=ended_at,
                first_prompt=first_prompt or title[:80],
                last_prompt=last_prompt,
                summary=title,
            )

    def _sync_sessions_v2(self, db, src: sqlite3.Connection) -> None:
        """Sync sessions using the opencode >= 1.19 `session_v2` tables.

        Unlike the legacy layout, the v2 schema keeps the token counters
        already aggregated on the session row, so this path avoids scanning
        and JSON-decoding every message.
        """
        sessions = src.execute(
            """SELECT id, title, directory, time_created, time_updated, model,
                      tokens_input, tokens_output, tokens_reasoning,
                      tokens_cache_read, tokens_cache_write
               FROM session_v2"""
        ).fetchall()

        # Message counts per session, keyed by message type
        counts: dict[str, dict[str, int]] = {}
        for r in src.execute(
            "SELECT session_id, type, COUNT(*) AS n FROM session_message GROUP BY session_id, type"
        ):
            counts.setdefault(r["session_id"], {})[r["type"]] = r["n"]

        # First and last user prompt per session
        session_prompts: dict[str, dict[str, str]] = {}
        for r in src.execute(
            """SELECT session_id,
                      CASE WHEN json_valid(data) THEN json_extract(data, '$.text') END AS text
               FROM session_message
               WHERE type = 'user'
               ORDER BY time_created"""
        ):
            sid = r["session_id"]
            text = (r["text"] or "").strip()
            if not text:
                continue
            sp = session_prompts.get(sid)
            if sp is None:
                sp = {"first": "", "last": ""}
                session_prompts[sid] = sp
            if not sp["first"]:
                sp["first"] = text[:80]
            sp["last"] = text[:80]

        for sess in sessions:
            sid = sess["id"]
            model = _model_id(sess["model"])

            input_tokens = sess["tokens_input"] or 0
            # reasoning tokens are billed as output
            output_tokens = (sess["tokens_output"] or 0) + (sess["tokens_reasoning"] or 0)
            cache_read = sess["tokens_cache_read"] or 0
            cache_write = sess["tokens_cache_write"] or 0

            cost = estimate_cost(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_write,
            )

            cnt = counts.get(sid, {})
            sp = session_prompts.get(sid, {})
            title = sess["title"] or ""

            db.upsert_session(
                f"opencode-{sid}",
                self.agent_type,
                project_path=sess["directory"] or "",
                model=model,
                message_count=sum(cnt.values()),
                user_turns=cnt.get("user", 0),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_write,
                estimated_cost_usd=cost,
                started_at=_ms_to_iso(sess["time_created"]),
                ended_at=_ms_to_iso(sess["time_updated"]),
                first_prompt=sp.get("first") or title[:80],
                last_prompt=sp.get("last", ""),
                summary=title,
            )
