import subprocess
import time
import xml.etree.ElementTree as ET
import sys
import re

ADB_PATH = r"C:\Users\fixit\AppData\Local\Android\Sdk\platform-tools\adb.exe"

def run_adb(args):
    result = subprocess.run([ADB_PATH] + args, capture_output=True, text=True, encoding='utf-8')
    return result.stdout

def get_xml(filename="view.xml"):
    for _ in range(3):
        run_adb(["shell", "uiautomator", "dump", f"/sdcard/{filename}"])
        run_adb(["pull", f"/sdcard/{filename}", filename])
        try:
            return ET.parse(filename).getroot()
        except:
            time.sleep(1)
    return None

def find_element(root, attr, val):
    if root is None: return None
    for node in root.iter():
        if val in node.get(attr, ""):
            return node
    return None

def get_coords(node):
    if node is None: return None
    bounds = node.get("bounds", "")
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None

def guaranteed_clear():
    print("Guaranteed clearing of composer...")
    # 1. Tap Composer
    run_adb(["shell", "input", "tap", "540", "2100"])
    time.sleep(0.3)
    
    # 2. Triple Attempt: Select All + Delete
    for _ in range(3):
        # Move cursor to end to ensure field has hard focus
        run_adb(["shell", "input", "keyevent", "123"]) # KEYCODE_MOVE_END
        time.sleep(0.1)
        # Select All (Ctrl+A)
        run_adb(["shell", "input", "keyevent", "--metaState", "4096", "29"])
        time.sleep(0.1)
        # Backspace
        run_adb(["shell", "input", "keyevent", "67"])
        time.sleep(0.2)
    
    # 3. Final verification - if still not empty, spam 50 backspaces
    root = get_xml("verify_clear.xml")
    node = find_element(root, "resource-id", "prompt-textarea")
    if node and node.get("text", "") not in ["", "Ask anything"]:
        print("Selection clear failed. Spamming backspaces...")
        for _ in range(50):
            run_adb(["shell", "input", "keyevent", "67"])
    
    # 4. Hide keyboard
    run_adb(["shell", "input", "keyevent", "KEYCODE_BACK"])
    time.sleep(0.3)

def send_message(text):
    print("--- Sending Prompt ---")
    run_adb(["shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"])
    time.sleep(1)
    
    # Ensure fresh start
    guaranteed_clear()
    
    # 1. Put prompt in clipboard
    run_adb(["shell", "am", "broadcast", "-a", "ch.pete.adbclipboard.WRITE", "--es", "text", text])
    time.sleep(0.3)
    
    # 2. Focus & Paste
    run_adb(["shell", "input", "tap", "540", "2100"])
    time.sleep(0.3)
    run_adb(["shell", "input", "keyevent", "279"]) # Paste
    time.sleep(0.3)
    
    # 3. Send (Coordinates from latest UI check)
    run_adb(["shell", "input", "tap", "950", "2060"])
    print("Sent.")
    time.sleep(0.5)
    run_adb(["shell", "input", "keyevent", "KEYCODE_BACK"]) # Hide keyboard
    return True

def wait_for_gpt():
    print("Waiting for response...", end="", flush=True)
    time.sleep(4) # Minimum generation time
    for _ in range(40):
        root = get_xml("s.xml")
        if root is None: continue
        xml_str = ET.tostring(root, encoding='unicode')
        if "Stop generating" not in xml_str and "Stop message" not in xml_str:
            print(" Done.")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    return False

def extract_response():
    print("--- Extracting via Composer Scrape ---")
    # 1. Scroll to end
    run_adb(["shell", "input", "swipe", "500", "1800", "500", "400", "400"])
    time.sleep(0.5)
    
    # 2. Tap Copy (Coordinates from bubble)
    run_adb(["shell", "input", "tap", "75", "1150"])
    time.sleep(1.0)
    
    # 3. Paste into Composer
    run_adb(["shell", "input", "tap", "540", "2100"])
    time.sleep(0.5)
    run_adb(["shell", "input", "keyevent", "279"]) # Paste
    time.sleep(2.0) # Critical: wait for XML sync
    
    # 4. Scrape
    root = get_xml("scratch.xml")
    result = "Error: Scrape failed."
    for node in root.iter():
        if "EditText" in node.get("class", "") or "prompt-textarea" in node.get("resource-id", ""):
            txt = node.get("text", "")
            if txt and txt != "Ask anything":
                result = txt
                break
    
    # 5. CLEAR COMPOSER (Mandatory clean state for next turn)
    guaranteed_clear()
    return result

if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit(1)
    
    cmd = sys.argv[1]
    if cmd.lower() == "response":
        resp = extract_response()
        with open("from_chatgpt.txt", "w", encoding="utf-8") as f:
            f.write(resp)
        print("Response saved to from_chatgpt.txt")
    else:
        # Send a prompt
        send_message(cmd + " p.s. sent from Gemini 3 in Android Studio!")
