# Local Relay (devTool_xO0)

`Local_Relay` is a specialized high-integrity bridge between local tools and ChatGPT. It operates through the Chrome DevTools Protocol (CDP) to drive an authenticated local Chrome instance, enabling complex agentic workflows without requiring an official OpenAI API key.

## Key Features

### 📏 Automatic Message Splitting
The relay automatically handles instruction overflow. If a prompt exceeds **750 characters**, the relay slices it into manageable chunks. It uses a robust UI-state handshake to deliver the entire message piece-by-piece, ensuring ChatGPT receives the complete mission before responding.

### 🛡️ Guarded Transactions
Every submission is a one-shot transaction. The relay observes the UI before the action, performs a guarded click, and reconciles the result. It strictly avoids "double-sending" or accidental duplicate messages by monitoring the browser's generation state.

### 📶 Persistent Session Logic
Supports multi-stage conversations through `--tab-session`. Reuse the same ChatGPT tab to preserve context across multiple tool calls or reasoning steps.

## Requirements

- Windows with Google Chrome installed.
- A ChatGPT session already signed in.
- Chrome launched with remote debugging enabled.
- Python 3.10 or newer.

## Setup & Launch

### 1. Launch Debug Chrome
Chrome must be started with a dedicated profile and debugging port. Close all existing Chrome windows first:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\Relay Data" `
  --profile-directory=Default `
  https://chatgpt.com/
```

### 2. Run the Relay
Send instructions directly from your terminal or another application:

```powershell
python relay.py --desktop-cdp 127.0.0.1:9222 "Your engineering mission here..."
```

## Advanced Usage

### Automatic Chunking
If you send a large block of code or a long instruction, the relay will automatically output progress markers:
`[MESSAGE PART 1/3 — MORE FOLLOWS — DO NOT RESPOND YET]`

It will wait for the browser to settle between parts and only return the final assistant response.

### Persistent Tabs
```powershell
# Start session
python relay.py --desktop-cdp 127.0.0.1:9222 --tab-session my-work "Stage 1"
# Continue in same tab
python relay.py --desktop-cdp 127.0.0.1:9222 --tab-session my-work "Stage 2"
```

## Validation

Run the regression suite to verify CDP connectivity and selector reliability:

```powershell
python smoke_test.py
```

---
*Part of the Magy Autonomous Engine ecosystem.*
