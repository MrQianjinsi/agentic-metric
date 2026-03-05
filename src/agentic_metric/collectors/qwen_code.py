"""Qwen Code collector: parse local JSONL files + live process monitoring."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import QWEN_PROJECTS_DIR
from ..models import LiveSession
from ..pricing import estimate_cost
from . import BaseCollector
from ._process import get_running_cwds


# ── Incremental JSONL accumulator ────────────────────────────────────────


class _SessionAccum:
    """Accumulator for incremental parsing of a single Qwen Code session JSONL.

    Token data comes from ``system/ui_telemetry`` entries (``qwen-code.api_response``),
    not from assistant messages.
    """

    __slots__ = (
        "file_path",
        "project_path",
        "session_id",
        "pid",
        "offset",
        "user_turns",
        "message_count",
        "input_tokens",
        "output_tokens",
        "cache_read",
        "first_ts",
        "last_ts",
        "first_prompt",
        "last_prompt",
        "git_branch",
        "model",
    )

    def __init__(self, file_path: Path, project_path: str, pid: int = 0) -> None:
        self.file_path = file_path
        self.project_path = project_path
        self.session_id = file_path.stem
        self.pid = pid
        self.offset = 0
        self.user_turns = 0
        self.message_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.first_ts = ""
        self.last_ts = ""
        self.first_prompt = ""
        self.last_prompt = ""
        self.git_branch = ""
        self.model = ""

    def read_new_lines(self) -> None:
        """Read only bytes appended since last call."""
        try:
            size = self.file_path.stat().st_size
            if size <= self.offset:
                return
            with open(self.file_path, "rb") as f:
                f.seek(self.offset)
                new_data = f.read()
            self.offset = size
        except OSError:
            return

        for raw_line in new_data.split(b"\n"):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            self._process_entry(entry)

    def _process_entry(self, entry: dict) -> None:
        ts = entry.get("timestamp", "")
        if ts:
            if not self.first_ts:
                self.first_ts = ts
            self.last_ts = ts

        if not self.git_branch:
            self.git_branch = entry.get("gitBranch", "")

        entry_type = entry.get("type", "")

        if entry_type == "user":
            self.user_turns += 1
            self.message_count += 1
            msg = entry.get("message", {})
            parts = msg.get("parts", []) if isinstance(msg, dict) else []
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        prompt = text.strip()[:80]
                        if prompt:
                            if not self.first_prompt:
                                self.first_prompt = prompt
                            self.last_prompt = prompt
                        break

        elif entry_type == "assistant":
            self.message_count += 1
            if not self.model:
                self.model = entry.get("model", "")

        elif entry_type == "system" and entry.get("subtype") == "ui_telemetry":
            payload = entry.get("systemPayload", {}).get("uiEvent", {})
            self.input_tokens += payload.get("input_token_count", 0)
            self.output_tokens += payload.get("output_token_count", 0)
            self.cache_read += payload.get("cached_content_token_count", 0)
            if not self.model:
                self.model = payload.get("model", "")

    def to_live_session(self) -> LiveSession:
        return LiveSession(
            session_id=self.session_id,
            agent_type="qwen_code",
            project_path=self.project_path,
            git_branch=self.git_branch,
            model=self.model,
            message_count=self.message_count,
            user_turns=self.user_turns,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read,
            cache_creation_tokens=0,
            started=self.first_ts,
            last_active=self.last_ts,
            first_prompt=self.first_prompt,
            last_prompt=self.last_prompt,
            pid=self.pid,
        )


# ── Live monitor ─────────────────────────────────────────────────────────


class _LiveMonitor:
    """Monitors running Qwen Code sessions with incremental JSONL parsing.

    Uses process detection to find running ``qwen`` processes (Node.js),
    maps their CWDs to QWEN_PROJECTS_DIR subdirectories, then incrementally
    parses the most-recently-modified .jsonl file for each active project.
    """

    def __init__(self) -> None:
        self._cwd_map: dict[str, Path] = {}
        self._cwd_map_built = False
        self._accums: dict[Path, _SessionAccum] = {}

    def refresh(self) -> list[LiveSession]:
        """Return currently running sessions. Fast on repeated calls."""
        pid_cwds: dict[int, str] = get_running_cwds("node.*bin/qwen$", exact=False)
        if not pid_cwds:
            return []

        cwd_set = set(pid_cwds.values())

        if not self._cwd_map_built or not cwd_set.issubset(self._cwd_map.keys()):
            self._build_cwd_map()

        cwd_to_pid: dict[str, int] = {}
        for pid, cwd in pid_cwds.items():
            cwd_to_pid[cwd] = pid

        results: list[LiveSession] = []
        active_files: set[Path] = set()

        for cwd in cwd_set:
            project_dir = self._cwd_map.get(cwd)
            if not project_dir:
                continue

            chats_dir = project_dir / "chats"
            try:
                jsonl_files = sorted(
                    chats_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            if not jsonl_files:
                continue

            latest = jsonl_files[0]
            if latest in active_files:
                continue
            active_files.add(latest)

            accum = self._accums.get(latest)
            if accum is None:
                accum = _SessionAccum(latest, cwd, pid=cwd_to_pid.get(cwd, 0))
                self._accums[latest] = accum
            else:
                accum.pid = cwd_to_pid.get(cwd, accum.pid)

            accum.read_new_lines()
            if accum.user_turns > 0:
                results.append(accum.to_live_session())

        stale = [k for k in self._accums if k not in active_files]
        for k in stale:
            del self._accums[k]

        results.sort(key=lambda s: s.last_active, reverse=True)
        return results

    def _build_cwd_map(self) -> None:
        """Map real CWDs to QWEN_PROJECTS_DIR subdirectories by reading JSONL headers."""
        self._cwd_map.clear()
        if not QWEN_PROJECTS_DIR.exists():
            return
        for project_dir in QWEN_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            try:
                jsonl_files = sorted(
                    chats_dir.glob("*.jsonl"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            if not jsonl_files:
                continue
            real_cwd = self._read_cwd(jsonl_files[0])
            if real_cwd:
                self._cwd_map[real_cwd] = project_dir
        self._cwd_map_built = True

    @staticmethod
    def _read_cwd(jsonl_file: Path) -> str:
        """Extract the cwd field from the first few lines of a JSONL file."""
        try:
            with open(jsonl_file) as f:
                for i, line in enumerate(f):
                    if i > 10:
                        break
                    try:
                        entry = json.loads(line)
                        cwd = entry.get("cwd", "")
                        if cwd:
                            return cwd
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return ""


# ── Collector implementation ─────────────────────────────────────────────


class QwenCodeCollector(BaseCollector):
    """Collector for Qwen Code agent data.

    - Live sessions: process detection + incremental JSONL parsing
    - History sync: walks ~/.qwen/projects/*/chats/*.jsonl
    """

    agent_type = "qwen_code"

    def __init__(self) -> None:
        self._monitor = _LiveMonitor()

    def get_live_sessions(self) -> list[LiveSession]:
        return self._monitor.refresh()

    def sync_history(self, db) -> None:
        self._sync_jsonl(db)
        db.commit()

    def _sync_jsonl(self, db) -> None:
        """Scan .jsonl files for per-session token data (incremental via sync_state)."""
        if not QWEN_PROJECTS_DIR.exists():
            return

        sync_prefix = "qc_jsonl:"

        for project_dir in QWEN_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue

            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue

            try:
                jsonl_files = list(chats_dir.glob("*.jsonl"))
            except OSError:
                continue

            for jsonl_file in jsonl_files:
                sync_key = f"{sync_prefix}{jsonl_file}"
                prev_offset_str = db.get_sync_state(sync_key)
                prev_offset = int(prev_offset_str) if prev_offset_str else 0

                try:
                    file_size = jsonl_file.stat().st_size
                except OSError:
                    continue

                if file_size <= prev_offset:
                    continue

                accum = _SessionAccum(jsonl_file, project_path=str(project_dir))
                accum.read_new_lines()

                if accum.user_turns == 0:
                    db.set_sync_state(sync_key, str(file_size))
                    continue

                cost = estimate_cost(
                    accum.model,
                    input_tokens=accum.input_tokens,
                    output_tokens=accum.output_tokens,
                    cache_read_tokens=accum.cache_read,
                )

                real_cwd = _LiveMonitor._read_cwd(jsonl_file)
                project_path = real_cwd if real_cwd else str(project_dir)

                db.upsert_session(
                    accum.session_id,
                    self.agent_type,
                    project_path=project_path,
                    git_branch=accum.git_branch,
                    model=accum.model,
                    message_count=accum.message_count,
                    user_turns=accum.user_turns,
                    input_tokens=accum.input_tokens,
                    output_tokens=accum.output_tokens,
                    cache_read_tokens=accum.cache_read,
                    cache_creation_tokens=0,
                    estimated_cost_usd=cost,
                    started_at=accum.first_ts,
                    ended_at=accum.last_ts,
                    first_prompt=accum.first_prompt,
                    last_prompt=accum.last_prompt,
                )

                db.set_sync_state(sync_key, str(file_size))
