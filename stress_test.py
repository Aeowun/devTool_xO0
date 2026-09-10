from cli.main import run_cli
import sys
import time
import os
import psutil

def get_rss():
    return psutil.Process().memory_info().rss

if __name__ == "__main__":
    session_id = f"stress-test-{int(time.time())}"
    print(f"Starting PROGRESSIVE stress test with session_id: {session_id}")
    
    # Test cases: (Prompt type, prompt text, expected response length description)
    test_cases = [
        ("Short", "Write a 1-sentence greeting.", "1 sentence"),
        ("Short", "Write a 2-sentence greeting.", "2 sentences"),
        ("Medium", "Write a 1-paragraph explanation of how a car engine works.", "1 paragraph"),
        ("Medium", "Write a 2-paragraph explanation of photosynthesis.", "2 paragraphs"),
        ("Long", "Write a 5-paragraph essay about the history of the internet.", "5 paragraphs"),
        ("Long", "Write a 10-paragraph story about a robot exploring a desert planet.", "10 paragraphs"),
        ("Very Long", "Generate a list of 50 fun facts about space, each as a separate paragraph.", "50 paragraphs"),
        ("Very Long", "Generate a list of 100 random words, each on a new line.", "100 lines"),
        ("Exhaustive", "Write a very detailed technical guide on building a modular Python application, aiming for at least 1500 words.", "Huge response"),
        ("Final", "Final short check. Reply with 'Done'.", "Short"),
    ]
    
    for i, (p_type, p_content, exp_len) in enumerate(test_cases, 1):
        print(f"\n=== Turn {i} [{p_type}] ===")
        print(f"Goal: {exp_len}")
        
        rss_before = get_rss()
        prompt_size = len(p_content)
        
        start_time = time.perf_counter()
        
        # We reuse the same session_id to maintain history
        sys.argv = ["relay.py", "--desktop-cdp", "127.0.0.1:9222", "--session-id", session_id, p_content]
        
        try:
            exit_code = run_cli()
            success = (exit_code == 0)
        except Exception as e:
            print(f"EXCEPTION in turn {i}: {e}")
            success = False
            
        end_time = time.perf_counter()
        rss_after = get_rss()
        
        duration = end_time - start_time
        print(f"Result: {'SUCCESS' if success else 'FAILED'}")
        print(f"Prompt Size: {prompt_size} chars")
        print(f"Duration: {duration:.2f}s")
        print(f"RSS Change: {rss_after - rss_before:+} bytes (Final: {rss_after})")
        
        if not success:
            print("Stopping stress test due to failure.")
            break
            
        time.sleep(3) # Wait for UI to settle
