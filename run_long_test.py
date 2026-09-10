import sys
from cli.main import run_cli

if __name__ == "__main__":
    with open("long_prompt.txt", "r", encoding="utf-8") as f:
        prompt = f.read()
    
    # We simulate sys.argv
    sys.argv = ["relay.py", "--desktop-cdp", "127.0.0.1:9222", prompt]
    sys.exit(run_cli())
