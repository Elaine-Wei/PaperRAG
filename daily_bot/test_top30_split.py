#!/usr/bin/env python3
"""Offline tests for the score/study handoff.

These tests exercise orchestration only; they never call arXiv, OpenAlex, relay,
COS, or WeCom.
"""

import sys
import types
import unittest
from unittest.mock import patch

import run
import deep_study


class FakeDB:
    def __init__(self):
        self.scores = {"a": {"composite_score": 8.0},
                       "b": {"composite_score": 7.0}}
        self.created = []
        self.recorded = []
        self.finished = []

    def ensure(self, conn):
        return conn

    def get_window_batch(self, conn, days):
        return [{"arxiv_id": "a", "published": "2026-09-20"},
                {"arxiv_id": "b", "published": "2026-09-19"}]

    def get_score(self, conn, aid):
        return self.scores.get(aid)

    def create_top30_run(self, conn, *args):
        self.created.append(args)

    def record_top30_run_papers(self, conn, *args):
        self.recorded.append(args)

    def finish_top30_run(self, conn, *args):
        self.finished.append(args)

    def get_latest_top30_score_run(self, conn, window_days):
        return {"run_id": "night-1", "completed_at": "2026-09-20 06:30",
                "papers": [{"arxiv_id": "a", "composite": 8.0},
                           {"arxiv_id": "b", "composite": 7.0}]}

    def get_stage_status(self, conn, aid):
        return {"studied": False}


class Top30SplitTests(unittest.TestCase):
    def test_score_only_persists_manifest_and_does_not_study(self):
        db = FakeDB()
        with patch.object(run, "db", db), \
             patch.object(run, "_ensure_score", side_effect=lambda c, a: (c, True)), \
             patch.object(run, "_ensure_composite", side_effect=lambda c, a: (c, True)), \
             patch.object(run, "run_study_with_backoff") as study:
            result = run.run_top30(object(), window_days=7, study_top=2,
                                    score_only=True)
        self.assertEqual(result["ranked"], ["a", "b"])
        self.assertEqual(len(db.created), 1)
        self.assertEqual(db.recorded[0][1], ["a", "b"])
        study.assert_not_called()

    def test_study_only_uses_exact_manifest_order(self):
        db = FakeDB()
        fake_assemble = types.SimpleNamespace(
            assemble_overview=lambda *args, **kwargs: "overview.html")
        fake_wecom = types.SimpleNamespace(WEBHOOK=None)
        with patch.object(run, "db", db), \
             patch.object(run, "run_study_with_backoff",
                          return_value={"completed": [], "gave_up": [], "log_path": "x"}) as study, \
             patch.dict(sys.modules, {"assemble": fake_assemble, "wecom": fake_wecom}):
            result = run.run_top30(object(), window_days=7, study_top=1,
                                    study_only=True)
        study.assert_called_once()
        self.assertEqual(study.call_args.args[1], ["a"])
        self.assertEqual(result["ranked"], ["a", "b"])

    def test_rotation_is_fresh_per_invocation_and_checkpoint_names_are_model_scoped(self):
        targets = ["a", "b", "c", "d"]
        models = ["sol", "terra", "luna", "flash"]
        first = {aid: models[i % len(models)] for i, aid in enumerate(targets)}
        second = {aid: models[i % len(models)] for i, aid in enumerate(targets)}
        self.assertEqual(set(first), set(second))
        self.assertTrue(all(first[aid] in models for aid in targets))
        self.assertIsNot(first, second)  # each invocation owns a fresh local mapping
        self.assertNotEqual(deep_study._cp_path("a", "sol"),
                            deep_study._cp_path("a", "terra"))


if __name__ == "__main__":
    unittest.main()
