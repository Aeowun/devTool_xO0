# Relay Architecture

## Overview
The relay is a thin automation layer that connects a user’s Android device to a remote AI assistant workflow. The current implementation uses `adb` plus `uiautomator2` to drive a browser session without the fragile coordinate-tap pattern used in the original prototype.

## Current component flow
```text
Android Studio / local toolchain
              |
              v
         relay.py
              |
              | ADB + uiautomator2
              v
    Connected Android device
              |
              v
      Chrome / ChatGPT WebView
```

## Why this architecture
The older prototype was tightly coupled to a specific device screen, fixed coordinates, and clipboard hacks. The new approach keeps the automation layer generic by selecting views and controls by metadata (`resourceIdMatches`, `textContains`, `className`, and similar attributes) and waiting for the expected state before continuing.

## Practical execution model
1. **Connect**: Ensure `adb devices` shows a live Android target.
2. **Launch**: Start Chrome and open the target ChatGPT URL.
3. **Discover selector**: Find the input field or message composer using view metadata.
4. **Send**: Write the prompt and trigger the send action.
5. **Wait**: Poll the UI until the assistant finishes or the timeout expires.
6. **Read**: Extract the newest response text from the current browser view.
7. **Retry/fallback**: If the view tree is missing, use a deeper browser automation layer such as Appium + Chromedriver.

## Notes on reliability
* Avoid fixed coordinates when possible.
* Use retried selector lookup and a bounded wait loop.
* Treat WebView accessibility as a fallback constraint rather than a given.
* Keep the logic isolated to the relay path so the rest of the codebase remains stable.
