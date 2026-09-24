"""Offline tests for the additional daily overview board sections."""

import os
import tempfile
import unittest
from unittest.mock import patch

import assemble


class BoardSectionTests(unittest.TestCase):
    def test_overview_contains_weekly_monthly_and_classic_sections(self):
        papers = {
            "weekly": {"arxiv_id": "weekly", "title": "Fresh paper", "abstract": ""},
            "monthly": {"arxiv_id": "monthly", "title": "Rising paper", "abstract": ""},
        }
        daily_rows = {"weekly": {"area": "options"}}
        scores = {"weekly": {"composite_score": 8.5, "composite_reason": "fresh"}}
        monthly = [{"arxiv_id": "monthly", "published": "2026-09-10",
                    "area": "CTA", "composite": 7.4}]
        classic = [("classic", "Classic paper", "2020-01-01", 8.1, 321,
                    4, 0.91, 1.7)]

        def get_papers(_conn, ids):
            return [papers[aid] for aid in ids if aid in papers]

        with tempfile.TemporaryDirectory() as out_dir, \
             patch.object(assemble, "OUTPUT_DIR", out_dir), \
             patch.object(assemble.db, "get_papers", side_effect=get_papers), \
             patch.object(assemble.db, "get_daily_row",
                          side_effect=lambda _conn, aid: daily_rows.get(aid, {})), \
             patch.object(assemble.db, "get_score",
                          side_effect=lambda _conn, aid: scores.get(aid)), \
             patch.object(assemble.wecom, "format_scores", return_value="score"), \
             patch.object(assemble.deep_study, "strip_dollar_in_svg_text",
                          side_effect=lambda text: text), \
             patch.object(assemble.katex_inline, "head_block", return_value=""), \
             patch.object(assemble.scorer, "CSS", ""), \
             patch.object(assemble, "_read", return_value=""):
            path = assemble.assemble_overview(
                object(), ["weekly"], ["weekly"], "2026-09-24",
                monthly_rows=monthly, classic_rows=classic)
            with open(path, "r", encoding="utf-8") as handle:
                html = handle.read()
            self.assertTrue(os.path.exists(path))

        self.assertIn("🔥 本周新品榜", html)
        self.assertIn("📈 本月潜力榜", html)
        self.assertIn("📚 经典沉淀榜", html)
        self.assertIn("monthly", html)
        self.assertIn("classic", html)
        self.assertIn("Classic score v2.1", html)


if __name__ == "__main__":
    unittest.main()
