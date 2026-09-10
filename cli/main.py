from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from core.protocol import RelayError, MAX_PROMPT_LENGTH
from diagnostics.session_logger import SessionLogger
from adb import adb_client
from cdp import cdp_client
from uiautomator2 import u2_runner
from cli import stdio_runner
from chunking.prompt_chunker import chunk_prompt

def run_cli(argv: list[str] | None = None) -> int:
    parser = stdio_runner.build_parser() # Shared parser
    args = parser.parse_args(argv)
    
    if args.stdio:
        def read_framed():
            line = sys.stdin.readline()
            if not line: return None
            try: return json.loads(line)
            except: return None

        def write_framed(msg):
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        return stdio_runner.run_stdio_mode(read_framed, write_framed)

    # Standard one-shot mode
    session_id = getattr(args, 'session_id', None) or uuid.uuid4().hex
    log_dir = getattr(args, 'log_dir', None)
    quiet = getattr(args, 'quiet', False)

    try:
        logger = SessionLogger(session_id, log_dir, quiet=quiet)
    except (OSError, ValueError) as exc:
        print(f"relay: unable to initialize session logging: {exc}", file=sys.stderr)
        return 1

    request_id = uuid.uuid4().hex
    
    # Standard CDP Orchestration
    client = None
    try:
        if args.desktop_cdp:
            client, serial, port = cdp_client.connect_desktop_cdp(args.desktop_cdp, args.url, args.timeout)
        else:
            client, serial, port = cdp_client.connect_cdp(args.adb, args.serial, args.url, args.timeout)
        
        effective_prompts = chunk_prompt(args.prompt, MAX_PROMPT_LENGTH)
        last_response = ""
        for p in effective_prompts:
            response, _ = cdp_client.run_cdp_transaction(client, p, args.timeout, logger=logger, request_id=request_id)
            last_response = response

        print(last_response)
        return 0
    except RelayError as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 1
    finally:
        if client: client.close()
