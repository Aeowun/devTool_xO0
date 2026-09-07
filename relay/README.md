# Gemini-ChatGPT Relay

A standalone developer utility to automate the copy/paste loop between Gemini inside Android Studio and ChatGPT in the browser.

## Features
* **Two-way Automation**: Hands-free conversation between two AI assistants.
* **Rust Core**: Fast, reliable, and memory-safe.
* **SQLite Persistence**: Full history of all exchanges.
* **Loop Detection**: Automatically stops if the assistants start repeating themselves.
* **Manual Intervention**: Pause, resume, or inject messages at any time.

## Prerequisites
* **Android Studio**: Installed and running with Gemini enabled.
* **Browser**: Chrome or Edge recommended.
* **Rust**: `cargo` must be in PATH.

## Setup
1. Enable Remote Debugging in your browser (optional but recommended for ChatGPT adapter).
2. Configure Gemini ToolWindow to be visible in Android Studio.

## Usage
```bash
cargo run -- start
```

## Documentation
* [Architecture](ARCHITECTURE.md)
* [Gemini Reconnaissance](GEMINI_ANDROID_STUDIO.md)
* [ChatGPT Reconnaissance](CHATGPT_BROWSER.md)
