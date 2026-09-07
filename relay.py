#!/usr/bin/env python3
"""uiautomator2-based relay proof of concept.

This script replaces the fragile coordinate and clipboard-based relay flow with
selector-driven automation against a connected Android device. It is intentionally
kept generic so the same logic can be reused for modern Chrome/ChatGPT UIs while
still accepting the old CLI style: `python relay.py "Tell me something"`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    import uiautomator2 as u2
except ImportError:  # pragma: no cover - exercised by smoke tests via import guard
    u2 = None

DEFAULT_ADB = os.environ.get("ADB_PATH", "adb")
DEFAULT_BROWSER_PACKAGE = "com.android.chrome"
DEFAULT_URL = "https://chat.openai.com/"


class RelayError(RuntimeError):
    """Raised when the device or UI cannot be reached."""


@dataclass
class SelectorConfig:
    app_package: str = DEFAULT_BROWSER_PACKAGE
    input_selectors: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"resourceIdMatches": r".*(prompt|composer|textarea|message_input).*"},
            {"textContains": "Ask anything"},
            {"className": "android.widget.EditText"},
            {"className": "android.webkit.WebView"},
        ]
    )
    send_selectors: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"resourceIdMatches": r".*(send|submit|send_button|composer_send).*"},
            {"contentDescriptionMatches": r".*(send|submit).*"},
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


def run_adb(args: Iterable[str]) -> str:
    result = subprocess.run(
        [DEFAULT_ADB, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and not result.stdout.strip():
        raise RelayError(f"ADB command failed: {' '.join(args)}\n{output}")
    return output.strip()


def ensure_device(serial: str | None = None) -> None:
    devices = run_adb(["devices"])
    if not devices or "List of devices attached" not in devices:
        raise RelayError("No Android device found via adb. Enable USB debugging and reconnect.")
    if serial:
        if serial not in devices:
            raise RelayError(f"Device {serial!r} was not found via adb.")
    elif "device" not in devices.lower():
        raise RelayError("ADB is available but no device is attached in an active state.")


def connect_device(serial: str | None = None):
    if u2 is None:
        raise RelayError("uiautomator2 is not installed. Run: python -m pip install -r requirements.txt")
    device = u2.connect(serial) if serial else u2.connect()
    try:
        device.wait(3)
        device.shell("echo ready")
    except Exception as exc:  # pragma: no cover - depends on the Android device runtime
        raise RelayError(f"Unable to connect to device: {exc}") from exc
    return device


def find_first_match(device: Any, selectors: Iterable[dict[str, str]], timeout_s: float = 10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                match = device(**selector)
            except TypeError:
                continue
            if match and getattr(match, "exists", None):
                try:
                    if match.exists:
                        return match
                except Exception:
                    return match
        time.sleep(0.25)
    return None


def wait_for_text(device: Any, selectors: Iterable[dict[str, str]], timeout_s: float, label: str):
    element = find_first_match(device, selectors, timeout_s=timeout_s)
    if element is None:
        raise RelayError(f"Could not locate the {label} element using selector-based discovery.")
    return element


def open_chatgpt(device: Any, url: str = DEFAULT_URL) -> None:
    try:
        device.app_start(DEFAULT_BROWSER_PACKAGE)
    except Exception:
        pass
    time.sleep(1)
    if url:
        try:
            device.shell(f"am start -a android.intent.action.VIEW -d {url}")
        except Exception:
            pass
    time.sleep(2)


def activate_input(device: Any, selectors: SelectorConfig) -> Any:
    element = wait_for_text(device, selectors.input_selectors, selectors.timeout_s, "input")
    try:
        element.click()
    except Exception:
        pass
    return element


def send_message(device: Any, prompt: str, selectors: SelectorConfig) -> str:
    input_element = activate_input(device, selectors)
    try:
        if hasattr(input_element, "clear_text"):
            input_element.clear_text()
    except Exception:
        pass
    try:
        input_element.set_text(prompt)
    except Exception:
        raise RelayError("Unable to write the prompt into the browser input field.")

    send_element = wait_for_text(device, selectors.send_selectors, selectors.timeout_s, "send button")
    try:
        send_element.click()
    except Exception:
        try:
            device.press("enter")
        except Exception:
            raise RelayError("The input was set, but the send control could not be activated.")
    return prompt


def read_response(device: Any, selectors: SelectorConfig, timeout_s: float | None = None) -> str:
    deadline = time.monotonic() + (selectors.timeout_s if timeout_s is None else timeout_s)
    last_text = ""
    while time.monotonic() < deadline:
        text_nodes = []
        for selector in selectors.response_selectors:
            try:
                match = device(**selector)
            except TypeError:
                continue
            if match and getattr(match, "exists", None):
                try:
                    if match.exists:
                        value = getattr(match, "text", None)
                        if callable(value):
                            value = value()
                        if value:
                            text_nodes.append(str(value).strip())
                except Exception:
                    continue
        if text_nodes:
            candidate = "\n".join(filter(None, text_nodes))
            if candidate != last_text:
                last_text = candidate
            if "Stop generating" not in candidate and "Generating" not in candidate and candidate:
                return candidate
        time.sleep(1.0)
    if last_text:
        return last_text
    raise RelayError("No response was found in the ChatGPT UI after the timeout window.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Selector-based Android relay using uiautomator2.")
    parser.add_argument("prompt", nargs="?", default="", help="Text to send to the browser assistant.")
    parser.add_argument("--serial", default=None, help="ADB serial to target when multiple devices are connected.")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to open in Chrome before sending the message.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout used during selector discovery and response polling.")
    parser.add_argument("--dry-run", action="store_true", help="Print the selected action without interacting with a device.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"Dry run: would use Chrome package {DEFAULT_BROWSER_PACKAGE} and URL {args.url}")
        return 0

    if not args.prompt:
        parser.error("A prompt is required unless --dry-run is used.")

    try:
        ensure_device(args.serial)
        device = connect_device(args.serial)
        cfg = SelectorConfig(timeout_s=args.timeout)
        open_chatgpt(device, args.url)
        send_message(device, args.prompt, cfg)
        response = read_response(device, cfg)
        print(response)
        return 0
    except RelayError as exc:
        print(f"relay.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
