# Stability & Simplified Chat Path Implementation Plan

Establish a predictable single-instance runtime and a direct, reliable chat path for MAGY.

## Proposed Changes

### [Aeowun Infrastructure]

#### [MODIFY] [code.bat](file:///C:/Dev/IDE/vscode/scripts/code.bat)
- Add a guard using `tasklist` and `findstr` to check if the Aeowun executable (e.g., `Aeowun.exe`) is already running.
- If an instance is detected, print "Aeowun is already running." and exit without launching a new instance.

### [MAGY Backend]

#### [MODIFY] [main.rs](file:///C:/Dev/IDE/Magy/magy-app/src/main.rs)
- Implement `is_port_open(port: u16) -> bool` using `std::net::TcpStream`.
- Before spawning Chrome with the `--remote-debugging-port=9222` flag, check if port 9222 is already open.
- If port 9222 is open, log "[MAGY] CDP port 9222 is active. Reusing existing Chrome instance." and skip spawning.
- Ensure the Axum server binding to port 3000 handles "Address already in use" errors by exiting gracefully with a clear message.

### [MAGY Frontend]

#### [MODIFY] [app.js](file:///C:/Dev/IDE/Magy/magy-ui/app.js)
- Simplify `handleChatSubmit` to act as a pure bridge: take user input, send to `/api/chat`, and wait for the relay response via SSE.
- Remove any frontend-side history management or reasoning logic that competes with the relay.
- Robustly implement the 4-second "M" loader:
    - Ensure it is shown when a request is pending.
    - Guarantee it is dismissed when a response arrives or after the 4-second timeout.
    - Prevent overlapping requests from creating stale UI states.

## Verification Plan

### Automated Verification
- **Rust Compilation**: Run `cargo build` in `magy-app` to ensure no syntax errors in the new port-check logic.
- **Frontend Transpile**: Run `npm run transpile-client` to verify `app.js` is correctly synced.

### Manual Verification
1. **Single Instance Aeowun**:
   - Launch Aeowun via `code.bat`.
   - Run `code.bat` again; verify it exits with "Aeowun is already running."
2. **Single Instance Chrome**:
   - Start Aeowun; verify one Chrome instance opens.
   - Restart the MAGY backend manually while Chrome is open; verify NO second Chrome window is spawned.
3. **Single Instance MAGY Backend**:
   - Try to run `magy-app.exe` while Aeowun is already running; verify it fails gracefully (Address in use).
4. **Chat flow**:
   - Send short and long messages; verify they all appear in the UI and are sourced from the relay.
   - Verify the "M" loader behavior matches the 4-second requirement exactly.
