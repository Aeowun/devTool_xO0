# ChatGPT Browser Reconnaissance

## Environment
* **URL**: `https://chat.openai.com/` or the current ChatGPT domain in your browser.
* **Browsers**: Chrome and Edge are both viable options.
* **Automation strategy**: The active relay path prefers Android view selectors over direct browser DOM automation when the page is not exposed via remote debugging.

## Observed mechanisms

### 1. Android view automation (`uiautomator2`)
* **Status**: Current default for the relay proof of concept.
* **Details**: The relay locates the ChatGPT composer and send button using Android view metadata, waits for them to appear, and interacts with them without relying on a fixed screen coordinate grid.
* **Benefits**: Works well when the browser and WebView expose enough accessibility/view information to the device hierarchy.

### 2. Browser automation via CDP
* **Status**: Best fallback when the page is accessible through Chrome DevTools.
* **Details**: Start Chrome with remote debugging enabled and interact with the page DOM (for example `textarea#prompt-textarea` and a send button by test id).
* **Tooling**: `playwright`, `puppeteer`, or a Rust CDP client.

## Selector guidance for the current relay
The current proof-of-concept is intentionally generic and uses a set of selectors such as:
* **Composer / input**: `resourceIdMatches`, `textContains`, `className`, `descriptionContains`
* **Send button**: `resourceIdMatches`, `contentDescriptionMatches`, `textContains`
* **Generated response**: text nodes exposed through the current conversation view

## Recommended next-step strategy
1. **Use `uiautomator2` first**: It offers the least brittle path for Android device automation.
2. **Fall back to CDP**: If the WebView is opaque or inaccessible, remote debugging can expose the page DOM.
3. **Use Appium / companion APK**: If both browser-level and view-level paths fail, a companion service may be required to expose the relevant content.

## Practical note
The goal is to avoid the brittle tap and clipboard choreography seen in the original `relay.py` script. The selector-driven approach keeps the relay maintainable even when the Chrome UI changes a little between versions.