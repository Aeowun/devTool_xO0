# Gemini-ChatGPT Relay

A developer utility for bridging Gemini and ChatGPT on Android devices without relying on screen-coordinate hacks. The current implementation is a Python-based proof of concept that uses `uiautomator2` selectors for robust, device-aware automation.

## Current status

This project is currently focused on the `relay.py` proof of concept, which replaces the brittle ADB coordinate flow with selector discovery and waits. The original Rust architecture files remain useful as a design reference, but the active automation path is the selector-based relay described in this document.

## Features
* **Selector-based automation**: Finds inputs, send controls, and responses by Android view metadata instead of fixed coordinates.
* **ADB + uiautomator2**: Works with a connected USB device and modern browser/WebView layouts.
* **Explicit retries and waits**: Handles UI timing more gracefully than tap-and-keyevent chaining.
* **Smoke validation**: Includes a Python smoke test for the selector logic.

## Prerequisites
* **Android Device**: Connected via USB with debugging enabled.
* **Chrome for Android**: Logged into your ChatGPT account.
* **Python 3**: Installed on your host machine.
* **ADB**: Available in your PATH.
* **uiautomator2**: `python -m pip install -r requirements.txt`

## Setup
1. Enable USB debugging on the Android device.
2. Confirm `adb devices` shows your device in an `device` state.
3. Install the Python requirements.
4. Open Chrome and sign in to ChatGPT.

## Usage
```bash
python relay.py "Your message here"
```

Optional flags:
```bash
python relay.py "Your message here" --serial <device-serial>
python relay.py "Your message here" --timeout 45
python relay.py --dry-run "Your message here"
```

## Selector discovery and debugging
When the browser UI changes or a control cannot be found, inspect the device hierarchy:
```bash
adb shell uiautomator dump /sdcard/ui.xml
adb pull /sdcard/ui.xml .
```
Use `uiautomatorviewer` if it is available in your Android SDK tools.

## Fallback path
If ChatGPT remains inaccessible from `uiautomator2` (for example, the WebView is not exposed to view selectors), use Appium + Chromedriver or a small companion APK with an AccessibilityService/IME bridge.

## Documentation
* [Thin Bridge README](../README_THIN_BRIDGE.md)
* [Architecture](ARCHITECTURE.md)
* [ChatGPT Browser Reconnaissance](CHATGPT_BROWSER.md)
