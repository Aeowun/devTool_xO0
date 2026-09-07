# Thin Bridge: Gemini ↔ ChatGPT Automation

A lightweight, deterministic relay that uses a connected Android device as a physical bridge between Gemini (in Android Studio) and ChatGPT (in Chrome).

## Architecture
```text
Android Studio (Gemini) <--> relay.py <--> ADB + uiautomator2 <--> Android Device (ChatGPT)
```

## Why this refactor

The original relay relied on hard-coded screen coordinates, clipboard broadcasts, and `keyevent` sequences that break on modern Android and WebView layouts. The new implementation replaces those brittle assumptions with selector-based automation using `uiautomator2`, which is much more robust across device and browser updates.

## Prerequisites
1. **Android Device**: Connected via USB with Debugging enabled.
2. **Chrome for Android**: Logged into your ChatGPT account.
3. **Python 3**: Installed on your host machine.
4. **ADB**: Available in your PATH (usually in `AppData/Local/Android/Sdk/platform-tools`).
5. **uiautomator2**: Installed via `python -m pip install -r requirements.txt`.

## How to Use

### 1. Install dependencies
```bash
python -m pip install -r requirements.txt
```

### 2. Run a prompt through the relay
```bash
python relay.py "Your message here"
```

This script now uses selector discovery (`resourceIdMatches`, `textContains`, `className`, etc.) instead of fixed coordinates.

### 3. Selector discovery and debugging
When a UI element is missing or moved, use one of the following to inspect the current hierarchy:
```bash
adb shell uiautomator dump /sdcard/ui.xml
adb pull /sdcard/ui.xml .
```
You can also inspect elements with `uiautomatorviewer` if it is available in the Android SDK.

### 4. Fallback guidance
If ChatGPT content remains inaccessible via `uiautomator2`, the next step is a more advanced automation layer such as Appium + Chromedriver or a small companion APK that exposes the WebView through AccessibilityService or an IME-based bridge.

## Validation
The repository includes `smoke_test.py` to exercise the selector discovery path and ensure the relay remains importable and functioning in a test harness.

```bash
python smoke_test.py
```

## Maintenance
The relay keeps a set of generic selector candidates rather than a device-specific coordinate grid, so when the UI changes you update the selectors and retry instead of editing numeric tap coordinates.
