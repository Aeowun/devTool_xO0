from __future__ import annotations
import os
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Iterable
from pathlib import Path

DEFAULT_ADB = os.environ.get("ADB_PATH")
DEFAULT_BROWSER_PACKAGE = "com.android.chrome"
DEFAULT_URL = "https://chatgpt.com/"
MAX_PROMPT_LENGTH = 2500

class RelayState(str, Enum):
    """States are deliberately explicit; UNKNOWN is never treated as success."""

    UNKNOWN = "unknown"
    OBSERVE = "observe"
    EVIDENCE = "evidence"
    GUARDED = "guarded"
    ACTION = "action"
    VERIFY = "verify"
    VERIFIED = "verified"
    FAILED = "failed"


class RecoveryTier(IntEnum):
    """Recovery can only move forward, and each tier must be verified."""

    NONE = 0
    DISMISS_OVERLAYS = 1
    REFRESH = 2
    REOPEN = 3


@dataclass
class FailureDiagnostics:
    phase: str
    state: RelayState = RelayState.UNKNOWN
    turn_id: str | None = None
    prompt_hash: str | None = None
    selectors: list[dict[str, str]] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    recovery_tier: RecoveryTier = RecoveryTier.NONE
    last_error: str | None = None
    session_id: str | None = None
    request_id: str | None = None

    def render(self) -> str:
        details = [
            f"phase={self.phase}",
            f"state={self.state.value}",
            f"turn_id={self.turn_id or '-'}",
            f"request_id={self.request_id or '-'}",
            f"session_id={self.session_id or '-'}",
            f"prompt_hash={self.prompt_hash or '-'}",
            f"recovery_tier={self.recovery_tier.name}",
        ]
        if self.candidates:
            details.append("candidates=" + "; ".join(self.candidates))
        if self.observations:
            details.append("observations=" + "; ".join(self.observations[-6:]))
        if self.last_error:
            details.append("last_error=" + self.last_error)
        return " (" + ", ".join(details) + ")"


class RelayError(RuntimeError):
    """Raised when a guarded relay operation cannot be verified."""

    def __init__(self, message: str, diagnostics: FailureDiagnostics | None = None):
        self.message = message
        self.diagnostics = diagnostics
        super().__init__(message + (diagnostics.render() if diagnostics else ""))


class CdpDisconnected(RelayError):
    """The browser connection ended; a transaction must never be retried."""
