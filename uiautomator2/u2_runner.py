from __future__ import annotations

import shlex
import time
import uuid
import hashlib
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.protocol import (
    RelayState, RecoveryTier, FailureDiagnostics, RelayError,
    DEFAULT_BROWSER_PACKAGE, DEFAULT_URL
)
from adb.adb_client import run_adb, ensure_device

# The supported runner deliberately has no third-party dependency.  Keep this
# name for the opt-in legacy implementation; it is imported only on demand.
u2 = None

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

def connect_device(serial: str | None = None, adb_path: str | None = None):
    global u2
    if u2 is None:
        try:
            import uiautomator2 as u2_module
        except ImportError as exc:
            raise RelayError(
                "Legacy mode requires uiautomator2. Install it separately or use the CDP runner."
            ) from exc
        u2 = u2_module
    device = u2.connect(serial) if serial else u2.connect()
    try:
        time.sleep(1)
        device.shell("echo ready")
        dismiss_overlays(device)
    except Exception as exc:
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
    import argparse
    parser = argparse.ArgumentParser(description="Selector-based Android relay using uiautomator2.")
    parser.add_argument("prompt", nargs="?", default="", help="Text to send to the browser assistant.")
    parser.add_argument("--serial", default=None, help="ADB serial to target when multiple devices are connected.")
    parser.add_argument("--adb", default=None, help="Path to adb executable.")
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

def legacy_main(argv: list[str] | None = None) -> int:
    parser = _build_legacy_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"Dry run: would use Chrome package {DEFAULT_BROWSER_PACKAGE} and URL {args.url}")
        return 0
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
        print(f"relay.py (legacy): {exc}", file=sys.stderr)
        return 1
    return 0
