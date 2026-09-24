"""Offline regression tests for OpenAlex fallback date-window semantics."""

import datetime as dt
import unittest
from unittest.mock import patch

import openalex_fetch
import run


class OpenAlexFallbackWindowTests(unittest.TestCase):
    def test_daily_default_remains_seven_days(self):
        with patch.object(openalex_fetch, "translate_query", return_value="option"), \
             patch.object(openalex_fetch, "_request", return_value=[]) as request:
            openalex_fetch.fetch_fallback("option pricing")

        start, end = request.call_args.args[1:3]
        self.assertIsInstance(start, dt.date)
        self.assertIsInstance(end, dt.date)
        self.assertEqual((end - start).days, 6)

    def test_prefixed_daily_query_translation_is_unchanged(self):
        self.assertEqual(
            openalex_fetch.translate_query('ti:"option pricing" abs:hedging'),
            '"option pricing" OR hedging',
        )

    def test_bare_quoted_title_becomes_search_term(self):
        query = '"Retail Trading in Options and the Rise of the Big Three Wholesalers" AuthorSurname'
        self.assertEqual(
            openalex_fetch.translate_query(query),
            '"Retail Trading in Options and the Rise of the Big Three Wholesalers AuthorSurname"',
        )
        with patch.object(openalex_fetch, "_request", return_value=[]) as request:
            openalex_fetch.fetch_fallback(query, date_window_days=None)
        self.assertEqual(request.call_args.args[0],
                         '"Retail Trading in Options and the Rise of the Big Three Wholesalers AuthorSurname"')

    def test_run_daily_ingest_keeps_original_call_signature(self):
        error = RuntimeError("simulated arXiv failure")
        counts = {"results": 0, "accepted": 0,
                  "discarded_no_arxiv_id": 0, "duplicate_arxiv_ids": 0}
        with patch.object(run, "fetch_arxiv", return_value=([], error)), \
             patch.object(run.openalex_fetch, "fetch_fallback",
                          return_value=([], counts)) as fallback, \
             patch.object(run.time, "sleep"):
            run.fetch_recent_papers(["all:option pricing"], per_query=1)

        fallback.assert_called_once_with("all:option pricing")

    def test_none_omits_publication_date_filter(self):
        with patch.object(openalex_fetch, "translate_query", return_value="option"), \
             patch.object(openalex_fetch, "_request", return_value=[]) as request:
            openalex_fetch.fetch_fallback("option pricing", date_window_days=None)

        self.assertEqual(request.call_args.args[1:], (None, None))

        with patch.object(openalex_fetch, "_request_json", return_value={}) as request_json:
            openalex_fetch._request("option", None, None)
        params = request_json.call_args.args[0]
        self.assertEqual(params["filter"], "indexed_in:arxiv")
        self.assertNotIn("from_publication_date", params["filter"])
        self.assertNotIn("to_publication_date", params["filter"])


if __name__ == "__main__":
    unittest.main()
