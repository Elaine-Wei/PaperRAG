"""Offline tests for run_boards arXiv/OpenAlex retrieval fallback."""

import unittest
from unittest.mock import patch

import run_boards


class RunBoardsFallbackTests(unittest.TestCase):
    def test_fetch_ids_falls_back_and_normalizes_metadata(self):
        work = {
            "display_name": "Fallback paper",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "A. Author"}}],
        }
        with patch.object(run_boards, "_get", return_value=None), \
             patch.object(run_boards.openalex_fetch,
                          "fetch_works_by_arxiv_ids",
                          return_value={"1234.5678": work}) as lookup:
            result = run_boards.fetch_ids(["1234.5678"])

        lookup.assert_called_once_with(["1234.5678"])
        self.assertEqual(result["1234.5678"]["title"], "Fallback paper")
        self.assertEqual(result["1234.5678"]["authors"], ["A. Author"])
        self.assertEqual(result["1234.5678"]["published"], "2024-01-01")
        self.assertEqual(result["1234.5678"]["fetch_source"], "openalex-fallback")

    def test_fetch_search_falls_back_and_preserves_source_tag(self):
        papers = [{"arxiv_id": "2345.6789", "title": "Search fallback",
                   "fetch_source": "openalex-fallback"}]
        counts = {"results": 1, "accepted": 1,
                  "discarded_no_arxiv_id": 0, "duplicate_arxiv_ids": 0}
        with patch.object(run_boards, "_get", return_value=None), \
             patch.object(run_boards.time, "sleep"), \
             patch.object(run_boards.openalex_fetch, "fetch_fallback",
                          return_value=(papers, counts)) as fallback:
            result = run_boards.fetch_search("option pricing", 5)

        fallback.assert_called_once_with("option pricing", date_window_days=None)
        self.assertEqual(result[0]["arxiv_id"], "2345.6789")
        self.assertEqual(result[0]["fetch_source"], "openalex-fallback")

    def test_successful_arxiv_response_does_not_call_fallback(self):
        direct = [{"arxiv_id": "3456.7890", "title": "Direct paper"}]
        with patch.object(run_boards, "_get", return_value=b"<feed/>"), \
             patch.object(run_boards.run, "parse_arxiv_xml", return_value=direct), \
             patch.object(run_boards.openalex_fetch,
                          "fetch_works_by_arxiv_ids") as id_fallback:
            result = run_boards.fetch_ids(["3456.7890"])

        self.assertEqual(result, {"3456.7890": direct[0]})
        id_fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
