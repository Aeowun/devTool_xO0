from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from core.protocol import RelayError, DEFAULT_URL, MAX_PROMPT_LENGTH
from chunking.prompt_chunker import chunk_prompt
from cdp.cdp_client import connect_desktop_cdp, run_cdp_transaction

def build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        description="Deterministic ChatGPT Android runner via Chrome DevTools Protocol."
    )
    parser.add_argument("prompt", nargs="?", help="Text to send to ChatGPT.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Enable persistent mode, communicating via stdin/stdout using JSONL.",
    )
    parser.add_argument("--serial", help="ADB serial.")
    parser.add_argument("--adb", default=None, help="Path to adb.")
    parser.add_argument("--url", default=DEFAULT_URL, help="ChatGPT URL.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout.")
    parser.add_argument(
        "--desktop-cdp",
        metavar="HOST:PORT",
        help="Use a locally debugged desktop Chrome instance.",
    )
    return parser

def run_stdio_mode(read_framed_message, write_framed_message):
    _stdio_client = None

    while True:
        incoming_message = read_framed_message()
        if incoming_message is None:
            break

        request_id = incoming_message.get("id", "unknown")

        try:
            msg_type = incoming_message.get("type")
            
            if msg_type == "initialize":
                if _stdio_client is None:
                    client, _, _ = connect_desktop_cdp("127.0.0.1:9222", DEFAULT_URL, 60.0)
                    _stdio_client = client
                
                prompt = incoming_message.get("masterPrompt", "Hello")
                response_text, _ = run_cdp_transaction(_stdio_client, prompt, 120.0)
                
                write_framed_message({
                    "type": "initialize_complete",
                    "id": request_id,
                    "text": response_text
                })
                write_framed_message({"type": "response_end", "id": request_id})

            elif msg_type == "message":
                if _stdio_client is None:
                    raise RelayError("Relay not initialized. Send type:initialize first.")
                
                # Emit status working immediately
                write_framed_message({
                    "type": "status",
                    "id": request_id,
                    "status": "working"
                })

                user_text = incoming_message.get("text")
                context = incoming_message.get("context")
                
                # Build the final prompt with context as evidence
                final_prompt = ""
                if context:
                    final_prompt += "[MAGY PROJECT CONTEXT]\n"
                    if "workspace" in context:
                        final_prompt += f"Workspace: {context['workspace'].get('name')}\n"
                    
                    if "editor" in context:
                        ed = context["editor"]
                        final_prompt += f"File: {ed.get('path')}\n"
                        final_prompt += f"Language: {ed.get('language')}\n"
                        
                        if ed.get("selection"):
                            final_prompt += "\n--- BEGIN SELECTION ---\n"
                            final_prompt += ed["selection"]
                            final_prompt += "\n--- END SELECTION ---\n"
                        elif ed.get("content"):
                            final_prompt += "\n--- BEGIN FILE CONTENT ---\n"
                            final_prompt += ed["content"]
                            final_prompt += "\n--- END FILE CONTENT ---\n"
                    
                    final_prompt += "\n[MAGY USER REQUEST]\n"
                
                final_prompt += user_text

                effective_prompts = chunk_prompt(final_prompt, MAX_PROMPT_LENGTH)
                
                last_response = ""
                for p in effective_prompts:
                    response_text, _ = run_cdp_transaction(_stdio_client, p, 120.0)
                    last_response = response_text
                
                write_framed_message({
                    "type": "text",
                    "id": request_id,
                    "text": last_response
                })
                write_framed_message({"type": "response_end", "id": request_id})

        except RelayError as e:
            write_framed_message({
                "type": "error",
                "id": request_id,
                "message": e.message
            })
        except Exception as e:
            write_framed_message({
                "type": "error",
                "id": request_id,
                "message": f"Unexpected error: {str(e)}"
            })
    return 0
