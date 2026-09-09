#!/usr/bin/env python3

import json
import io
import shutil
import unittest
from datetime import datetime
from pathlib import Path

from relay import (
    RecoveryController,
    RecoveryTier,
    RelayError,
    RelayState,
    ResponseNode,
    ResponseSnapshot,
    SelectorConfig,
    _element_exists,
    _is_likely_chat_input,
    dismiss_overlays,
    find_first_match,
    find_best_match,
    set_prompt_text,
    submit_message,
    run_cdp_transaction,
    _FakeCdp,
    SessionLogger,
    self_test,
    CERTIFICATION_PHASES,
    CERTIFICATION_SCENARIOS,
    run_certification,
    verify_response_stability,
    wait_for_send_control,
    _MESSAGES_JS,
)


class DummyElement:
    def __init__(self, *, text="", resource="", exists=True):
        self.text = text
        self.exists = exists
        self.info = {"resourceName": resource} if resource else {}
        self.clicks = 0

    def click(self):
        self.clicks += 1

    def clear_text(self):
        self.text = ""

    def set_text(self, value):
        self.text = value


class DummyDevice:
    def __init__(self):
        self.input = DummyElement(text="", resource="chat:prompt")
        self.send = DummyElement(text="Send", resource="chat:send")
        self.response = DummyElement(text="old reply", resource="chat:message")
        self.sent = False
        self.enter_presses = 0

    def __call__(self, **kwargs):
        if "textContains" in kwargs and kwargs["textContains"] == "Ask anything":
            return self.input
        if "resourceIdMatches" in kwargs:
            value = kwargs["resourceIdMatches"]
            if "prompt" in value:
                return self.input
            if any(word in value for word in ("message", "response", "assistant", "bubble")):
                return self.response
            if any(word in value for word in ("send", "submit", "composer_send")):
                return self.send if not self.sent else DummyElement(exists=False)
        if "textContains" in kwargs and kwargs["textContains"] == "Send":
            return self.send if not self.sent else DummyElement(exists=False)
        return DummyElement(exists=False)

    def press(self, key):
        self.enter_presses += 1


class TransactionDevice(DummyDevice):
    def __init__(self):
        super().__init__()
        self.input.info = {"resourceName": "chat:prompt"}

    def __call__(self, **kwargs):
        element = super().__call__(**kwargs)
        if element is self.send:
            element.click = self._send
        return element

    def _send(self):
        self.send.clicks += 1
        self.sent = True
        self.input.text = ""
        self.response.text = "new reply"


class AmbiguousDevice:
    def __init__(self):
        self.left = DummyElement(resource="chat:left")
        self.right = DummyElement(resource="chat:right")

    def __call__(self, **kwargs):
        return self.left if kwargs.get("text") == "left" else self.right


class IconOnlyComposerDevice(DummyDevice):
    def __init__(self):
        super().__init__()
        self.input.info.update({"bounds": "[84,1919][998,2043]"})
        self.icon_button = DummyElement(resource="radix-send")
        self.icon_button.info.update(
            {
                "bounds": "[908,2012][1015,2116]",
                "className": "android.widget.Button",
                "clickable": True,
                "enabled": True,
            }
        )

    def __call__(self, **kwargs):
        if kwargs.get("className") == "android.widget.Button":
            return [self.icon_button]
        return DummyElement(exists=False)


class FlakyInput(DummyElement):
    def __init__(self):
        super().__init__(resource="chat:prompt")
        self.attempts = 0

    def set_text(self, value):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("uiautomator timeout -32002")
        super().set_text(value)


class ShellDevice:
    def __init__(self):
        self.commands = []

    def shell(self, command):
        self.commands.append(command)


class RelaySmokeTests(unittest.TestCase):
    def cfg(self):
        return SelectorConfig(timeout_s=0.2, reconciliation_timeout_s=0.05, poll_interval_s=0.01)

    def test_default_selectors_are_present(self):
        cfg = SelectorConfig()
        self.assertTrue(cfg.input_selectors)
        self.assertTrue(cfg.send_selectors)
        self.assertTrue(cfg.response_selectors)

    def test_selector_finder_uses_available_fields(self):
        device = DummyDevice()
        found = find_first_match(device, [{"textContains": "Ask anything"}], timeout_s=1.0)
        self.assertIsNotNone(found)
        self.assertEqual(found.text, "")

    def test_element_exists_supports_property_and_callable_forms(self):
        self.assertTrue(_element_exists(DummyElement()))
        self.assertTrue(_element_exists(type("CallableElement", (), {"exists": lambda self: True})()))

    def test_selector_config_uses_supported_description_matcher(self):
        cfg = SelectorConfig()
        self.assertTrue(any("descriptionMatches" in selector for selector in cfg.send_selectors))

    def test_input_selectors_do_not_use_generic_edit_text_fallback(self):
        cfg = SelectorConfig()
        self.assertNotIn({"className": "android.widget.EditText"}, cfg.input_selectors)

    def test_address_bar_is_rejected(self):
        address_bar = type(
            "AddressBar",
            (),
            {"info": {"resourceName": "com.android.chrome:id/url_bar"}},
        )()
        self.assertFalse(_is_likely_chat_input(address_bar))

    def test_icon_only_composer_button_is_found_by_prompt_geometry(self):
        device = IconOnlyComposerDevice()
        diagnostics = type("Diagnostics", (), {"observations": []})()
        button = wait_for_send_control(device, self.cfg(), device.input, diagnostics)
        self.assertIs(button, device.icon_button)
        self.assertIn("geometry-validated", diagnostics.observations[0])

    def test_overlay_dismissal_never_opens_chrome_menu(self):
        device = ShellDevice()
        dismiss_overlays(device)
        self.assertNotIn("KEYCODE_MENU", " ".join(device.commands))

    def test_prompt_insertion_retries_after_uiautomator_timeout(self):
        device = DummyDevice()
        flaky = FlakyInput()
        device.input = flaky
        diagnostics = type("Diagnostics", (), {"observations": [], "last_error": None})()
        result = set_prompt_text(device, flaky, "hello", self.cfg(), diagnostics)
        self.assertIs(result, flaky)
        self.assertEqual(flaky.text, "hello")
        self.assertEqual(flaky.attempts, 2)

    def test_ambiguous_candidates_are_not_guessed(self):
        with self.assertRaises(RelayError):
            find_best_match(
                AmbiguousDevice(),
                [{"text": "left"}, {"text": "right"}],
                timeout_s=0.01,
            )

    def test_one_shot_submission_reconciles_and_never_presses_enter(self):
        device = TransactionDevice()
        transaction = submit_message(device, "hello", self.cfg())
        self.assertEqual(transaction.state, RelayState.VERIFIED)
        self.assertTrue(transaction.reconciled)
        self.assertEqual(device.send.clicks, 1)
        self.assertEqual(device.enter_presses, 0)
        self.assertEqual(len(transaction.prompt_hash), 64)

    def test_failed_click_is_unknown_and_not_retried(self):
        device = DummyDevice()
        device.send.click = lambda: (_ for _ in ()).throw(RuntimeError("blocked"))
        with self.assertRaises(RelayError) as error:
            submit_message(device, "hello", self.cfg())
        self.assertIn("guarded UI action", str(error.exception))
        self.assertEqual(device.enter_presses, 0)

    def test_uncertain_click_stops_without_a_second_send(self):
        device = DummyDevice()
        with self.assertRaises(RelayError) as error:
            submit_message(device, "hello", self.cfg())
        self.assertIn("state=unknown", str(error.exception))
        self.assertEqual(device.send.clicks, 1)
        self.assertEqual(device.enter_presses, 0)

    def test_three_snapshots_require_correlated_stability(self):
        stable = ResponseSnapshot((ResponseNode("message", "ok"),), "ok", False)
        changing_identity = ResponseSnapshot((ResponseNode("other", "ok"),), "ok", False)
        self.assertEqual(verify_response_stability([stable, stable, stable]), "ok")
        self.assertIsNone(verify_response_stability([stable, stable, changing_identity]))

    def test_message_capture_truncates_old_history_before_cdp_serialization(self):
        self.assertIn("allNodes.slice(-3)", _MESSAGES_JS)

    def test_recovery_tiers_are_monotonic_and_verified(self):
        recovery = RecoveryController()
        recovery.advance(RecoveryTier.DISMISS_OVERLAYS, verified=True)
        with self.assertRaises(RelayError):
            recovery.advance(RecoveryTier.NONE, verified=True)
        with self.assertRaises(RelayError):
            recovery.advance(RecoveryTier.REFRESH)

    def test_fake_cdp_transaction_submits_exactly_once(self):
        fake = _FakeCdp()
        response, transaction = run_cdp_transaction(
            fake, "offline prompt", timeout_s=0.2, poll_interval_s=0
        )
        self.assertEqual(response, "offline response")
        self.assertEqual(fake.sent, 1)
        self.assertTrue(transaction.action_attempted)

    def test_offline_self_test(self):
        self_test()

    def test_self_test_log_has_iso_timestamp_and_ids(self):
        log_dir = Path("relay_log_test_artifacts")
        shutil.rmtree(log_dir, ignore_errors=True)
        try:
            logger = SessionLogger("self-test-session", log_dir, stream=io.StringIO())
            self_test(logger)
            logger.close()
            path = log_dir / "session_self-test-session.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(records), 5)
            for record in records:
                self.assertEqual(record["session_id"], "self-test-session")
                datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                self.assertTrue(record["phase"])
                self.assertTrue(record["event"])
                self.assertTrue(record["request_id"])
            self.assertTrue(any(record["turn_id"] for record in records))
            self.assertEqual(records[-1]["outcome"], "success")
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_repeatability_certification_covers_all_stages_and_edge_cases(self):
        log_dir = Path("relay_cert_test_artifacts")
        shutil.rmtree(log_dir, ignore_errors=True)
        try:
            logger = SessionLogger("certification-test", log_dir, stream=io.StringIO())
            summary = run_certification(10, logger=logger)
            logger.close()
            self.assertEqual(summary["certification"], "PASS")
            self.assertEqual(summary["duplicate_submits"], 0)
            self.assertEqual(summary["false_positives"], 0)
            self.assertEqual(
                {item["scenario"] for item in summary["scenarios"]},
                set(CERTIFICATION_SCENARIOS),
            )
            records = [
                json.loads(line)
                for line in (log_dir / "session_certification-test.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            phase_records = [record for record in records if record["phase"] in CERTIFICATION_PHASES]
            for phase in CERTIFICATION_PHASES:
                self.assertEqual(
                    sum(record["phase"] == phase for record in phase_records),
                    10,
                )
            self.assertEqual(
                sum(record["event"] == "iteration-outcome" for record in records),
                10,
            )
            self.assertTrue(all(record["session_id"] == "certification-test" for record in records))
            self.assertTrue(all(record["request_id"] for record in phase_records))
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
