# Finalize Modular Architecture and Fix Targeting

The goal is to finalize the refactoring of `relay.py` and fix the composer hijacking bug identified in Turn 5 of `stress_test.py`.

## Proposed Changes

### [CDP Client]

#### [MODIFY] [cdp_client.py](file:///C:/Dev/IDE/devTool_xO0/cdp/cdp_client.py)
- Consolidate composer discovery logic across `_COMPOSER_JS`, `_SET_COMPOSER_JS`, and `_SUBMIT_JS`.
- Ensure strict targeting of "Ask anything" and exclusion of history areas (nav, header, article).
- Fix any remaining typos in JS strings.
- Verify "Copy" beacon logic in `_MESSAGES_JS` for response detection.

## Verification Plan

### Automated Tests
- Run `python stress_test.py` and verify it passes Turn 5 and subsequent turns.
- Monitor `RSS` memory usage via the stress test output.

### Manual Verification
- Verify that the relay correctly identifies the main composer in a live session with long history.
