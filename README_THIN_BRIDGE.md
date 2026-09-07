# Thin Bridge: Gemini ↔ ChatGPT Automation

A lightweight, deterministic relay that uses a connected Android device as a physical bridge between Gemini (in Android Studio) and ChatGPT (in Chrome).

## Architecture
```text
Android Studio (Gemini) <--> relay.py <--> ADB <--> Android Device (ChatGPT)
```

## Prerequisites
1. **Android Device**: Connected via USB with Debugging enabled.
2. **Chrome for Android**: Logged into your ChatGPT account.
3. **Python 3**: Installed on your host machine.
4. **ADB**: Available in your PATH (usually in `AppData/Local/Android/Sdk/platform-tools`).

## How to Use

### 1. The Automation Script (`relay.py`)
This script handles the "Heavy Lifting" of UI interaction:
* Force-foregrounds ChatGPT.
* Clears the composer.
* Injects your message via ADB.
* Polls for completion (locally, saving tokens).
* Extracts the response text and writes it to `from_chatgpt.txt`.

### 2. Manual Command
You can trigger a relay turn manually from your terminal:
```bash
python relay.py "Your message here"
```

### 3. Integrated Loop (Gemini Workflow)
Ask me (Gemini) to perform an audit or task using the bridge:
> "Gemini, audit `main.rs` and send the findings to ChatGPT via the Thin Bridge."

I will then:
1. Write the findings to a temporary string.
2. Execute `python relay.py [findings]`.
3. Read `from_chatgpt.txt` to get ChatGPT's critique.
4. Update the code based on the feedback.

## Token Efficiency
| Interaction Method | Host Token Cost | Reliability |
| :--- | :--- | :--- |
| **Gemini Native UI** | ~20,000+ per turn | Medium |
| **Thin Bridge (relay.py)** | **~200 per turn** | **High** |

## Maintenance
If the ChatGPT UI changes (e.g., button moves), update the coordinates in `relay.py`:
* `COMPOSER_TAP`: Where you tap to type.
* `SEND_TAP`: The blue "Send" arrow.
