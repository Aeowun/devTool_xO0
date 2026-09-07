# Relay Architecture

## Overview
The relay is a standalone Rust utility that acts as a message broker between Gemini (in Android Studio) and ChatGPT (in a browser).

## Component Diagram
```text
             +-------------------+
             |      Bridge       |
             | (State, Database) |
             +---------+---------+
                       |
        +--------------+--------------+
        |                             |
+-------v-------+             +-------v-------+
| GeminiAdapter |             | ChatGPTAdapter|
| (WinAPI/AS)   |             | (CDP/Browser) |
+-------+-------+             +-------+-------+
        |                             |
        v                             v
 Android Studio                Browser (Chrome)
 (JCEF / UI)                   (ChatGPT Web)
```

## Data Model (SQLite)
* **Sessions**: `id`, `start_time`, `end_time`, `description`.
* **Messages**: `id`, `session_id`, `turn_id`, `sender` (GEMINI/CHATGPT), `content`, `timestamp`, `status`.

## Turn Logic
1. **Poll Gemini**: Check for new response from Gemini.
2. **Persist**: Store message in SQLite.
3. **Forward to ChatGPT**: Send the message to the browser.
4. **Poll ChatGPT**: Wait for full response generation.
5. **Persist**: Store response.
6. **Forward to Gemini**: Inject text back into Android Studio.
7. **Repeat**: Until max turns reached or loop detected.

## Duplicate Detection
Hash message content + turn index. If a message with the same hash exists for the current turn, skip forwarding.
