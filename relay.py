#!/usr/bin/env python3
"""Thin entry point for the modularized Android relay."""

import sys
import io
import json
import uuid

# Force UTF-8 for standard streams to prevent charmap crashes on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from cli.main import run_cli
from cli import stdio_runner

def main():
    # If --stdio is passed, we enter the persistent loop here
    if "--stdio" in sys.argv:
        def read_framed():
            line = sys.stdin.readline()
            if not line: return None
            try:
                return json.loads(line)
            except:
                return None

        def write_framed(msg):
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        return stdio_runner.run_stdio_mode(read_framed, write_framed)
    
    # Otherwise, run the normal one-shot CLI
    return run_cli()

if __name__ == "__main__":
    sys.exit(main())
