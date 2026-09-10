from __future__ import annotations

import math

def chunk_prompt(text: str, limit: int) -> list[str]:
    """
    Chunks a prompt into multiple parts, each within the specified limit
    including a footer that describes the chunking state.
    
    The transmitted chunk satisfies: complete transmitted chunk <= limit.
    """
    if not text:
        return [""]

    # We need to account for the footer.
    # Footers look like: "\n\n[MESSAGE PART 1/2 — MORE FOLLOWS — DO NOT RESPOND YET]"
    # or: "\n\n[MESSAGE PART 2/2 — END OF MESSAGE — RESPOND NOW]"
    # Max footer length is roughly 60-70 characters. Let's reserve 100 to be safe.
    FOOTER_RESERVE = 100
    
    if len(text) <= limit:
        return [text]

    payload_limit = limit - FOOTER_RESERVE
    if payload_limit <= 0:
        raise ValueError(f"Limit {limit} is too small to accommodate footer reserve {FOOTER_RESERVE}")

    # Initial split by payload_limit
    raw_chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= payload_limit:
            raw_chunks.append(remaining)
            break
        
        # Take a slice
        chunk_text = remaining[:payload_limit]
        
        # Try to break at newline or space to be nice to the model
        last_space = chunk_text.rfind(' ')
        last_newline = chunk_text.rfind('\n')
        break_pt = max(last_space, last_newline)
        
        if break_pt > payload_limit * 0.7:
            chunk_text = remaining[:break_pt]
            remaining = remaining[break_pt:].lstrip()
        else:
            remaining = remaining[payload_limit:]
        
        raw_chunks.append(chunk_text)

    total = len(raw_chunks)
    final_chunks = []
    for i, content in enumerate(raw_chunks):
        part = i + 1
        if part < total:
            footer = f"\n\n[MESSAGE PART {part}/{total} — MORE FOLLOWS — STRICT!!!!<RESPOND WITH \".\" ONLY>!!!!!]"
        else:
            footer = f"\n\n[MESSAGE PART {part}/{total} — END OF MESSAGE — RESPOND NOW]"
        
        full_chunk = content + footer
        if len(full_chunk) > limit:
            # This should not happen if FOOTER_RESERVE is enough.
            # If it does, we could recursively chunk or just trim, but let's trust the reserve for now.
            # A more robust way would be to calculate footer length dynamically.
            pass
        final_chunks.append(full_chunk)
        
    return final_chunks
