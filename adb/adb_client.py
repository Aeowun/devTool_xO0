from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from core.protocol import RelayError, DEFAULT_ADB

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

def adb_devices(adb_path: str | None) -> list[str]:
    output = run_adb(["devices"], adb_path=adb_path)
    devices = []
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])
    return devices

def choose_device(adb_path: str | None, serial: str | None) -> str:
    devices = adb_devices(adb_path)
    if serial:
        if serial not in devices:
            raise RelayError(f"Device {serial!r} is not connected and authorized.")
        return serial
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise RelayError("No authorized Android device found via adb.")
    raise RelayError("Multiple Android devices found; pass --serial for deterministic selection.")
