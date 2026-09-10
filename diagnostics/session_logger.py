from __future__ import annotations

import os
import json
import uuid
import re
import sys
import psutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _timestamp() -> str:
    """Return a sortable UTC timestamp with an explicit ISO-8601 suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _get_rss() -> int:
    """Get the current process RSS memory in bytes."""
    try:
        return psutil.Process().memory_info().rss
    except Exception:
        return 0


class SessionLogger:
    """Append structured session events while mirroring concise diagnostics to stderr."""

    def __init__(
        self,
        session_id: str | None = None,
        log_dir: str | os.PathLike[str] | None = None,
        stream: Any = None,
        quiet: bool = False,
    ):
        candidate = session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", candidate):
            raise ValueError("session ID must contain only letters, numbers, '.', '_' or '-'.")
        self.session_id = candidate
        configured = log_dir or os.environ.get("RELAY_LOG_DIR") or "relay_logs_live"
        self.log_dir = Path(configured)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"session_{self.session_id}.jsonl"
        self.stream = stream if stream is not None else sys.stderr
        self.quiet = quiet
        self.records: list[dict[str, Any]] = []
        self._handle = self.path.open("a", encoding="utf-8")

    def record(
        self,
        phase: str,
        event: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
        device: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        outcome: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": _timestamp(),
            "rss": _get_rss(),
            "session_id": self.session_id,
            "phase": phase,
            "event": event,
            "request_id": request_id,
            "turn_id": turn_id,
            "device": device,
            "target": target,
            "outcome": outcome,
        }
        record.update(details)
        self.records.append(record)
        self._handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\\n")
        self._handle.flush()
        human = (
            f"[{record['timestamp']}] session={self.session_id} phase={phase} event={event}"
            f" request={request_id or '-'} turn={turn_id or '-'}"
            f" outcome={outcome or '-'}"
        )
        if device:
            human += f" device={device.get('serial') or '-'}"
        if target:
            human += f" target={target.get('url') or target.get('id') or '-'}"
        if details.get("error"):
            human += f" error={details['error']}"
        if not self.quiet:
            print(human, file=self.stream)
        return record

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
