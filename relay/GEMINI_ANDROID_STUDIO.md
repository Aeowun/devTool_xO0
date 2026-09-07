# Gemini Android Studio Reconnaissance

## Environment
* **IDE**: Android Studio (Flamingo/Giraffe or later, based on build)
* **OS**: Windows 11
* **Process**: `studio64.exe`

## Observed Mechanisms

### 1. UI Automation (Accessibility)
* **Status**: Brittle / Limited.
* **Details**: Android Studio is a Swing-based application. While it supports accessibility, the Gemini tool window is often a **JCEF (Java Chromium Embedded Framework)** component.
* **Limitation**: Standard `UIAutomation` (Windows Accessibility API) sees the JCEF container but cannot inspect the inner DOM of the Chromium instance without remote debugging enabled for JCEF.

### 2. Clipboard Integration
* **Status**: Most Reliable (Initial).
* **Details**: Can capture messages by focusing the window and sending `Ctrl+A` / `Ctrl+C`.
* **Workflow**:
    1. Identify `studio64.exe` window.
    2. Locate the "Gemini" ToolWindow handle.
    3. Send keystrokes to extract/input text.

### 3. JCEF Remote Debugging
* **Status**: Potential.
* **Details**: If Android Studio is started with `-Dide.browser.jcef.debug.port=xxxx`, we can use CDP to automate the Gemini UI exactly like a browser.
* **Verification Required**: Check if this property can be set via `idea.properties` or VM options.

## Proposed Adapter Strategy
The `GeminiAndroidStudioAdapter` will use a hybrid approach:
1. **Window Management**: Use Windows API (via Rust `windows` crate) to find the Android Studio window and the specific Gemini sub-pane.
2. **Text Extraction**: Fallback to clipboard/keystroke automation if JCEF debugging is unavailable.
3. **Text Injection**: Focus input area and `send_keys`.
