#!/usr/bin/env python3
"""A guarded, selector-driven Android relay.

The public command line remains ``python relay.py "prompt"``.  Internally a
submission is a one-shot transaction: it records what was visible before the
action, performs one guarded click, and reconciles the result without pressing
Enter or clicking Send again when the outcome is uncertain.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import http.client
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Iterable

# The supported runner deliberately has no third-party dependency.  Keep this
# name for the opt-in legacy implementation; it is imported only on demand.
u2 = None

DEFAULT_ADB = os.environ.get("ADB_PATH")
DEFAULT_BROWSER_PACKAGE = "com.android.chrome"
DEFAULT_URL = "https://chatgpt.com/"
MAX_PROMPT_LENGTH = 1000 # User-defined limit for prompts sent to ChatGPT


def _configure_stdio() -> None:
    """Keep JSON and model text printable on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


def _read_framed_message() -> dict[str, Any] | None:
    try:
        # Read length line
        length_line = sys.stdin.readline()
        if not length_line: # EOF
            return None
        length = int(length_line.strip())

        # Read JSON payload
        payload_bytes = sys.stdin.read(length)
        # Read the trailing newline
        sys.stdin.read(1) # Consume the newline after payload

        return json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error reading framed message: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error reading framed message: {e}", file=sys.stderr)
        return None

def _write_framed_message(message: dict[str, Any]) -> None:
    try:
        payload_bytes = json.dumps(message, ensure_ascii=False).encode('utf-8')
        length = len(payload_bytes)
        sys.stdout.write(f"{length}\n")
        sys.stdout.buffer.write(payload_bytes)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as e:
        print(f"Error writing framed message: {e}", file=sys.stderr)


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


@dataclass
class _ChunkState:
    total_parts: int
    received_parts: dict[int, str] = field(default_factory=dict)
    last_received_timestamp: float = field(default_factory=time.monotonic)
    is_final_chunk_received: bool = False

class ChunkReassembler:
    """Manages the reassembly of chunked messages."""
    def __init__(self, timeout_s: float = 60.0):
        self._chunks: dict[str, _ChunkState] = {}
        self._timeout_s = timeout_s

    def add_chunk(self, chunk_message: dict[str, Any]) -> None:
        message_id = chunk_message["message_id"]
        part = chunk_message["part"]
        parts = chunk_message["parts"]
        payload = chunk_message["payload"]
        is_final = chunk_message["final"]

        if message_id not in self._chunks:
            self._chunks[message_id] = _ChunkState(total_parts=parts)
        
        chunk_state = self._chunks[message_id]

        if parts != chunk_state.total_parts:
            # Conflicting chunk, possibly a protocol violation or error
            raise RelayError(f"Conflicting 'parts' count for message_id {message_id}: expected {chunk_state.total_parts}, got {parts}")
        
        if part in chunk_state.received_parts:
            # Duplicate chunk, ignore for now
            return

        chunk_state.received_parts[part] = payload
        chunk_state.last_received_timestamp = time.monotonic()
        if is_final:
            chunk_state.is_final_chunk_received = True

    def is_complete(self, message_id: str) -> bool:
        if message_id not in self._chunks:
            return False
        
        chunk_state = self._chunks[message_id]
        return chunk_state.is_final_chunk_received and \
               len(chunk_state.received_parts) == chunk_state.total_parts

    def get_reassembled_message(self, message_id: str) -> str:
        if not self.is_complete(message_id):
            raise RelayError(f"Message {message_id} is not complete.")
        
        chunk_state = self._chunks[message_id]
        sorted_payloads = [chunk_state.received_parts[i] for i in range(1, chunk_state.total_parts + 1)]
        return "".join(sorted_payloads)

    def clear_message(self, message_id: str) -> None:
        if message_id in self._chunks:
            del self._chunks[message_id]
    
    def cleanup_old_chunks(self) -> None:
        now = time.monotonic()
        messages_to_delete = [
            msg_id for msg_id, chunk_state in self._chunks.items()
            if now - chunk_state.last_received_timestamp > self._timeout_s
        ]
        for msg_id in messages_to_delete:
            del self._chunks[msg_id]


def _timestamp() -> str:
    """Return a sortable UTC timestamp with an explicit ISO-8601 suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
        configured = log_dir or os.environ.get("RELAY_LOG_DIR") or "relay_logs"
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
        self._handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
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


@dataclass
class SelectorConfig:
    app_package: str = DEFAULT_BROWSER_PACKAGE
    input_selectors: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"textContains": "Ask anything"},
            {"descriptionContains": "Ask anything"},
            {"resourceIdMatches": r".*(prompt|composer|textarea|message_input).*"},
        ]
    )
    send_selectors: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"resourceIdMatches": r".*(send|submit|send_button|composer_send).*"},
            {"descriptionMatches": r".*(send|submit).*"},
            {"textContains": "Send"},
            {"descriptionContains": "Send"},
        ]
    )
    response_selectors: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"resourceIdMatches": r".*(message|response|assistant|bubble).*"},
            {"textContains": "Stop generating"},
            {"className": "android.widget.TextView"},
        ]
    )
    timeout_s: float = 30.0
    poll_interval_s: float = 1.0
    reconciliation_timeout_s: float = 5.0


@dataclass(frozen=True)
class SelectorCandidate:
    selector: dict[str, str]
    element: Any
    score: int
    identity: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResponseNode:
    identity: str
    text: str


@dataclass(frozen=True)
class ResponseSnapshot:
    nodes: tuple[ResponseNode, ...]
    text: str
    generating: bool

    @property
    def signature(self) -> tuple[tuple[str, str], ...]:
        return tuple((node.identity, node.text) for node in self.nodes)

    @property
    def correlation(self) -> tuple[str, ...]:
        return tuple(node.identity for node in self.nodes)


@dataclass(frozen=True)
class SubmissionBaseline:
    response: ResponseSnapshot
    input_text: str


@dataclass
class SubmissionTransaction:
    turn_id: str
    prompt_hash: str
    baseline: SubmissionBaseline
    state: RelayState = RelayState.UNKNOWN
    action_attempted: bool = False
    reconciled: bool = False
    observations: list[str] = field(default_factory=list)


class RecoveryController:
    """Owns monotonic recovery escalation and refuses unverified downgrades."""

    def __init__(self) -> None:
        self.tier = RecoveryTier.NONE
        self.verified = True

    def advance(self, tier: RecoveryTier, verified: bool = False) -> RecoveryTier:
        if tier < self.tier:
            raise RelayError(
                f"Recovery tier cannot decrease from {self.tier.name} to {tier.name}."
            )
        if not verified:
            raise RelayError(f"Recovery tier {tier.name} was not verified.")
        if tier > self.tier:
            self.tier = tier
        self.verified = True
        return self.tier


def recover_device(
    device: Any,
    controller: RecoveryController,
    url: str,
    app_package: str,
) -> RecoveryTier:
    """Escalate recovery monotonically and verify each tier before returning."""
    if controller.tier >= RecoveryTier.REOPEN:
        raise RelayError("No bounded recovery tier remains.")
    next_tier = RecoveryTier(controller.tier + 1)
    if next_tier == RecoveryTier.DISMISS_OVERLAYS:
        dismiss_overlays(device)
    elif next_tier == RecoveryTier.REFRESH:
        device.shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")
    elif next_tier == RecoveryTier.REOPEN:
        try:
            device.app_stop(app_package)
        except Exception:
            pass
        open_chatgpt(device, url, app_package)
    device.shell("echo recovery-ready")
    return controller.advance(next_tier, verified=True)


def resolve_adb(path: str | None = None) -> str:
    """Find adb without assuming the SDK belongs to a particular user."""
    candidates = [path, shutil.which("adb")]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"))
    for candidate in candidates:
        if candidate and (os.path.isfile(candidate) or shutil.which(candidate)):
            return candidate
    raise RelayError("adb was not found; install Android platform-tools or pass --adb.")


def run_adb(args: Iterable[str], adb_path: str | None = DEFAULT_ADB, serial: str | None = None) -> str:
    command = [resolve_adb(adb_path)]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RelayError(f"ADB command failed: {' '.join(args)}\n{output}")
    return output.strip()


def capture_failure_screenshot(
    adb_path: str | None,
    serial: str,
    destination: Path,
) -> Path:
    """Capture the device screen without routing binary PNG data through text."""
    command = [resolve_adb(adb_path), "-s", serial, "exec-out", "screencap", "-p"]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as exc:
        raise RelayError(f"Unable to capture failure screenshot: {exc}") from exc
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RelayError(
            "ADB failure screenshot command did not return a PNG"
            + (f": {detail}" if detail else ".")
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result.stdout)
    return destination


def ensure_device(serial: str | None = None, adb_path: str = DEFAULT_ADB) -> None:
    devices = run_adb(["devices"], adb_path=adb_path)
    if not devices or "List of devices attached" not in devices:
        raise RelayError("No Android device found via adb. Enable USB debugging and reconnect.")
    if serial:
        if serial not in devices:
            raise RelayError(f"Device {serial!r} was not found via adb.")
    elif "device" not in devices.lower():
        raise RelayError("ADB is available but no device is attached in an active state.")


def connect_device(serial: str | None = None, adb_path: str = DEFAULT_ADB):
    global u2
    if u2 is None:
        try:
            import uiautomator2 as u2_module
        except ImportError as exc:  # pragma: no cover - legacy/device only
            raise RelayError(
                "Legacy mode requires uiautomator2. Install it separately or use the CDP runner."
            ) from exc
        u2 = u2_module
    device = u2.connect(serial) if serial else u2.connect()
    try:
        time.sleep(1)
        device.shell("echo ready")
        dismiss_overlays(device)
    except Exception as exc:  # pragma: no cover - depends on Android runtime
        raise RelayError(f"Unable to connect to device: {exc}") from exc
    return device


def dismiss_overlays(device: Any) -> None:
    """Give common Android system overlays a chance to clear without tapping."""
    commands = (
        "input keyevent KEYCODE_ESCAPE",
        "input keyevent KEYCODE_WAKEUP",
        "input swipe 500 1200 500 300 250",
    )
    failures = []
    for command in commands:
        try:
            device.shell(command)
        except Exception as exc:
            failures.append(str(exc))
    if failures and len(failures) == len(commands):
        raise RelayError(f"Unable to clear Android system overlays: {failures[-1]}")


def dismiss_transient_popup(device: Any) -> bool:
    """Close a known Chrome popup without opening a new browser menu."""
    try:
        popup = device(resourceId="com.android.chrome:id/app_menu_list")
        if _element_exists(popup):
            device.press("back")
            time.sleep(0.5)
            return not _element_exists(popup)
    except Exception:
        return False
    return True


def wait_for_input_ready(device: Any, element: Any, timeout_s: float) -> Any:
    """Require a fresh, enabled prompt element after WebView/keyboard settling."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _element_exists(element):
            info = _element_info(element)
            if info.get("enabled", True) is not False and info.get("clickable", True) is not False:
                return element
        time.sleep(0.25)
    raise RelayError("ChatGPT prompt did not become ready after the UI settled.")


def set_prompt_text(
    device: Any,
    input_element: Any,
    prompt: str,
    selectors: SelectorConfig,
    diagnostics: FailureDiagnostics,
) -> Any:
    """Retry text insertion only after re-observing and re-focusing the same prompt."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            dismiss_transient_popup(device)
            input_element = wait_for_input_ready(
                device, input_element, min(selectors.timeout_s, 5.0)
            )
            input_element.click()
            time.sleep(0.75)
            if _input_text(input_element) == prompt:
                return input_element
            input_element.set_text(prompt)
            if _input_text(input_element) == prompt:
                return input_element
        except Exception as exc:
            last_error = exc
            diagnostics.observations.append(f"prompt insertion attempt {attempt + 1} failed: {exc}")
            try:
                send_keys = getattr(device, "send_keys")
                send_keys(prompt, clear=True)
                if _input_text(input_element) == prompt:
                    diagnostics.observations.append("prompt inserted through IME fallback")
                    return input_element
            except Exception as fallback_exc:
                last_error = fallback_exc
                diagnostics.observations.append(
                    f"IME prompt insertion fallback failed: {fallback_exc}"
                )
            input_element = None
        if attempt < 2:
            time.sleep(0.75 * (attempt + 1))
            input_element = find_best_match(
                device,
                selectors.input_selectors,
                timeout_s=min(selectors.timeout_s, 5.0),
                purpose="input",
                diagnostics=diagnostics,
            )
    diagnostics.last_error = str(last_error or "text was not verified")
    raise RelayError("Unable to write the prompt into the browser input field.", diagnostics)


def _element_exists(element: Any) -> bool:
    try:
        value = getattr(element, "exists", False)
        return bool(value() if callable(value) else value)
    except Exception:
        return False


def _element_info(element: Any) -> dict[str, Any]:
    try:
        info = getattr(element, "info", {})
        info = info() if callable(info) else info
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def _element_text(element: Any) -> str:
    try:
        value = getattr(element, "text", "")
        value = value() if callable(value) else value
        return str(value or "").strip()
    except Exception:
        return ""


def _element_identity(element: Any) -> str:
    info = _element_info(element)
    parts = [
        str(info.get(key, ""))
        for key in ("resourceName", "resourceId", "className", "bounds", "contentDescription")
    ]
    stable = "|".join(part for part in parts if part)
    return stable or f"object:{id(element)}"


def _element_count(element: Any) -> int:
    try:
        count = getattr(element, "count", 1)
        count = count() if callable(count) else count
        return int(count)
    except (TypeError, ValueError, AttributeError):
        return 1


def _bounds(element: Any) -> tuple[int, int, int, int] | None:
    raw = _element_info(element).get("bounds")
    if isinstance(raw, dict):
        try:
            return (
                int(raw["left"]),
                int(raw["top"]),
                int(raw["right"]),
                int(raw["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            values = [int(value) for value in raw.replace("][", ",").strip("[]").split(",")]
            return tuple(values) if len(values) == 4 else None
        except (TypeError, ValueError):
            return None
    return None


def _composer_button_candidates(device: Any, input_element: Any) -> list[SelectorCandidate]:
    """Find icon-only composer actions omitted from the WebView accessibility labels."""
    input_bounds = _bounds(input_element)
    if input_bounds is None:
        return []
    left, top, right, bottom = input_bounds
    try:
        buttons = device(className="android.widget.Button")
    except Exception:
        return []
    count = _element_count(buttons)
    if isinstance(buttons, (list, tuple)):
        button_elements = list(buttons)
    else:
        button_elements = []
        for index in range(count):
            try:
                button_elements.append(buttons[index] if count > 1 else buttons)
            except Exception:
                continue
    candidates: list[SelectorCandidate] = []
    for button in button_elements:
        info = _element_info(button)
        bounds = _bounds(button)
        if not bounds or not _element_exists(button):
            continue
        button_left, button_top, button_right, button_bottom = bounds
        if info.get("enabled") is False or info.get("clickable") is False:
            continue
        overlaps_composer = button_bottom > top and button_top < bottom
        is_right_side = button_left >= right - 260 and button_right > right - 120
        if not overlaps_composer or not is_right_side:
            continue
        identity = _element_identity(button)
        candidates.append(
            SelectorCandidate(
                {"className": "android.widget.Button"},
                button,
                60,
                identity,
                ("clickable", "enabled", "overlaps prompt", "right-side composer action"),
            )
        )
    unique = {candidate.identity: candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda candidate: candidate.score, reverse=True)


def wait_for_send_control(
    device: Any,
    selectors: SelectorConfig,
    input_element: Any,
    diagnostics: FailureDiagnostics,
) -> Any:
    """Use semantic selectors first, then a unique geometry-validated icon fallback."""
    try:
        semantic = find_best_match(
            device,
            selectors.send_selectors,
            timeout_s=min(selectors.timeout_s, 3.0),
            purpose="send",
            diagnostics=diagnostics,
        )
        if semantic is not None:
            return semantic
    except RelayError:
        pass
    candidates = _composer_button_candidates(device, input_element)
    if len(candidates) == 1:
        diagnostics.observations.append("using geometry-validated icon-only composer action")
        return candidates[0].element
    if len(candidates) > 1:
        diagnostics.observations.append("multiple icon-only composer actions matched")
    return None


def _selector_score(selector: dict[str, str], element: Any, purpose: str) -> tuple[int, tuple[str, ...]]:
    weights = {
        "resourceIdMatches": 40,
        "resourceId": 40,
        "text": 32,
        "textContains": 25,
        "description": 28,
        "descriptionContains": 22,
        "descriptionMatches": 22,
        "className": 5,
    }
    score = sum(weights.get(key, 0) for key in selector)
    evidence = ["/".join(f"{key}={value}" for key, value in selector.items())]
    info = _element_info(element)
    identity = " ".join(str(info.get(key, "")).lower() for key in (
        "resourceName", "resourceId", "text", "contentDescription", "hint"
    ))
    if purpose == "input" and _is_likely_chat_input(element):
        score += 12
        evidence.append("chat-input identity")
    if purpose == "response" and any(word in identity for word in ("assistant", "response", "message", "bubble")):
        score += 8
        evidence.append("response identity")
    return score, tuple(evidence)


def observe_candidates(
    device: Any, selectors: Iterable[dict[str, str]], purpose: str = "element"
) -> list[SelectorCandidate]:
    """Observe and score live candidates; action candidates cannot be multi-match."""
    candidates: list[SelectorCandidate] = []
    for selector in selectors:
        try:
            element = device(**selector)
        except Exception:
            continue
        if not element:
            continue
        count = _element_count(element)
        elements = [element]
        if count > 1:
            if purpose != "response":
                continue
            try:
                elements = [element[index] for index in range(count)]
            except Exception:
                continue
        for candidate_element in elements:
            if not _element_exists(candidate_element):
                continue
            score, evidence = _selector_score(selector, candidate_element, purpose)
            candidates.append(
                SelectorCandidate(
                    selector, candidate_element, score, _element_identity(candidate_element), evidence
                )
            )
    unique: dict[str, SelectorCandidate] = {}
    for candidate in candidates:
        previous = unique.get(candidate.identity)
        if previous is None or candidate.score > previous.score:
            unique[candidate.identity] = candidate
    return sorted(unique.values(), key=lambda item: item.score, reverse=True)


def observe(
    device: Any, selectors: Iterable[dict[str, str]], purpose: str = "element"
) -> list[SelectorCandidate]:
    """Observe a live UI and return scored, deduplicated candidates."""
    return observe_candidates(device, selectors, purpose)


def find_best_match(
    device: Any,
    selectors: Iterable[dict[str, str]],
    timeout_s: float = 10.0,
    purpose: str = "element",
    diagnostics: FailureDiagnostics | None = None,
    score_threshold: int = 25,
    ambiguity_margin: int = 8,
) -> Any | None:
    selector_list = list(selectors)
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        candidates = observe_candidates(device, selector_list, purpose)
        if candidates:
            if diagnostics:
                diagnostics.candidates = [
                    f"{candidate.identity}:{candidate.score}" for candidate in candidates
                ]
            top_score = candidates[0].score
            tied = [candidate for candidate in candidates if candidate.score == top_score]
            if len(tied) > 1 or (
                len(candidates) > 1
                and top_score - candidates[1].score < ambiguity_margin
            ):
                details = diagnostics or FailureDiagnostics("observe")
                details.observations.append("top selector candidates are ambiguous")
                raise RelayError("Selector discovery was ambiguous; refusing to guess.", details)
            selected = candidates[0]
            if selected.score < score_threshold:
                details = diagnostics or FailureDiagnostics("observe")
                details.observations.append(f"best candidate score {selected.score} is below threshold")
                raise RelayError("No selector candidate met the confidence threshold.", details)
            if not _element_exists(selected.element):
                if diagnostics:
                    diagnostics.observations.append("selected candidate became stale")
            else:
                return selected.element
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def find_first_match(device: Any, selectors: Iterable[dict[str, str]], timeout_s: float = 10.0):
    """Compatibility wrapper retaining the original public helper name."""
    return find_best_match(device, selectors, timeout_s=timeout_s)


def _is_likely_chat_input(element: Any) -> bool:
    info = _element_info(element)
    if not info:
        return True
    identity = " ".join(
        str(info.get(key, "")).lower()
        for key in ("resourceName", "resourceId", "text", "contentDescription", "hint")
    )
    return not any(name in identity for name in ("omnibox", "url_bar", "search_box", "address"))


def wait_for_text(
    device: Any, selectors: Iterable[dict[str, str]], timeout_s: float, label: str, purpose: str = "element"
):
    diagnostics = FailureDiagnostics("observe", selectors=list(selectors))
    element = find_best_match(
        device,
        diagnostics.selectors,
        timeout_s,
        purpose,
        diagnostics,
        score_threshold=25,
        ambiguity_margin=8,
    )
    if element is None:
        diagnostics.observations.append(f"no live {label} candidate")
        raise RelayError(f"Could not locate the {label} element using selector-based discovery.", diagnostics)
    return element


def _capture_response_snapshot(device: Any, selectors: Iterable[dict[str, str]]) -> ResponseSnapshot:
    nodes: dict[str, ResponseNode] = {}
    for candidate in observe_candidates(device, selectors, "response"):
        text = _element_text(candidate.element)
        if text:
            nodes[candidate.identity] = ResponseNode(candidate.identity, text)
    ordered = tuple(nodes.values())
    text = "\n".join(node.text for node in ordered)
    generating = any(marker.lower() in text.lower() for marker in (
        "stop generating", "generating", "regenerate response"
    ))
    return ResponseSnapshot(ordered, text, generating)


def _input_text(element: Any) -> str:
    text = _element_text(element)
    if text:
        return text
    info = _element_info(element)
    return str(info.get("text") or info.get("hint") or "").strip()


def _capture_baseline(device: Any, selectors: SelectorConfig) -> SubmissionBaseline:
    response = _capture_response_snapshot(device, selectors.response_selectors)
    input_candidates = observe_candidates(device, selectors.input_selectors, "input")
    input_text = _input_text(input_candidates[0].element) if input_candidates else ""
    return SubmissionBaseline(response, input_text)


def collect_evidence(device: Any, selectors: SelectorConfig) -> SubmissionBaseline:
    """Capture the pre-action evidence used to reconcile a submission."""
    return _capture_baseline(device, selectors)


def activate_input(device: Any, selectors: SelectorConfig) -> Any:
    diagnostics = FailureDiagnostics("observe", selectors=list(selectors.input_selectors))
    dismiss_transient_popup(device)
    element = find_best_match(
        device, diagnostics.selectors, selectors.timeout_s, "input", diagnostics
    )
    if element is None:
        diagnostics.observations.append("input candidate absent")
        raise RelayError("Could not locate the input element.", diagnostics)
    if not _is_likely_chat_input(element):
        diagnostics.observations.append("candidate matched the browser address bar")
        raise RelayError("The discovered input is Chrome's address bar, not the ChatGPT prompt.", diagnostics)
    if not _element_exists(element):
        diagnostics.observations.append("input candidate became stale before click")
        raise RelayError("The discovered input became stale before activation.", diagnostics)
    try:
        element.click()
        time.sleep(0.75)
    except Exception as exc:
        diagnostics.last_error = str(exc)
        raise RelayError("Unable to activate the browser input field.", diagnostics) from exc
    return element


def begin_submission(device: Any, prompt: str, selectors: SelectorConfig) -> SubmissionTransaction:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    baseline = collect_evidence(device, selectors)
    return SubmissionTransaction(uuid.uuid4().hex, digest, baseline)


def guard_action(candidate: Any, diagnostics: FailureDiagnostics) -> None:
    if not candidate or not _element_exists(candidate):
        diagnostics.observations.append("action candidate is stale or absent")
        raise RelayError("Refusing an action against a stale UI candidate.", diagnostics)
    if _element_count(candidate) != 1:
        diagnostics.observations.append("action candidate has multiple matches")
        raise RelayError("Refusing an action against an ambiguous UI candidate.", diagnostics)


def perform_action(candidate: Any, action: str, diagnostics: FailureDiagnostics) -> None:
    guard_action(candidate, diagnostics)
    try:
        getattr(candidate, action)()
    except Exception as exc:
        diagnostics.last_error = str(exc)
        raise RelayError(f"Unable to perform guarded UI action {action!r}.", diagnostics) from exc


def reconcile_submission(
    device: Any,
    transaction: SubmissionTransaction,
    selectors: SelectorConfig,
) -> bool:
    deadline = time.monotonic() + min(selectors.timeout_s, selectors.reconciliation_timeout_s)
    while True:
        snapshot = _capture_response_snapshot(device, selectors.response_selectors)
        if snapshot.text and (
            snapshot.signature != transaction.baseline.response.signature
            or snapshot.text != transaction.baseline.response.text
        ):
            transaction.observations.append("response changed after one-shot click")
            return True
        input_candidates = observe_candidates(device, selectors.input_selectors, "input")
        current_input = _input_text(input_candidates[0].element) if input_candidates else ""
        if transaction.baseline.input_text != current_input and not current_input:
            transaction.observations.append("input cleared after one-shot click")
            return True
        if snapshot.generating:
            transaction.observations.append("generation marker observed")
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(selectors.poll_interval_s, 0.25))


def verify_action(
    device: Any, transaction: SubmissionTransaction, selectors: SelectorConfig
) -> bool:
    """Verify a completed one-shot action without issuing another action."""
    return reconcile_submission(device, transaction, selectors)


def submit_message(device: Any, prompt: str, selectors: SelectorConfig) -> SubmissionTransaction:
    """Submit once and reconcile; never fall back to Enter or a second click."""
    transaction = begin_submission(device, prompt, selectors)
    diagnostics = FailureDiagnostics(
        "submit", turn_id=transaction.turn_id, prompt_hash=transaction.prompt_hash
    )
    try:
        transaction.state = RelayState.OBSERVE
        diagnostics.state = transaction.state
        input_element = activate_input(device, selectors)
        transaction.state = RelayState.EVIDENCE
        diagnostics.state = transaction.state
        if hasattr(input_element, "clear_text"):
            try:
                if _input_text(input_element):
                    input_element.clear_text()
            except Exception as exc:
                transaction.observations.append(f"clear_text unavailable: {exc}")
        input_element = set_prompt_text(
            device, input_element, prompt, selectors, diagnostics
        )

        send_element = wait_for_send_control(device, selectors, input_element, diagnostics)
        if send_element is None:
            diagnostics.observations.append("no semantic or geometry-validated send candidate")
            raise RelayError("Could not locate the send button element using selector-based discovery.", diagnostics)
        transaction.state = RelayState.GUARDED
        diagnostics.state = transaction.state
        guard_action(send_element, diagnostics)
        transaction.state = RelayState.ACTION
        diagnostics.state = transaction.state
        transaction.action_attempted = True
        perform_action(send_element, "click", diagnostics)
        transaction.state = RelayState.VERIFY
        diagnostics.state = transaction.state
        if not verify_action(device, transaction, selectors):
            diagnostics.observations.extend(transaction.observations)
            transaction.state = RelayState.UNKNOWN
            diagnostics.state = transaction.state
            raise RelayError(
                "Submission outcome is unknown after the one-shot send; refusing to retry.",
                diagnostics,
            )
        transaction.reconciled = True
        transaction.state = RelayState.VERIFIED
        return transaction
    except RelayError as exc:
        transaction.state = RelayState.UNKNOWN if transaction.action_attempted else RelayState.FAILED
        diagnostics.state = transaction.state
        if exc.diagnostics is not None:
            exc.diagnostics.turn_id = transaction.turn_id
            exc.diagnostics.prompt_hash = transaction.prompt_hash
            exc.diagnostics.state = transaction.state
            exc.args = (exc.message + exc.diagnostics.render(),)
        raise


def send_message(device: Any, prompt: str, selectors: SelectorConfig) -> str:
    """Compatibility wrapper: return the prompt after a verified one-shot submission."""
    submit_message(device, prompt, selectors)
    return prompt


def verify_response_stability(
    snapshots: Iterable[ResponseSnapshot], required: int = 3
) -> str | None:
    recent = list(snapshots)[-required:]
    if len(recent) != required or any(snapshot.generating for snapshot in recent):
        return None
    if not recent[0].text:
        return None
    if not all(snapshot.correlation == recent[0].correlation for snapshot in recent):
        return None
    if not all(snapshot.signature == recent[0].signature for snapshot in recent):
        return None
    return recent[-1].text


def read_response(
    device: Any,
    selectors: SelectorConfig,
    timeout_s: float | None = None,
    baseline: ResponseSnapshot | None = None,
) -> str:
    timeout = selectors.timeout_s if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout
    snapshots: deque[ResponseSnapshot] = deque(maxlen=3)
    diagnostics = FailureDiagnostics("verify")
    while True:
        snapshot = _capture_response_snapshot(device, selectors.response_selectors)
        if baseline is not None and snapshot.signature == baseline.signature:
            snapshots.clear()
            diagnostics.observations.append("snapshot still matches baseline")
        elif snapshot.text:
            snapshots.append(snapshot)
            value = verify_response_stability(snapshots, 3)
            if value is not None:
                return value
        if time.monotonic() >= deadline:
            diagnostics.observations.append(f"snapshots_collected={len(snapshots)}")
            raise RelayError(
                "No three-snapshot correlated, stable response was verified before timeout.",
                diagnostics,
            )
        time.sleep(selectors.poll_interval_s)


def open_chatgpt(
    device: Any,
    url: str = DEFAULT_URL,
    app_package: str = DEFAULT_BROWSER_PACKAGE,
) -> None:
    try:
        device.app_start(app_package)
    except Exception as exc:
        raise RelayError(f"Unable to start browser package {app_package!r}: {exc}") from exc
    time.sleep(1)
    if url:
        try:
            device.shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")
        except Exception as exc:
            raise RelayError(f"Unable to open ChatGPT URL: {exc}") from exc
    time.sleep(2)


def _build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Selector-based Android relay using uiautomator2.")
    parser.add_argument("prompt", nargs="?", default="", help="Text to send to the browser assistant.")
    parser.add_argument("--serial", default=None, help="ADB serial to target when multiple devices are connected.")
    parser.add_argument("--adb", default=DEFAULT_ADB, help=f"Path to adb executable (default: {DEFAULT_ADB}).")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to open in Chrome before sending the message.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout used during selector discovery and response polling.")
    parser.add_argument(
        "--recovery-attempts",
        type=int,
        default=3,
        help="Automatic recovery attempts for failures before Send (default: 3).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the selected action without interacting with a device.")
    return parser


def _legacy_main(argv: list[str] | None = None) -> int:
    parser = _build_legacy_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"Dry run: would use Chrome package {DEFAULT_BROWSER_PACKAGE} and URL {args.url}")
        return 0
    if not args.prompt and len(sys.argv) > 1:
        args.prompt = sys.argv[1]
    if not args.prompt:
        parser.error("A prompt is required unless --dry-run is used.")
    if args.recovery_attempts < 0:
        parser.error("--recovery-attempts must be zero or greater.")
    device = None
    recovery = RecoveryController()
    try:
        ensure_device(args.serial, args.adb)
        device = connect_device(args.serial, args.adb)
        cfg = SelectorConfig(timeout_s=args.timeout)
        open_chatgpt(device, args.url, cfg.app_package)
        for attempt in range(args.recovery_attempts + 1):
            try:
                transaction = submit_message(device, args.prompt, cfg)
            except RelayError as exc:
                diagnostics = exc.diagnostics
                if diagnostics is None or diagnostics.state == RelayState.UNKNOWN:
                    raise
                if attempt >= args.recovery_attempts:
                    raise
                diagnostics.observations.append(
                    f"recoverable pre-submit failure; recovery attempt {attempt + 1}"
                )
                recover_device(device, recovery, args.url, cfg.app_package)
                continue
            for response_attempt in range(args.recovery_attempts + 1):
                try:
                    print(read_response(device, cfg, baseline=transaction.baseline.response))
                    return 0
                except RelayError:
                    if response_attempt >= args.recovery_attempts:
                        raise
                    recover_device(device, recovery, args.url, cfg.app_package)
    except RelayError as exc:
        print(f"relay.py: {exc}", file=sys.stderr)
        return 1


class CdpDisconnected(RelayError):
    """The browser connection ended; a transaction must never be retried."""


class CdpClient:
    """Small synchronous CDP websocket client using only the Python standard library."""

    def __init__(self, websocket_url: str, timeout_s: float = 10.0):
        from urllib.parse import urlsplit

        parts = urlsplit(websocket_url)
        if parts.scheme != "ws" or not parts.hostname or not parts.path:
            raise RelayError(f"Unsupported CDP websocket URL: {websocket_url}")
        try:
            self._sock = socket.create_connection(
                (parts.hostname, parts.port or 80), timeout=max(0.1, timeout_s)
            )
        except OSError as exc:
            raise CdpDisconnected(f"Unable to connect to CDP websocket: {exc}") from exc
        self._sock.settimeout(max(0.1, timeout_s))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request_target = parts.path + (f"?{parts.query}" if parts.query else "")
        request = (
            f"GET {request_target} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{parts.port or 80}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            self.close()
            raise RelayError("Chrome did not accept the CDP websocket upgrade.")
        self._next_id = 0
        self.target_metadata: dict[str, Any] = {}

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CdpDisconnected("CDP disconnected during websocket handshake.")
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise RelayError("CDP websocket handshake was unexpectedly large.")
        return bytes(data)

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        length = len(payload)
        first = 0x80 | (opcode & 0x0F)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length < 65536:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        mask = os.urandom(4)
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._sock.sendall(header + mask + encoded)

    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._sock.recv(size - len(chunks))
            if not chunk:
                raise CdpDisconnected("CDP websocket disconnected.")
            chunks.extend(chunk)
        return bytes(chunks)

    def _recv_frame(self) -> tuple[int, bytes]:
        try:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            masked = bool(second & 0x80)
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            return opcode, payload
        except (OSError, TimeoutError, ValueError) as exc:
            raise CdpDisconnected(f"CDP websocket read failed: {exc}") from exc

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._sock is None:
            raise CdpDisconnected("CDP connection is closed.")
        self._next_id += 1
        call_id = self._next_id
        try:
            self._send_frame(
                json.dumps({"id": call_id, "method": method, "params": params or {}}).encode()
            )
        except OSError as exc:
            raise CdpDisconnected(f"CDP websocket write failed: {exc}") from exc
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x9:  # ping
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0x8:
                raise CdpDisconnected("Chrome closed the CDP websocket.")
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RelayError("CDP returned an invalid websocket message.") from exc
            if message.get("id") != call_id:
                continue  # Events and unrelated replies are safe to ignore.
            if "error" in message:
                raise RelayError(f"CDP {method} failed: {message['error']}")
            return message

    def evaluate(self, expression: str) -> Any:
        response = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        )
        result = response.get("result", {}).get("result", {})
        if "exceptionDetails" in response.get("result", {}):
            details = response["result"]["exceptionDetails"]
            raise RelayError(f"JavaScript evaluation failed: {details}")
        if result.get("subtype") == "error":
            raise RelayError(str(result.get("description") or "JavaScript evaluation failed."))
        return result.get("value")

    def close(self) -> None:
        sock, self._sock = getattr(self, "_sock", None), None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _adb_devices(adb_path: str | None) -> list[str]:
    output = run_adb(["devices"], adb_path=adb_path)
    devices = []
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])
    return devices


def choose_device(adb_path: str | None, serial: str | None) -> str:
    devices = _adb_devices(adb_path)
    if serial:
        if serial not in devices:
            raise RelayError(f"Device {serial!r} is not connected and authorized.")
        return serial
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise RelayError("No authorized Android device found via adb.")
    raise RelayError("Multiple Android devices found; pass --serial for deterministic selection.")


def _local_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _json_http(
    port: int,
    path: str,
    host: str = "127.0.0.1",
    method: str = "GET",
) -> Any:
    try:
        connection = http.client.HTTPConnection(host, port, timeout=2.0)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RelayError(f"Chrome CDP endpoint returned HTTP {response.status}.")
        return json.loads(body.decode("utf-8"))
    except (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, (TimeoutError, ConnectionRefusedError, ConnectionResetError)):
            raise RelayError(
                "Chrome CDP endpoint is unavailable at "
                f"http://{host}:{port}. Chrome must be fully closed and relaunched "
                "with --remote-debugging-port=9222; an already-running Chrome "
                "process ignores the debugging-port argument."
            ) from exc
        raise RelayError(f"Unable to query Chrome CDP endpoint: {exc}") from exc


def open_cdp_tab(port: int, url: str, host: str = "127.0.0.1") -> dict[str, Any]:
    from urllib.parse import quote

    # Chrome's /json/new endpoint creates a tab in the existing profile, so
    # its authenticated cookies are retained without reusing another chat.
    target = _json_http(port, "/json/new?" + quote(url, safe=":/?=&"), host, "PUT")
    if not isinstance(target, dict) or not target.get("webSocketDebuggerUrl"):
        raise RelayError(f"Chrome did not return a debuggable new tab: {target!r}")
    return target


def _find_chrome() -> str:
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    chrome = shutil.which("chrome") or shutil.which("chrome.exe")
    if chrome:
        return chrome
    raise RelayError("Google Chrome executable was not found.")


def ensure_desktop_cdp(
    host: str,
    port: int,
    _url: str = DEFAULT_URL,
    _profile_directory: str = "Default",
    _timeout_s: float = 2.0,
) -> None:
    """Verify that a Chrome CDP endpoint is already reachable."""
    try:
        _json_http(port, "/json/version", host)
        return
    except RelayError as exc:
        raise RelayError(
            f"Chrome CDP is not reachable at http://{host}:{port}. "
            "Ensure you have manually started Chrome with --remote-debugging-port="
            f"{port} and a non-standard --user-data-dir before running the relay."
        ) from exc


def close_cdp_tab(port: int, target_id: str, host: str = "127.0.0.1") -> None:
    from urllib.parse import quote

    try:
        connection = http.client.HTTPConnection(host, port, timeout=2.0)
        connection.request("GET", "/json/close/" + quote(target_id, safe=""))
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise RelayError(f"Chrome could not close relay tab (HTTP {response.status}).")
    except (OSError, http.client.HTTPException) as exc:
        raise RelayError(f"Unable to close relay tab: {exc}") from exc


def _tab_state_path(log_dir: Path, session_key: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_key).strip("._") or "default"
    return log_dir / f"relay_tab_{safe_key}.json"


def _read_saved_tab(port: int, path: Path, host: str) -> dict[str, Any] | None:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        target_id = str(saved.get("id") or "")
        if not target_id:
            return None
        targets = _json_http(port, "/json/list", host)
        return next(
            (target for target in targets
             if target.get("type") == "page" and target.get("id") == target_id),
            None,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_tab(path: Path, target: dict[str, Any]) -> None:
    path.write_text(
        json.dumps({"id": target.get("id"), "url": target.get("url")}, ensure_ascii=False),
        encoding="utf-8",
    )


def discover_cdp_target(
    port: int,
    preferred_url: str,
    timeout_s: float,
    host: str = "127.0.0.1",
) -> dict[str, Any]:
    from urllib.parse import urlsplit

    deadline = time.monotonic() + timeout_s
    last_error = "no page target"
    preferred_host = (urlsplit(preferred_url).hostname or "").lower()
    while time.monotonic() < deadline:
        try:
            targets = _json_http(port, "/json/list", host)
            pages = [target for target in targets if target.get("type") == "page"]
            preferred = [
                target for target in pages
                if any(host in str(target.get("url", "")).lower()
                       for host in ("chat.openai.com", "chatgpt.com"))
                or (preferred_host and preferred_host in str(target.get("url", "")).lower())
            ]
            if preferred:
                # Chrome returns the active/visible tab first. Preserve that
                # ordering; choosing an arbitrary conversation tab can make
                # CDP operate invisibly in a background tab.
                return preferred[0]
        except RelayError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RelayError(f"Could not discover a Chrome page through CDP ({last_error}).")


def connect_cdp(
    adb_path: str | None,
    serial: str | None,
    url: str,
    timeout_s: float,
) -> tuple[CdpClient, str, int]:
    selected = choose_device(adb_path, serial)
    port = _local_port()
    run_adb(["forward", f"tcp:{port}", "localabstract:chrome_devtools_remote"], adb_path, selected)
    try:
        # Reuse an existing ChatGPT page. Navigating on every invocation
        # destroys the active conversation and makes consecutive turns race a
        # fresh root page. Only open the URL when no suitable page exists.
        try:
            target = discover_cdp_target(port, url, 1.5)
        except RelayError:
            run_adb(["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                    adb_path, selected)
            target = discover_cdp_target(port, url, min(timeout_s, 15.0), host)
        from urllib.parse import urlsplit, urlunsplit

        ws = urlsplit(target["webSocketDebuggerUrl"])
        local_ws = urlunsplit(("ws", f"127.0.0.1:{port}", ws.path, ws.query, ws.fragment))
        # DOM evaluation can take longer on large streamed conversations.
        # Keep the socket alive beyond the short discovery budget; the
        # transaction deadline still bounds the overall operation.
        client = CdpClient(local_ws, timeout_s=max(30.0, min(timeout_s, 120.0)))
        # Enable the browser domains explicitly; all page interaction below is
        # still performed by Runtime.evaluate against the live DOM.
        client.call("Page.enable")
        client.call("DOM.enable")
        client.call("Runtime.enable")
        ready_deadline = time.monotonic() + min(timeout_s, 15.0)
        while True:
            try:
                ready_state = client.evaluate("document.readyState")
                if ready_state in ("interactive", "complete"):
                    break
            except RelayError as exc:
                if "default execution context" not in str(exc).lower():
                    raise
            if time.monotonic() >= ready_deadline:
                raise RelayError("Chrome page did not expose a JavaScript execution context.")
            time.sleep(0.25)
        client.target_metadata = {
            key: target.get(key)
            for key in ("id", "type", "title", "url")
            if target.get(key) is not None
        }
    except Exception:
        try:
            run_adb(["forward", "--remove", f"tcp:{port}"], adb_path, selected)
        except RelayError:
            pass
        raise
    return client, selected, port


def connect_desktop_cdp(
    endpoint: str,
    url: str,
    timeout_s: float,
) -> tuple[CdpClient, str, None]:
    """Connect directly to a locally debugged desktop Chrome instance."""
    try:
        host, port_text = endpoint.rsplit(":", 1)
        port = int(port_text)
    except ValueError as exc:
        raise RelayError("Desktop CDP endpoint must be HOST:PORT.") from exc
    ensure_desktop_cdp(host, port, url)
    
    # Reuse an existing ChatGPT page exactly like the Android version.
    target = discover_cdp_target(port, url, min(timeout_s, 15.0), host)
    
    from urllib.parse import urlsplit, urlunsplit

    ws = urlsplit(target["webSocketDebuggerUrl"])
    local_ws = urlunsplit(("ws", f"{host}:{port}", ws.path, ws.query, ws.fragment))
    client = CdpClient(local_ws, timeout_s=max(30.0, min(timeout_s, 120.0)))
    client.call("Page.enable")
    client.call("DOM.enable")
    client.call("Runtime.enable")
    ready_deadline = time.monotonic() + min(timeout_s, 15.0)
    while True:
        try:
            ready_state = client.evaluate("document.readyState")
            if ready_state in ("interactive", "complete"):
                break
        except RelayError as exc:
            if "default execution context" not in str(exc).lower():
                raise
        if time.monotonic() >= ready_deadline:
            raise RelayError("Chrome page did not expose a JavaScript execution context.")
        time.sleep(0.25)
    client.target_metadata = {
        key: target.get(key)
        for key in ("id", "type", "title", "url")
        if target.get(key) is not None
    }
    return client, f"desktop:{endpoint}", None


_COMPOSER_JS = r"""/* relay:discover-composer */
(() => {
  const visible = e => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const nodes = [...document.querySelectorAll("textarea,[contenteditable='true'],[role='textbox']")]
    .filter(visible).filter(e => !e.closest("nav,header,[role='navigation']"));
  const score = e => { const a = ((e.getAttribute("aria-label")||"")+" "+
    (e.getAttribute("placeholder")||"")+" "+(e.getAttribute("data-testid")||"")).toLowerCase();
    return (a.includes("ask")||a.includes("message")||a.includes("prompt") ? 50 : 0) +
      (e.tagName === "TEXTAREA" ? 10 : 0) + (e.isContentEditable ? 8 : 0); };
  nodes.sort((a,b) => score(b)-score(a));
  if (!nodes.length) return {found:false};
  const e = nodes[0], value = e.tagName === "TEXTAREA" || e.tagName === "INPUT" ? e.value : e.innerText;
  return {found:true, value:value || "", tag:e.tagName, editable:!!e.isContentEditable};
})()"""

_PAGE_STATE_JS = r"""/* relay:inspect-page-state */
(() => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden";};
  const overlays=[...document.querySelectorAll("[role='dialog'],[aria-modal='true'],[role='menu']")]
    .filter(visible).map(e=>({role:e.getAttribute("role"),text:(e.innerText||"").slice(0,160)}));
  const composer=[...document.querySelectorAll("textarea,[contenteditable='true'],[role='textbox']")]
    .some(visible);
  const busy=!!document.querySelector("[data-is-streaming='true'],button[aria-label*='Stop']");
  return {url:location.href,ready:document.readyState,composer,overlays,busy,
    viewport:{width:innerWidth,height:innerHeight,visualHeight:visualViewport?.height||innerHeight}};
})()"""

_HEAL_PAGE_JS = r"""/* relay:heal-page-state */
(() => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden";};
  const closeWords=/^(close|cancel|dismiss|done|got it|not now|no thanks|escape)$/i;
  const candidates=[...document.querySelectorAll(
    "[role='dialog'] button,[role='menu'] button,[aria-modal='true'] button,button,[role='button']")]
    .filter(visible).filter(e=>closeWords.test((e.getAttribute("aria-label")||e.textContent||"").trim()));
  let clicked=0;
  for(const e of candidates.slice(0,3)){e.click();clicked++;}
  document.activeElement?.blur();
  document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",code:"Escape",bubbles:true}));
  document.dispatchEvent(new KeyboardEvent("keyup",{key:"Escape",code:"Escape",bubbles:true}));
  return {clicked};
})()"""

_SET_COMPOSER_JS = r"""/* relay:set-composer */
(() => {
  const wanted = __RELAY_TEXT__;
  const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>0 && r.height>0 && s.display!=="none" && s.visibility!=="hidden"; };
  const nodes=[...document.querySelectorAll("textarea,[contenteditable='true'],[role='textbox']")]
    .filter(visible).filter(e=>!e.closest("nav,header,[role='navigation']"));
  const score=e=>{const a=((e.getAttribute("aria-label")||"")+" "+
    (e.getAttribute("placeholder")||"")+" "+(e.getAttribute("data-testid")||"")).toLowerCase();
    return (a.includes("ask")||a.includes("message")||a.includes("prompt")?50:0)+(e.tagName==="TEXTAREA"?10:0);};
  nodes.sort((a,b)=>score(b)-score(a)); const e=nodes[0]; if(!e) return {found:false,value:""};
  e.focus();
  if(e.tagName==="TEXTAREA" || e.tagName==="INPUT") {
    const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value")?.set ||
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")?.set;
    if(setter) setter.call(e,wanted); else e.value=wanted;
  } else {
    e.innerHTML=""; document.execCommand("insertText",false,wanted);
    if((e.innerText||"")!==wanted) e.textContent=wanted;
  }
  e.dispatchEvent(new InputEvent("input",{bubbles:true,inputType:"insertText",data:wanted}));
  e.dispatchEvent(new Event("change",{bubbles:true}));
  const value=e.tagName==="TEXTAREA" || e.tagName==="INPUT" ? e.value : e.innerText;
  return {found:true,value:value||""};
})()"""

_PREPARE_SEND_JS = r"""/* relay:prepare-send */
(() => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden";};
  const nodes=[...document.querySelectorAll("button,[role='button']")].filter(visible)
    .filter(e=>!e.disabled && e.getAttribute("aria-disabled")!=="true");
  const semantic=nodes.filter(e=>{const label=((e.getAttribute("aria-label")||"")+" "+
    (e.getAttribute("title")||"")+" "+(e.textContent||"")).trim().toLowerCase();
    const testid=(e.getAttribute("data-testid")||"").toLowerCase();
    return (/^(send|send prompt|send message|submit)$/.test(label) ||
      /\b(send prompt|send message|submit)\b/.test(label) ||
      /(^|[-_])send([-_]|$)/.test(testid)) &&
      !/temporary|new chat|chat mode/.test(label+" "+testid);});
  const matches=semantic.length ? semantic : nodes.filter(e=>
    e.tagName==="BUTTON" && e.type==="submit");
  return {count:matches.length,labels:matches.map(e=>(e.getAttribute("aria-label")||e.textContent||"").trim())};
})()"""

_SUBMIT_JS = r"""/* relay:submit-once */
(() => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden";};
  const input=[...document.querySelectorAll("textarea,[contenteditable='true'],[role='textbox']")]
    .filter(visible).filter(e=>!e.closest("nav,header,[role='navigation']"))
    .sort((a,b)=>((b.getAttribute("aria-label")||"").match(/ask|message|prompt/i)?1:0)-
      ((a.getAttribute("aria-label")||"").match(/ask|message|prompt/i)?1:0))[0];
  const value=input ? (input.tagName==="TEXTAREA"||input.tagName==="INPUT"?input.value:input.innerText) : "";
  if(!input || value !== __RELAY_TEXT__) return {clicked:false,reason:"composer text changed"};
  const nodes=[...document.querySelectorAll("button,[role='button']")].filter(visible)
    .filter(e=>!e.disabled && e.getAttribute("aria-disabled")!=="true");
  const semantic=nodes.filter(e=>{const label=((e.getAttribute("aria-label")||"")+" "+
    (e.getAttribute("title")||"")+" "+(e.textContent||"")).trim().toLowerCase();
    const testid=(e.getAttribute("data-testid")||"").toLowerCase();
    return (/^(send|send prompt|send message|submit)$/.test(label) ||
      /\b(send prompt|send message|submit)\b/.test(label) ||
      /(^|[-_])send([-_]|$)/.test(testid)) &&
      !/temporary|new chat|chat mode/.test(label+" "+testid);});
  const matches=semantic.length ? semantic : nodes.filter(e=>
    e.tagName==="BUTTON"&&e.type==="submit");
  if(matches.length!==1) return {clicked:false,reason:"send control not unique",count:matches.length};
  matches[0].click(); return {clicked:true};
})()"""

_MESSAGES_JS = r"""/* relay:capture-assistant */
(() => {
  const roleNodes=[...document.querySelectorAll(
    "[data-message-author-role='assistant'], [data-message-role='assistant']"
  )];
  // Older turns are irrelevant after the baseline. Keep only a tiny identity
  // window so large conversations do not create huge CDP response frames.
  const allNodes=roleNodes.length ? roleNodes : [
    ...document.querySelectorAll("article, [class*='assistantMessage']")
  ];
  // Increased window to 20 to ensure we capture long turn transitions properly.
  const nodes=allNodes.slice(-20);
  const result=[]; const seen=new Set();
  for(let i=0;i<nodes.length;i++) {
    const e=nodes[i], role=e.getAttribute("data-message-author-role") ||
      e.getAttribute("data-message-role");
    if(role && role!=="assistant") continue; if(!e.innerText) continue;
    const content=e.querySelector("[data-assistant-markdown], [class*='messageCopy']");
    const text=(content ? content.innerText : e.innerText || "").trim();
    if(!text) continue;
    const id=e.getAttribute("data-message-id")||e.id||e.getAttribute("data-testid")||
      ((role||"article")+":"+i); if(seen.has(id)) continue; seen.add(id);
    const parent=e.closest("[data-is-streaming]");
    result.push({id,text,streaming:!!parent||e.getAttribute("data-is-streaming")==="true"});
  }
  return result;
})()"""


@dataclass
class CdpTransaction:
    turn_id: str
    prompt_hash: str
    baseline_ids: frozenset[str]
    baseline_texts: frozenset[str] = frozenset()
    action_attempted: bool = False
    request_id: str = ""


def _eval_json(client: Any, script: str) -> Any:
    value = client.evaluate(script)
    if value is None:
        raise RelayError("CDP returned no value for a DOM operation.")
    return value


def _composer(client: Any) -> dict[str, Any]:
    value = _eval_json(client, _COMPOSER_JS)
    if not isinstance(value, dict) or not value.get("found"):
        raise RelayError("ChatGPT composer was not found in the page DOM.")
    return value


def _messages(client: Any) -> list[dict[str, Any]]:
    value = _eval_json(client, _MESSAGES_JS)
    return value if isinstance(value, list) else []


def _set_composer(client: Any, prompt: str) -> None:
    script = _SET_COMPOSER_JS.replace("__RELAY_TEXT__", json.dumps(prompt))
    result = _eval_json(client, script)
    if not isinstance(result, dict) or result.get("value") != prompt:
        raise RelayError("Composer text did not exactly match the requested prompt.")


def _page_state(client: Any) -> dict[str, Any]:
    value = _eval_json(client, _PAGE_STATE_JS)
    return value if isinstance(value, dict) else {}


def _heal_page(client: Any) -> dict[str, Any]:
    value = _eval_json(client, _HEAL_PAGE_JS)
    return value if isinstance(value, dict) else {}


def _submit_once(
    client: Any,
    prompt: str,
    transaction: CdpTransaction,
    stage_hook: Any = None,
) -> None:
    prepared = _eval_json(client, _PREPARE_SEND_JS)
    if not isinstance(prepared, dict) or prepared.get("count") != 1:
        raise RelayError(f"Send control was not unique: {prepared!r}")
    if stage_hook:
        stage_hook(
            "send-control-discovery",
            "verified",
            {"labels": prepared.get("labels", []), "turn_id": transaction.turn_id},
        )
    transaction.action_attempted = True
    if stage_hook:
        stage_hook("one-shot-submission", "attempt", {"turn_id": transaction.turn_id})
    result = _eval_json(client, _SUBMIT_JS.replace("__RELAY_TEXT__", json.dumps(prompt)))
    if not isinstance(result, dict) or not result.get("clicked"):
        raise RelayError(f"Send action was not verified: {result!r}")
    if stage_hook:
        stage_hook("one-shot-submission", "reconciled", {"sent": True, "turn_id": transaction.turn_id})


def _wait_for_send_control(client: Any, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_result: Any = None
    while time.monotonic() < deadline:
        last_result = _eval_json(client, _PREPARE_SEND_JS)
        if isinstance(last_result, dict) and last_result.get("count") == 1:
            return
        time.sleep(0.2)
    raise RelayError(f"Send control was not unique: {last_result!r}")


def _wait_for_composer(client: Any, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    checks = 0
    while time.monotonic() < deadline:
        try:
            return _composer(client)
        except CdpDisconnected:
            raise
        except RelayError as exc:
            last_error = exc
            checks += 1
            if checks % 5 == 0:
                _heal_page(client)
            time.sleep(0.2)
    raise RelayError(f"ChatGPT composer was not ready before timeout: {last_error}")


def run_cdp_transaction(
    client: Any,
    prompt: str,
    timeout_s: float = 45.0,
    poll_interval_s: float = 0.4,
    *,
    logger: SessionLogger | None = None,
    request_id: str | None = None,
    device_metadata: dict[str, Any] | None = None,
    target_metadata: dict[str, Any] | None = None,
    stage_hook: Any = None,
) -> tuple[str, CdpTransaction]:
    """Perform exactly one submit and return a correlated, stable assistant turn."""
    if not prompt:
        raise RelayError("A non-empty prompt is required.")
    baseline = _messages(client)
    transaction = CdpTransaction(
        uuid.uuid4().hex,
        hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        frozenset(str(item.get("id")) for item in baseline),
        frozenset(str(item.get("text") or "").strip() for item in baseline),
        request_id=request_id or "",
    )
    if logger:
        logger.record(
            "transaction",
            "baseline-captured",
            request_id=request_id,
            turn_id=transaction.turn_id,
            device=device_metadata,
            target=target_metadata,
            outcome="ready",
            baseline_count=len(baseline),
        )
    try:
        state = _page_state(client)
        if logger:
            logger.record(
                "recovery",
                "state-observed",
                request_id=request_id,
                turn_id=transaction.turn_id,
                device=device_metadata,
                target=target_metadata,
                outcome="ready",
                state=state,
            )
        if state.get("overlays"):
            healed = _heal_page(client)
            if logger:
                logger.record(
                    "recovery",
                    "overlay-dismissed",
                    request_id=request_id,
                    turn_id=transaction.turn_id,
                    device=device_metadata,
                    target=target_metadata,
                    outcome="attempted",
                    overlays=len(state["overlays"]),
                    clicked=healed.get("clicked", 0),
                )
        _wait_for_composer(client, min(timeout_s, 15.0))
        if stage_hook:
            stage_hook("input-area-discovery", "verified", {"turn_id": transaction.turn_id})
        for insertion_attempt in range(2):
            try:
                _set_composer(client, prompt)
                break
            except RelayError:
                if insertion_attempt == 1:
                    raise
                _heal_page(client)
                _wait_for_composer(client, min(timeout_s, 5.0))
        if _composer(client).get("value") != prompt:
            raise RelayError("Composer text changed before the guarded Send action.")
        if stage_hook:
            stage_hook("exact-text-insertion", "verified", {"turn_id": transaction.turn_id})
        if logger:
            logger.record(
                "composer",
                "text-verified",
                request_id=request_id,
                turn_id=transaction.turn_id,
                device=device_metadata,
                target=target_metadata,
                outcome="ready",
            )
        if logger:
            logger.record(
                "submit",
                "attempt",
                request_id=request_id,
                turn_id=transaction.turn_id,
                device=device_metadata,
                target=target_metadata,
                outcome="one-shot",
            )
        _wait_for_send_control(client, min(timeout_s, 15.0))
        _submit_once(client, prompt, transaction, stage_hook=stage_hook)
        if logger:
            logger.record(
                "submit",
                "verified",
                request_id=request_id,
                turn_id=transaction.turn_id,
                device=device_metadata,
                target=target_metadata,
                outcome="accepted",
            )
    except CdpDisconnected as exc:
        if transaction.action_attempted:
            disconnect_error = CdpDisconnected(
                "CDP disconnected after Send was attempted; refusing to submit again."
            )
            disconnect_error.transaction = transaction
            raise disconnect_error from exc
        exc.transaction = transaction
        raise
    except RelayError as exc:
        exc.transaction = transaction
        raise
    deadline = time.monotonic() + timeout_s
    stable_id: str | None = None
    stable_text: str | None = None
    stable_count = 0
    while time.monotonic() < deadline:
        try:
            current = _messages(client)
            # Check global page state for the 'Stop generating' button or streaming markers.
            page_state = _page_state(client)
            is_page_busy = page_state.get("busy", False)
        except CdpDisconnected as exc:
            disconnect_error = CdpDisconnected(
                "CDP disconnected while awaiting the submitted turn; refusing to retry."
            )
            disconnect_error.transaction = transaction
            raise disconnect_error from exc
        new = [
            item for item in current
            if (
                str(item.get("id")) not in transaction.baseline_ids
                or str(item.get("text") or "").strip() not in transaction.baseline_texts
            )
        ]
        if new:
            node = new[-1]
            node_id, text = str(node.get("id")), str(node.get("text") or "").strip()
            # A node is considered generating if its element is streaming OR the page is globally busy.
            generating = is_page_busy or bool(node.get("streaming")) or any(
                marker in text.lower() for marker in ("stop generating", "generating...")
            )
            
            # Require stability AND that the assistant is no longer generating.
            if text and not generating and node_id == stable_id and text == stable_text:
                stable_count += 1
            elif text and not generating:
                stable_id, stable_text, stable_count = node_id, text, 1
            else:
                stable_id, stable_text, stable_count = None, None, 0
            
            # Increased stability count to 5 (approx 2s) to be safe with slow network/streaming.
            if stable_count >= 5:
                if stage_hook:
                    stage_hook(
                        "correlated-stable-response",
                        "verified",
                        {"text_length": len(stable_text or ""), "turn_id": transaction.turn_id},
                    )
                if logger:
                    logger.record(
                        "response",
                        "stable",
                        request_id=request_id,
                        turn_id=transaction.turn_id,
                        device=device_metadata,
                        target=target_metadata,
                        outcome="success",
                    )
                return stable_text or "", transaction
        time.sleep(poll_interval_s)
    raise RelayError(
        "A new stable assistant response was not observed before timeout "
        f"(turn_id={transaction.turn_id}, submit_attempted={transaction.action_attempted})."
    )


class _FakeCdp:
    """Offline DOM/CDP state machine used by --self-test."""

    def __init__(self):
        self.prompt = ""
        self.sent = 0
        self.messages = [{"id": "old", "text": "previous", "streaming": False}]

    def evaluate(self, script: str) -> Any:
        if "inspect-page-state" in script:
            return {"ready": "complete", "composer": True, "overlays": [], "busy": False}
        if "heal-page-state" in script:
            return {"clicked": 0}
        if "discover-composer" in script:
            return {"found": True, "value": self.prompt}
        if "set-composer" in script:
            prefix = "const wanted = "
            start = script.index(prefix) + len(prefix)
            self.prompt = json.JSONDecoder().raw_decode(script[start:])[0]
            return {"found": True, "value": self.prompt}
        if "prepare-send" in script:
            return {"count": 1, "labels": ["Send"]}
        if "submit-once" in script:
            self.sent += 1
            self.messages.append({"id": "new", "text": "offline response", "streaming": False})
            self.prompt = ""
            return {"clicked": True}
        if "capture-assistant" in script:
            return self.messages
        raise AssertionError(f"unexpected fake CDP script: {script[:40]}")


CERTIFICATION_PHASES = (
    "target-open-confirmation",
    "chatgpt-origin-readiness",
    "input-area-discovery",
    "exact-text-insertion-verification",
    "send-control-discovery",
    "one-shot-submission-reconciliation",
    "correlated-stable-response-extraction",
)

CERTIFICATION_SCENARIOS = (
    "delayed-target",
    "stale-target-reconnect",
    "popup-overlay-dom",
    "unlabeled-icon-only-send",
    "slow-composer-readiness",
    "delayed-response",
    "cdp-timeout-before-submit",
    "disconnect-after-submit",
    "stale-prior-response",
    "duplicate-submit-pressure",
)


class _CertificationFakeCdp(_FakeCdp):
    """Deterministic offline target/DOM model for repeatability certification."""

    def __init__(self, scenario: str):
        super().__init__()
        self.scenario = scenario
        self.composer_checks = 0
        self.response_checks = 0
        self.target_checks = 0
        self.reconnects = 0
        self.prepare_checks = 0
        self.disconnected = False
        if scenario == "stale-prior-response":
            self.messages = [{"id": "prior-turn", "text": "stale prior response", "streaming": False}]

    def open_target(self) -> dict[str, Any] | None:
        self.target_checks += 1
        if self.scenario == "delayed-target" and self.target_checks < 3:
            return None
        if self.scenario == "stale-target-reconnect" and self.target_checks == 1:
            return {"id": "stale-target", "stale": True}
        return {"id": "cert-target", "url": "https://chatgpt.com/", "stale": False}

    def reconnect_target(self) -> dict[str, Any]:
        self.reconnects += 1
        return {"id": "cert-target-reconnected", "url": "https://chatgpt.com/", "stale": False}

    def confirm_origin_ready(self, target: dict[str, Any]) -> bool:
        return (
            target.get("stale") is False
            and target.get("url", "").startswith(("https://chatgpt.com", "https://chat.openai.com"))
        )

    def evaluate(self, script: str) -> Any:
        if "capture-assistant" in script:
            if self.disconnected:
                raise CdpDisconnected("certification fake disconnected after submit")
            self.response_checks += 1
            if self.sent and self.scenario == "delayed-response" and self.response_checks <= 2:
                return self.messages + [{"id": "new", "text": "offline response", "streaming": True}]
            return self.messages
        if "discover-composer" in script:
            self.composer_checks += 1
            if self.scenario == "cdp-timeout-before-submit":
                return {"found": False, "value": ""}
            if self.scenario in ("popup-overlay-dom", "slow-composer-readiness"):
                required = 3 if self.scenario == "slow-composer-readiness" else 2
                if self.composer_checks < required:
                    return {"found": False, "value": ""}
            return {"found": True, "value": self.prompt}
        if "set-composer" in script:
            if self.scenario == "cdp-timeout-before-submit":
                return {"found": False, "value": ""}
            return super().evaluate(script)
        if "prepare-send" in script:
            self.prepare_checks += 1
            return {"count": 1, "labels": [] if self.scenario == "unlabeled-icon-only-send" else ["Send"]}
        if "submit-once" in script:
            result = super().evaluate(script)
            if self.scenario == "disconnect-after-submit":
                self.disconnected = True
            return result
        return super().evaluate(script)


def run_certification(
    iterations: int = 10,
    *,
    logger: SessionLogger | None = None,
) -> dict[str, Any]:
    """Run staged repeatability certification; negative scenarios must fail safely."""
    if iterations < 1:
        raise ValueError("certification iterations must be at least 1")
    owned_logger = logger is None
    logger = logger or SessionLogger()
    summary: dict[str, Any] = {
        "certification": "PASS",
        "iterations": iterations,
        "phases": list(CERTIFICATION_PHASES),
        "scenarios": [],
        "duplicate_submits": 0,
        "false_positives": 0,
        "failures": [],
    }
    phase_alias = {
        "input-area-discovery": "input-area-discovery",
        "exact-text-insertion": "exact-text-insertion-verification",
        "send-control-discovery": "send-control-discovery",
        "one-shot-submission": "one-shot-submission-reconciliation",
        "correlated-stable-response": "correlated-stable-response-extraction",
    }
    try:
        for index in range(iterations):
            scenario = CERTIFICATION_SCENARIOS[index % len(CERTIFICATION_SCENARIOS)]
            fake = _CertificationFakeCdp(scenario)
            request_id = uuid.uuid4().hex
            prompt = f"certification prompt {index + 1}"
            turn_id: str | None = None
            phase_results: dict[str, str] = {}

            def phase_record(phase: str, outcome: str, **details: Any) -> None:
                phase_results[phase] = outcome
                logger.record(
                    phase,
                    "certification-run",
                    request_id=request_id,
                    turn_id=turn_id,
                    device={"serial": "offline-certification"},
                    target={"url": "https://chatgpt.com/", "scenario": scenario},
                    outcome=outcome,
                    iteration=index + 1,
                    scenario=scenario,
                    **details,
                )

            logger.record(
                "certification",
                "iteration-start",
                request_id=request_id,
                device={"serial": "offline-certification"},
                target={"url": "https://chatgpt.com/", "scenario": scenario},
                outcome="started",
                iteration=index + 1,
                scenario=scenario,
            )
            target = None
            for _ in range(4):
                target = fake.open_target()
                if target and not target.get("stale"):
                    break
                if target and target.get("stale"):
                    target = fake.reconnect_target()
                    break
            if not target or target.get("stale"):
                phase_record("target-open-confirmation", "failed")
                summary["failures"].append(f"{index + 1}: target")
                continue
            phase_record("target-open-confirmation", "verified", reconnects=fake.reconnects)
            if not fake.confirm_origin_ready(target):
                phase_record("chatgpt-origin-readiness", "failed")
                summary["failures"].append(f"{index + 1}: origin")
                continue
            phase_record("chatgpt-origin-readiness", "verified")

            def stage_hook(stage: str, event: str, details: dict[str, Any]) -> None:
                nonlocal turn_id
                details = dict(details)
                turn_id = details.pop("turn_id", turn_id)
                phase = phase_alias[stage]
                if stage == "one-shot-submission" and event == "attempt":
                    return
                phase_record(phase, "verified", stage_event=event, **details)

            error: Exception | None = None
            response = None
            try:
                response, transaction = run_cdp_transaction(
                    fake,
                    prompt,
                    timeout_s=0.8,
                    poll_interval_s=0,
                    logger=logger,
                    request_id=request_id,
                    device_metadata={"serial": "offline-certification"},
                    target_metadata=target,
                    stage_hook=stage_hook,
                )
                turn_id = transaction.turn_id
            except Exception as exc:
                error = exc
                transaction = getattr(exc, "transaction", None)
                if transaction is not None:
                    turn_id = transaction.turn_id

            iteration_pass = False
            if scenario == "cdp-timeout-before-submit":
                for phase in CERTIFICATION_PHASES[2:]:
                    if phase not in phase_results:
                        phase_record(phase, "expected-timeout")
                iteration_pass = fake.sent == 0 and response is None and isinstance(error, RelayError)
                if not iteration_pass:
                    summary["false_positives"] += 1
                    summary["failures"].append(f"{index + 1}: timeout false positive")
            elif scenario == "disconnect-after-submit":
                if CERTIFICATION_PHASES[6] not in phase_results:
                    phase_record(CERTIFICATION_PHASES[6], "expected-disconnect")
                iteration_pass = fake.sent == 1 and isinstance(error, CdpDisconnected)
                if not iteration_pass:
                    summary["false_positives"] += 1
                    summary["failures"].append(f"{index + 1}: disconnect handling")
            elif error is not None or response is None:
                summary["failures"].append(f"{index + 1}: {error}")
            elif fake.sent != 1:
                summary["failures"].append(f"{index + 1}: submission count={fake.sent}")
            else:
                iteration_pass = True
            if fake.sent > 1:
                summary["duplicate_submits"] += fake.sent - 1
            for phase in CERTIFICATION_PHASES:
                if phase not in phase_results:
                    phase_record(
                        phase,
                        "expected-stop"
                        if scenario in ("cdp-timeout-before-submit", "disconnect-after-submit")
                        else "failed",
                    )
            logger.record(
                "certification",
                "iteration-outcome",
                request_id=request_id,
                turn_id=turn_id,
                device={"serial": "offline-certification"},
                target=target,
                outcome="pass" if iteration_pass else "fail",
                iteration=index + 1,
                scenario=scenario,
                submit_count=fake.sent,
            )
            summary["scenarios"].append({
                "iteration": index + 1,
                "scenario": scenario,
                "submit_count": fake.sent,
                "error": type(error).__name__ if error else None,
            })
        if summary["duplicate_submits"] or summary["false_positives"] or summary["failures"]:
            summary["certification"] = "FAIL"
        logger.record(
            "certification",
            "outcome",
            outcome=summary["certification"],
            iterations=iterations,
            duplicate_submits=summary["duplicate_submits"],
            false_positives=summary["false_positives"],
            failures=len(summary["failures"]),
        )
        return summary
    finally:
        if owned_logger:
            logger.close()


def self_test(logger: SessionLogger | None = None) -> None:
    request_id = uuid.uuid4().hex
    if logger:
        logger.record("self-test", "start", request_id=request_id, outcome="started")
    fake = _FakeCdp()
    response, transaction = run_cdp_transaction(
        fake,
        "offline prompt",
        timeout_s=1,
        poll_interval_s=0,
        logger=logger,
        request_id=request_id,
        device_metadata={"serial": "offline-fake"},
        target_metadata={"type": "fake-cdp", "url": "offline://fake"},
    )
    if response != "offline response" or fake.sent != 1 or not transaction.action_attempted:
        raise RelayError("offline CDP self-test did not complete its one-shot transaction.")
    if logger:
        logger.record(
            "self-test",
            "outcome",
            request_id=request_id,
            turn_id=transaction.turn_id,
            device={"serial": "offline-fake"},
            target={"type": "fake-cdp", "url": "offline://fake"},
            outcome="success",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic ChatGPT Android runner via Chrome DevTools Protocol."
    )
    parser.add_argument("prompt", nargs="?", help="Text to send to ChatGPT.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Enable persistent mode, communicating via stdin/stdout using JSONL.",
    )
    parser.add_argument("--serial", help="ADB serial (auto-selected only when exactly one device exists).")
    parser.add_argument("--adb", default=DEFAULT_ADB, help="Path to adb (auto-discovered by default).")
    parser.add_argument("--url", default=DEFAULT_URL, help="ChatGPT URL used to open Chrome when needed.")
    parser.add_argument("--timeout", type=float, default=45.0, help="CDP discovery and response timeout.")
    parser.add_argument(
        "--desktop-cdp",
        metavar="HOST:PORT",
        help="Use a locally debugged desktop Chrome instance instead of Android ADB.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print response metadata as JSON.")
    parser.add_argument("--quiet", "-q", action="store_true", help="Print only the URL and response.")
    parser.add_argument("--session-id", help="Stable ID to use for this invocation's log file.")
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for JSONL session logs (default: RELAY_LOG_DIR or ./relay_logs).",
    )
    parser.add_argument("--self-test", action="store_true", help="Run the offline fake-CDP transaction test.")
    parser.add_argument(
        "--certify",
        action="store_true",
        help="Run the staged offline repeatability certification gate.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Certification iterations (default: 10).",
    )
    parser.add_argument(
        "--device-smoke-test", action="store_true",
        help="Connect to a discovered Chrome target and inspect the DOM without sending a prompt.",
    )
    parser.add_argument(
        "--legacy", action="store_true",
        help="Use the old uiautomator2 runner (unsupported compatibility mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.device_smoke_test and args.prompt:
        parser.error("--device-smoke-test does not accept a prompt.")
    if args.stdio and (
        args.self_test or args.certify or args.legacy
    ):
        parser.error("--stdio cannot be combined with other operation modes.")
    if args.iterations < 1:
        parser.error("--iterations must be at least 1.")
    
    def chunk_prompt(text: str, limit: int = 750) -> list[str]:
        if len(text) <= limit:
            return [text]
        
        chunks = []
        remaining = text
        
        # We assume max N/N is 2 digits (e.g. 10/10). 
        # Footer is roughly 60 chars.
        footer_reserve = 80 
        payload_limit = limit - footer_reserve
        
        part = 1
        # We don't know the total yet, so we'll patch it later or use a generic 'total'
        # Actually, let's estimate.
        est_total = (len(text) // payload_limit) + 1
        
        while remaining:
            if len(remaining) <= limit and part > 1:
                # This is the final chunk
                chunks.append(remaining)
                break
            
            # Take a slice
            chunk_text = remaining[:payload_limit]
            
            # Try to break at newline or space
            last_space = chunk_text.rfind(' ')
            last_newline = chunk_text.rfind('\n')
            break_pt = max(last_space, last_newline)
            
            if break_pt > payload_limit * 0.7:
                chunk_text = remaining[:break_pt]
                remaining = remaining[break_pt:].lstrip()
            else:
                remaining = remaining[payload_limit:]
            
            chunks.append(chunk_text)
            part += 1
            
        # Add footers
        total = len(chunks)
        final_chunks = []
        for i, content in enumerate(chunks):
            current_part = i + 1
            if current_part < total:
                footer = f"\n\n[MESSAGE PART {current_part}/{total} — MORE FOLLOWS — DO NOT RESPOND YET]"
            else:
                footer = f"\n\n[MESSAGE PART {current_part}/{total} — END OF MESSAGE — RESPOND NOW]"
            final_chunks.append(content + footer)
            
        return final_chunks

    if (
        not args.device_smoke_test and not args.prompt and not args.self_test
        and not args.legacy and not args.certify and not args.stdio
    ):
        parser.error("A prompt is required.")
    if args.stdio:
        reassembler = ChunkReassembler()
        _stdio_client = None
        _stdio_serial = None
        
        # If an initial prompt was provided on the CLI, treat it as the first context/prompt
        if args.prompt:
            # We don't have a session_id yet, but we can't do much without session_start
            # However, the user wants us to solve the 'A prompt is required' error.
            # In magy-agent/1, the first message MUST be session_start.
            # So we will just hold this prompt until we have a client.
            pass

        while True:
            incoming_message = _read_framed_message()
            if incoming_message is None:
                break

            processed_message: dict[str, Any] | None = None
            error_details: dict[str, Any] | None = None

            try:
                # ... [basic validation omitted for brevity in targetContent match]
                
                # ...
                if processed_message:
                    msg_type = processed_message.get("type")
                    session_id = processed_message.get("session_id")
                    
                    if msg_type == "session_start":
                        config = processed_message.get("config", {})
                        endpoint = config.get("desktop_cdp_endpoint", "127.0.0.1:9222")
                        url = config.get("chatgpt_url", DEFAULT_URL)
                        timeout = config.get("timeout_s", 60.0)
                        
                        try:
                            # Re-use existing CDP connection logic
                            client, serial, port = connect_desktop_cdp(endpoint, url, timeout)
                            _stdio_client = client
                            _stdio_serial = serial
                            
                            _write_framed_message({
                                "protocol": "magy-agent/1",
                                "type": "session_ready",
                                "session_id": session_id,
                                "message_id": str(uuid.uuid4().hex)
                            })
                        except Exception as e:
                             raise RelayError(f"Failed to connect to Chrome: {e}")

                    elif msg_type == "assistant_message":
                        if _stdio_client is None:
                            raise RelayError("Session not started. Send session_start first.")
                            
                        prompt = processed_message.get("prompt")
                        turn_id = processed_message.get("turn_id", "unknown")
                        
                        effective_prompts = chunk_prompt(prompt, 750)
                        
                        last_response = ""
                        for i, p in enumerate(effective_prompts):
                            # Execute the CDP transaction
                            response_text, transaction = run_cdp_transaction(
                                _stdio_client,
                                p,
                                60.0, # Timeout
                                request_id=processed_message.get("message_id")
                            )
                            last_response = response_text
                        
                        _write_framed_message({
                            "protocol": "magy-agent/1",
                            "type": "thought",
                            "session_id": session_id,
                            "message_id": str(uuid.uuid4().hex),
                            "turn_id": turn_id,
                            "text": last_response
                        })
                        
                        # Also send a tool_call or completion based on what's in the text
                        # Rust AgentController handles the actual parsing of ModelManifest.
                    
                    elif msg_type == "tool_result":
                        # Handled by the next assistant_message turn usually
                        pass

                    else:
                        # Fallback for unknown messages
                        _write_framed_message({
                            "protocol": "magy-agent/1",
                            "type": "response",
                            "session_id": session_id,
                            "message_id": str(uuid.uuid4().hex),
                            "original_message_id": processed_message.get("message_id"),
                            "status": "received"
                        })

            except RelayError as e:
                error_details = {
                    "code": e.diagnostics.phase if e.diagnostics else "UNKNOWN_ERROR",
                    "message": e.message,
                    "details": e.diagnostics.last_error if e.diagnostics else str(e)
                }
                _write_framed_message({
                    "protocol": "magy-agent/1",
                    "type": "error",
                    "session_id": incoming_message.get("session_id", "unknown"),
                    "message_id": str(uuid.uuid4().hex),
                    "original_message_id": incoming_message.get("message_id", "unknown"),
                    "error": error_details
                })
            except Exception as e:
                # Catch any other unexpected errors
                _write_framed_message({
                    "protocol": "magy-agent/1",
                    "type": "error",
                    "session_id": incoming_message.get("session_id", "unknown"),
                    "message_id": str(uuid.uuid4().hex),
                    "original_message_id": incoming_message.get("message_id", "unknown"),
                    "error": {
                        "code": "UNEXPECTED_ERROR",
                        "message": f"An unexpected error occurred: {type(e).__name__}",
                        "details": str(e)
                    }
                })
        return 0

    # If not in stdio mode, proceed with existing CDP logic
    try:
        logger = SessionLogger(args.session_id, args.log_dir, quiet=args.quiet)
    except (OSError, ValueError) as exc:
        print(f"relay.py: unable to initialize session logging: {exc}", file=sys.stderr)
        return 1
    request_id = uuid.uuid4().hex
    logger.record(
        "invocation",
        "start",
        request_id=request_id,
        device={"serial": args.serial} if args.serial else None,
        target={"url": args.url},
        outcome="started",
        mode=(
            "self-test" if args.self_test else
            "certification" if args.certify else
            "legacy" if args.legacy else ("desktop-cdp" if args.desktop_cdp else "cdp")
        ),
    )
    client = None
    port = None
    serial = None
    transaction = None
    target_metadata = None
    outcome = "failed"
    error_text = None
    try:
        if args.self_test:
            self_test(logger)
            print("self-test: ok")
            outcome = "success"
            return 0
        if args.certify:
            summary = run_certification(args.iterations, logger=logger)
            if args.json_output:
                print(json.dumps({
                    "session_id": logger.session_id,
                    **summary,
                }, ensure_ascii=False))
            else:
                print(
                    "certification: "
                    f"{summary['certification']} iterations={summary['iterations']} "
                    f"duplicate_submits={summary['duplicate_submits']} "
                    f"false_positives={summary['false_positives']}"
                )
            outcome = "success" if summary["certification"] == "PASS" else "failed"
            return 0 if summary["certification"] == "PASS" else 1
        if args.legacy:
            legacy_args = list(argv if argv is not None else sys.argv[1:])
            filtered: list[str] = []
            skip_value = False
            for item in legacy_args:
                if skip_value:
                    skip_value = False
                    continue
                if item == "--legacy":
                    continue
                if item in ("--session-id", "--log-dir"):
                    skip_value = True
                    continue
                filtered.append(item)
            logger.record("legacy", "delegated", request_id=request_id, outcome="started")
            result = _legacy_main(filtered)
            outcome = "success" if result == 0 else "failed"
            return result
        if args.desktop_cdp:
            client, serial, port = connect_desktop_cdp(
                args.desktop_cdp, args.url, args.timeout
            )
            device_metadata = {"serial": serial, "transport": "desktop-cdp"}
        else:
            client, serial, port = connect_cdp(args.adb, args.serial, args.url, args.timeout)
            device_metadata = {"serial": serial, "adb": resolve_adb(args.adb)}
        target_metadata = getattr(client, "target_metadata", {}) or {"url": args.url}
        if args.quiet:
            print(target_metadata.get("url") or args.url)
        logger.record(
            "connect",
            "target-ready",
            request_id=request_id,
            device=device_metadata,
            target=target_metadata,
            outcome="connected",
        )
        if args.device_smoke_test:
            title = _eval_json(client, "document.title")
            result = {
                "ok": True,
                "session_id": logger.session_id,
                "request_id": request_id,
                "serial": serial,
                "target": target_metadata,
                "title": title or "",
            }
            print(json.dumps(result) if args.json_output else "device-smoke-test: ok")
            outcome = "success"
            return 0
        effective_prompts = chunk_prompt(args.prompt, 750)
        
        last_response = ""
        for i, p in enumerate(effective_prompts):
            is_final = (i == len(effective_prompts) - 1)
            
            if logger:
                logger.record(
                    "prompt_handling",
                    "chunk_sending",
                    request_id=request_id,
                    part=i+1,
                    total=len(effective_prompts),
                    is_final=is_final,
                    outcome="started",
                )

            response, transaction = run_cdp_transaction(
                client,
                p,
                args.timeout,
                logger=logger,
                request_id=request_id,
                device_metadata=device_metadata,
                target_metadata=target_metadata,
            )
            last_response = response

        if args.json_output:
            print(json.dumps({
                "response": last_response,
                "session_id": logger.session_id,
                "request_id": request_id,
                "turn_id": transaction.turn_id,
                "prompt_sha256": transaction.prompt_hash,
                "device": device_metadata,
                "target": target_metadata,
                "chunks": len(effective_prompts)
            }, ensure_ascii=False))
        else:
            print(last_response)
        outcome = "success"
        return 0
    except RelayError as exc:
        error_text = str(exc)
        failure_artifact = None
        if exc.diagnostics is not None:
            exc.diagnostics.session_id = logger.session_id
            exc.diagnostics.request_id = request_id
            if transaction is not None:
                exc.diagnostics.turn_id = transaction.turn_id
            exc.args = (exc.message + exc.diagnostics.render(),)
        if serial and not serial.startswith("desktop:"):
            screenshot_path = logger.log_dir / f"session_{logger.session_id}_failure.png"
            try:
                capture_failure_screenshot(args.adb, serial, screenshot_path)
                logger.record(
                    "diagnostics",
                    "failure-screenshot",
                    request_id=request_id,
                    turn_id=transaction.turn_id if transaction is not None else None,
                    device={"serial": serial},
                    target=getattr(client, "target_metadata", None) if client is not None else None,
                    outcome="captured",
                    path=str(screenshot_path),
                )
                error_text = f"{error_text}; failure_screenshot={screenshot_path}"
                failure_artifact = screenshot_path
            except RelayError as screenshot_error:
                logger.record(
                    "diagnostics",
                    "failure-screenshot",
                    request_id=request_id,
                    turn_id=transaction.turn_id if transaction is not None else None,
                    device={"serial": serial},
                    outcome="failed",
                    error=str(screenshot_error),
                )
        suffix = f"; failure_screenshot={failure_artifact}" if failure_artifact else ""
        print(
            f"relay.py: {exc}{suffix} "
            f"(session_id={logger.session_id}, request_id={request_id})",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()
        if port is not None and serial is not None:
            try:
                run_adb(["forward", "--remove", f"tcp:{port}"], args.adb, serial)
            except RelayError:
                pass
        logger.record(
            "invocation",
            "outcome",
            request_id=request_id,
            turn_id=transaction.turn_id if transaction is not None else None,
            device={"serial": serial} if serial else None,
            target=getattr(client, "target_metadata", None) if client is not None else None,
            outcome=outcome,
            error=error_text,
        )
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
