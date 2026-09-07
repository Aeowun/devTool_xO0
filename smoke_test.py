#!/usr/bin/env python3

import unittest

from relay import SelectorConfig, find_first_match


class DummyElement:
    def __init__(self, *, text="", exists=True):
        self.text = text
        self.exists = exists

    def click(self):
        self.clicked = True


class DummyDevice:
    def __init__(self):
        self.available = {
            "prompt": DummyElement(text="Ask anything"),
            "send": DummyElement(text="Send"),
            "response": DummyElement(text="This is the assistant reply"),
        }

    def __call__(self, **kwargs):
        for name, value in kwargs.items():
            if name == "resourceIdMatches" and "prompt" in value:
                return self.available["prompt"]
            if name == "textContains" and value == "Ask anything":
                return self.available["prompt"]
            if name == "textContains" and value == "Send":
                return self.available["send"]
            if name == "resourceIdMatches" and "message" in value:
                return self.available["response"]
        return DummyElement(exists=False)


class RelaySmokeTests(unittest.TestCase):
    def test_default_selectors_are_present(self):
        cfg = SelectorConfig()
        self.assertTrue(cfg.input_selectors)
        self.assertTrue(cfg.send_selectors)
        self.assertTrue(cfg.response_selectors)

    def test_selector_finder_uses_available_fields(self):
        device = DummyDevice()
        found = find_first_match(device, [{"textContains": "Ask anything"}], timeout_s=1.0)
        self.assertIsNotNone(found)
        self.assertEqual(found.text, "Ask anything")


if __name__ == "__main__":
    unittest.main()
