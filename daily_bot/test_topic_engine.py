"""Offline tests for the review-gated topic engine merge/validation layer."""

import copy
import json
import sys
import unittest
from pathlib import Path

import topic_engine


class TopicEngineTests(unittest.TestCase):
    def test_existing_alias_and_idempotent_seed_merge(self):
        self.assertEqual(topic_engine._topic_key("期权"), "options")
        existing = {"topic": "options", "verified_anchors": [
            {"arxiv_id": "1901.09647", "title": "Deep Learning Volatility",
             "category": "foundational"}], "keyword_nets": []}
        review = {"verified_anchors": [
            {"arxiv_id": "1901.09647", "title": "duplicate",
             "category": "foundational"},
            {"arxiv_id": "1802.03042", "title": "Deep Hedging",
             "category": "foundational"}], "keyword_nets": []}
        merged, added = topic_engine._merge_artifacts("options", "", existing, review)
        self.assertEqual([item["arxiv_id"] for item in added], ["1802.03042"])
        self.assertEqual(len(merged["verified_anchors"]), 2)

    def test_real_options_artifact_validates_through_loader(self):
        config = json.loads(topic_engine.TOPICS_CONFIG.read_text(encoding="utf-8"))
        artifact_path = topic_engine._artifact_path("options")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        config["topics"]["options"]["_artifact"] = copy.deepcopy(artifact)
        topic_engine._validate_candidate_config(config, "options", artifact_path)

    def test_new_topic_config_has_loader_required_fields(self):
        item = topic_engine._new_topic_config(
            "esg", "Environmental, social, and governance research",
            {"keyword_nets": [{"queries": ["ESG", "sustainable investing"]}]})
        self.assertEqual(item["bootstrap_json"], "manual_topic_bootstrap/esg.json")
        self.assertTrue(item["prefilter_terms"])
        self.assertEqual(set(item["weights"]), set(topic_engine.topic_config.WEIGHT_KEYS))


if __name__ == "__main__":
    unittest.main()
