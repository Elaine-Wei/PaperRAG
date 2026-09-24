#!/usr/bin/env python3
"""Offline tests for topic-bootstrap nomination fallback."""

import unittest
from unittest.mock import patch

import topic_bootstrap as tb


class TopicBootstrapNominationTests(unittest.TestCase):
    def _args(self):
        return ("options", "option pricing and hedging", "gpt-5.6-sol", 2, 2, 2)

    def test_falls_through_models_after_timeout(self):
        payload = '{"foundational": [], "recent_influential": [], "keyword_nets": []}'
        calls = []

        def fake_chat(system, user, temperature, model, max_tokens):
            calls.append(model)
            if model == "gpt-5.6-sol":
                raise TimeoutError("simulated relay timeout")
            return payload, {"model": model}

        with patch.object(tb.relay, "relay_chat", side_effect=fake_chat):
            result = tb.nominate(*self._args())

        self.assertEqual(calls[:2], ["gpt-5.6-sol", "gpt-5.6-terra"])
        self.assertEqual(result["_model"], "gpt-5.6-terra")
        self.assertEqual(result["_nomination_attempts"][0]["status"], "error")
        self.assertEqual(result["_nomination_attempts"][1]["status"], "success")

    def test_all_models_fail_without_unbounded_retry(self):
        with patch.object(tb.relay, "relay_chat",
                          side_effect=TimeoutError("simulated relay timeout")) as chat:
            result = tb.nominate(*self._args())

        self.assertEqual(chat.call_count, 4)
        self.assertEqual(len(result["_nomination_attempts"]), 4)
        self.assertEqual(result["foundational"], [])
        self.assertIn("timeout", result["_error"])


if __name__ == "__main__":
    unittest.main()
