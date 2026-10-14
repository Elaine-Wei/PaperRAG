"""Offline tests for reader outline skipping and on-demand section coordination."""

import datetime as dt
import os
import threading
import time
import unittest
from unittest.mock import patch

import reader_outline
import reader_service


class FakeStore:
    def __init__(self):
        self.outlines = {"p": ("Paper", {
            "big_sections": [{"index": 0, "title": "Methods", "chunks": [{"index": 0}]}],
            "source_chunks": [{"index": 0, "label": "1", "title": "Methods", "text": "content"}],
        }, "fp", "ds-direct")}
        self.cache = {}
        self.lock = threading.Lock()
        self.generated_count = 0
        self.allow_daily = True

    def get_outline(self, aid):
        return self.outlines.get(aid)

    def get_cache(self, key):
        with self.lock:
            return self.cache.get(key)

    def claim_cache(self, key):
        with self.lock:
            if key in self.cache:
                return False, self.cache[key]
            row = ["queued", None, None, dt.datetime.now(dt.timezone.utc)]
            self.cache[key] = row
            return True, row

    def mark_running(self, key):
        with self.lock:
            self.cache[key][0] = "running"

    def mark_done(self, key, fragment):
        with self.lock:
            self.cache[key] = ["done", fragment, None, dt.datetime.now(dt.timezone.utc)]

    def mark_failed(self, key, error):
        with self.lock:
            self.cache[key] = ["failed", None, str(error), dt.datetime.now(dt.timezone.utc)]

    def consume_daily_cap(self):
        if not self.allow_daily:
            return False
        self.generated_count += 1
        return True


class ReaderTests(unittest.TestCase):
    def test_first_model_failure_retries_whole_section_with_second_model(self):
        store = FakeStore()
        calls = []

        def pass2(chunk, model):
            calls.append(model)
            if model == "gpt-5.6-sol":
                raise RuntimeError("relay unavailable")
            return f"<p>{model}</p>", {}

        coordinator = reader_service.SectionCoordinator(
            store, daily_cap=False, models=("gpt-5.6-sol", "ds-direct"))
        with patch.object(reader_service.deep_study, "pass2_explain", side_effect=pass2):
            coordinator.request("p", 0, "1.2.3.4")
            time.sleep(0.03)

        row = next(iter(store.cache.values()))
        self.assertEqual(row[0], "done")
        self.assertEqual(row[1], "<p>ds-direct</p>")
        self.assertEqual(calls, ["gpt-5.6-sol", "ds-direct"])

    def test_all_models_failure_marks_row_failed(self):
        store = FakeStore()
        calls = []

        def pass2(chunk, model):
            calls.append(model)
            raise RuntimeError("unavailable")

        coordinator = reader_service.SectionCoordinator(
            store, daily_cap=False, models=("gpt-5.6-sol", "ds-direct"))
        with patch.object(reader_service.deep_study, "pass2_explain", side_effect=pass2):
            coordinator.request("p", 0, "1.2.3.4")
            time.sleep(0.03)

        row = next(iter(store.cache.values()))
        self.assertEqual(row[0], "failed")
        self.assertEqual(calls, ["gpt-5.6-sol", "ds-direct"])

    def test_default_reader_models_is_ds_direct_only(self):
        with patch.dict(os.environ, {}, clear=True):
            coordinator = reader_service.SectionCoordinator(FakeStore(), daily_cap=False)
        self.assertEqual(coordinator.models, ("ds-direct",))

    def test_cache_hit_makes_no_generator_call(self):
        store = FakeStore()
        coordinator = reader_service.SectionCoordinator(store, generator=lambda *_: "bad")
        status, _ = coordinator.request("p", 0, "1.2.3.4")
        time.sleep(0.02)
        calls = []
        coordinator.generator = lambda *_: calls.append(1) or "new"
        status, payload = coordinator.request("p", 0, "1.2.3.4")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(calls, [])

    def test_concurrent_identical_requests_generate_once(self):
        store = FakeStore()
        calls = []
        def generator(*_):
            calls.append(1)
            time.sleep(0.05)
            return "<p>done</p>"
        coordinator = reader_service.SectionCoordinator(store, generator=generator)
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            coordinator.request("p", 0, "1.2.3.4"))) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        time.sleep(0.08)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(code in (202, 200) for code, _ in results))

    def test_daily_cap_returns_429(self):
        store = FakeStore()
        store.allow_daily = False
        coordinator = reader_service.SectionCoordinator(store)
        status, payload = coordinator.request("p", 0, "1.2.3.4")
        self.assertEqual(status, 429)
        self.assertIn("cap", payload["error"])

    def test_invalid_section_is_rejected(self):
        coordinator = reader_service.SectionCoordinator(FakeStore())
        status, payload = coordinator.request("p", 99, "1.2.3.4")
        self.assertEqual(status, 400)
        self.assertIn("invalid", payload["error"])

    def test_outline_batch_skips_matching_fingerprint(self):
        class FakeConn:
            def cursor(self):
                class Cursor:
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def execute(self, *_args): pass
                return Cursor()
            def commit(self): pass
            def close(self): pass
        with patch.object(reader_outline, "_connection", return_value=FakeConn()), \
             patch.object(reader_outline.db, "get_window_batch",
                          return_value=[{"arxiv_id": "p", "composite": 8.0}]), \
             patch.object(reader_outline, "_existing_outline",
                          return_value={"source_fingerprint": "fp", "model": "ds-direct"}), \
             patch.object(reader_outline, "_checkpoint_fingerprint", return_value="fp"), \
             patch.object(reader_outline.deep_study, "generate_study") as generate:
            result = reader_outline.generate_outlines(top=1, model="ds-direct")
        self.assertEqual(result["skipped"], ["p"])
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
