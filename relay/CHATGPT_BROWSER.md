# ChatGPT Browser Reconnaissance

## Environment
* **URL**: `https://chatgpt.com/`
* **Browsers**: Chrome, Edge (running).

## Observed Mechanisms

### 1. Chrome DevTools Protocol (CDP)
* **Status**: Highly Reliable.
* **Details**: By starting Chrome with `--remote-debugging-port=9222`, the relay can connect directly to the browser, inspect the DOM, and interact with elements without OS-level mouse/keyboard simulation.
* **Rust Tooling**: `headless_chrome` or `chromium-oxide` (Rust crates).

### 2. DOM Analysis (Current ChatGPT)
* **Selectors (Subject to change)**:
    * **Composer**: `textarea#prompt-textarea`
    * **Submit Button**: `button[data-testid="send-button"]`
    * **Last Response**: `div.agent-turn` or `div[data-testid^="conversation-turn"]`
    * **Generating State**: Presence of `button[data-testid="stop-button"]`.

## Proposed Adapter Strategy
The `ChatGPTAdapter` will:
1. **Connect**: Attempt to connect to a running browser instance via CDP.
2. **Sync**: Find the active tab with `chatgpt.com`.
3. **Automate**: 
    * `send_message`: Type into `#prompt-textarea` and click send.
    * `wait_for_response`: Poll for the disappearance of the stop button and appearance of a new `assistant` turn.
    * `read_latest_message`: Extract text from the last `div` with assistant role.
