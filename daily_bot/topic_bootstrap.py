#!/usr/bin/env python3
"""Generate a review-only, verified seed proposal for ``run_topic.py``.

This module deliberately has no database writes and no code path that edits
run_topic.py.  The LLM nominates; arXiv verifies; a human promotes the output.
"""

import argparse
import datetime as dt
import difflib
import html
import json
import re
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import relay  # noqa: E402


def _load_arxiv_helpers():
    """Load the existing helpers without requiring the optional DB driver.

    run_boards imports the daily DB module even though fetch_ids/fetch_search
    do not use it.  Keep this review-only tool runnable in a minimal checkout;
    when psycopg2 is unavailable, run.py only needs a placeholder at import
    time and no database function is ever called here.
    """
    try:
        import run_boards as helpers  # noqa: PLC0415
        return helpers
    except ModuleNotFoundError as exc:
        if exc.name != "psycopg2":
            raise
        sys.modules.setdefault("db", types.ModuleType("db"))
        import run_boards as helpers  # noqa: PLC0415
        return helpers


run_boards = _load_arxiv_helpers()


def _install_review_only_arxiv_get():
    """Keep a rate-limited review run from retrying a whole nomination set.

    The existing helper remains the public fetch_ids/fetch_search API used
    below.  Only its transport callback is replaced in this additive tool so
    a 429 becomes an explicit unverified/preview failure in the artifact.
    """
    def get_once(url, tries=3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PaperRAG-topic-bootstrap/1"})
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            print(f"    [arxiv] review fetch failed HTTP {exc.code}; recording as unavailable", flush=True)
            return None
        except Exception as exc:
            print(f"    [arxiv] review fetch failed ({str(exc)[:80]}); recording as unavailable", flush=True)
            return None
    run_boards._get = get_once


_install_review_only_arxiv_get()


NOMINATION_SYSTEM = r'''You are a research-literature nomination assistant.

Given a topic name and description, nominate papers that would make a strong
bootstrap reading list for an arXiv-based research topic board.

Produce two categories:
1. foundational: classic, early, or field-defining work that introduced a
   core model, theorem, algorithm, dataset, or research direction.
2. recent_influential: recent papers that appear influential, widely
   discussed, highly cited, or important to the current direction.

You are allowed to be uncertain. Never invent an arXiv ID. If you do not know
an exact arXiv ID, leave it null and provide only title, authors, and year.
Also propose keyword-search queries covering distinct sub-flavors.

Return JSON only with this schema:
{"foundational":[{"title":"...","authors":["..."],"approx_year":2020,
"arxiv_id":null,"why":"...","confidence":"high|medium|low"}],
"recent_influential":[{"title":"...","authors":["..."],"approx_year":2025,
"arxiv_id":null,"why":"...","confidence":"high|medium|low"}],
"keyword_nets":[{"label":"...","queries":["..."],"coverage":"..."}]}

Do not claim that a paper exists merely because it sounds plausible. Do not
use citation counts unless they are supplied by a source in the prompt.'''


def _norm_text(value):
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


def _title_score(a, b):
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    token = len(ta & tb) / max(1, len(ta))
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    return round(max(token, seq), 4)


def _author_tokens(names):
    out = set()
    for name in names or []:
        bits = re.findall(r"[a-zA-Z]+", str(name).lower())
        if bits:
            out.add(bits[-1])
    return out


def _author_score(nominated, actual):
    a, b = _author_tokens(nominated), _author_tokens(actual)
    return round(len(a & b) / max(1, len(a)), 4) if a else None


def _year_score(year, published):
    if not year or not published:
        return None
    try:
        delta = abs(int(year) - int(str(published)[:4]))
    except (TypeError, ValueError):
        return None
    return max(0.0, round(1 - delta / 2, 4))


def _candidate_score(candidate, meta):
    title = _title_score(candidate.get("title"), meta.get("title"))
    author = _author_score(candidate.get("authors"), meta.get("authors"))
    year = _year_score(candidate.get("approx_year"), meta.get("published"))
    # Title is the identity anchor. Authors/year improve confidence but are
    # allowed to be absent because nominations are often incomplete.
    strict = title >= 0.80 and (author is None or author > 0) and (year is None or year > 0)
    score = round(0.70 * title + 0.20 * (author if author is not None else 1) +
                  0.10 * (year if year is not None else 1), 4)
    return {"title": title, "author": author, "year": year,
            "combined": score, "strict_match": strict}


def _openalex_enrichment(arxiv_id, title):
    """Optional post-verification enrichment; never participates in identity matching."""
    try:
        crawler = HERE.parent / "crawler"
        if str(crawler) not in sys.path:
            sys.path.insert(0, str(crawler))
        from enrichers import enrich_openalex  # noqa: PLC0415
        return enrich_openalex(arxiv_id, title) or None
    except Exception as exc:
        return {"unavailable": str(exc)[:160]}


def nominate(topic, description, model, foundational_n, recent_n, queries_per_flavor):
    user = (f"Topic name: {topic}\nTopic description: {description}\n\n"
            f"Nominate at most {foundational_n} foundational and {recent_n} recent_influential "
            f"papers. Propose at most {queries_per_flavor} queries per keyword-net flavor.")
    last_error = None
    # One nomination call per CLI run: relay itself already rotates keys and
    # retries transient failures.  A second full nomination would duplicate
    # a potentially expensive LLM request and delay the review artifact.
    for attempt in range(1):
        try:
            content, usage = relay.relay_chat(NOMINATION_SYSTEM, user, temperature=0,
                                               model=model, max_tokens=5000)
            obj = relay.extract_json(content)
            if isinstance(obj, dict):
                obj.setdefault("foundational", [])
                obj.setdefault("recent_influential", [])
                obj.setdefault("keyword_nets", [])
                obj["_usage"] = usage
                obj["_attempt"] = attempt + 1
                return obj
            last_error = "nomination response was not valid JSON"
        except Exception as exc:  # preserve a review artifact even on model failure
            last_error = str(exc)
    return {"foundational": [], "recent_influential": [], "keyword_nets": [],
            "_error": last_error}


def verify_candidate(candidate, category):
    title = str(candidate.get("title") or "").strip()
    if not title:
        return "rejected_or_ambiguous", {"category": category, "candidate": candidate,
                                         "reason": "missing title"}
    attempts = []
    matches = []
    aid = str(candidate.get("arxiv_id") or "").strip()
    if aid:
        attempts.append({"method": "arxiv_id", "query": aid})
        matches = list((run_boards.fetch_ids([aid]) or {}).values())
    if not matches:
        queries = [f'"{title}"']
        authors = candidate.get("authors") or []
        if authors:
            queries.append(f'"{title}" {str(authors[0]).split()[-1]}')
        for query in queries:
            attempts.append({"method": "arxiv_title_search", "query": query})
            found = run_boards.fetch_search(query, 5)
            seen = {m.get("arxiv_id") for m in matches}
            matches.extend(m for m in found if m.get("arxiv_id") not in seen)
    scored = []
    for meta in matches[:10]:
        scores = _candidate_score(candidate, meta)
        scored.append({"arxiv_id": meta.get("arxiv_id"), "title": meta.get("title"),
                       "published": meta.get("published"), "scores": scores})
    strict = [x for x in scored if x["scores"]["strict_match"]]
    strict.sort(key=lambda x: x["scores"]["combined"], reverse=True)
    evidence = {"category": category, "nominated": candidate, "search_attempts": attempts,
                "top_matches": scored[:5]}
    if len(strict) == 1 or (strict and strict[0]["scores"]["combined"] -
                             strict[1]["scores"]["combined"] >= 0.08):
        best = strict[0]
        meta = (run_boards.fetch_ids([best["arxiv_id"]]) or {}).get(best["arxiv_id"], {})
        evidence.update({"arxiv_id": best["arxiv_id"], "title": meta.get("title"),
                         "authors": meta.get("authors") or [],
                         "published": meta.get("published"), "verification_method":
                         "arXiv ID lookup" if aid else "arXiv title search",
                         "match_scores": best["scores"],
                         "arxiv_url": f"https://arxiv.org/abs/{best['arxiv_id']}"})
        evidence["openalex_enrichment"] = _openalex_enrichment(best["arxiv_id"], meta.get("title"))
        return "verified_anchors", evidence
    if not scored:
        evidence["reason"] = "no arXiv result matched the nomination"
        return "proposed_but_unverified", evidence
    evidence["reason"] = "arXiv results exist, but identity match is weak or ambiguous"
    return "rejected_or_ambiguous", evidence


def preview_keywords(nets, topic, queries_per_flavor):
    out = []
    for net in nets or []:
        queries = [str(q).strip() for q in (net.get("queries") or []) if str(q).strip()]
        item = {"label": net.get("label") or "unnamed", "coverage": net.get("coverage") or "",
                "queries": []}
        for query in queries[:queries_per_flavor]:
            results = run_boards.fetch_search(query, 5)
            topic_terms = set(_norm_text(topic).split())
            topical = [m for m in results if topic_terms & set(_norm_text(
                f"{m.get('title','')} {m.get('abstract','')}").split())]
            flag = "zero_results" if not results else ("possibly_irrelevant" if not topical else None)
            item["queries"].append({"query": query, "result_count": len(results),
                "representative_titles": [m.get("title") for m in results[:3]],
                "topical_signal_count": len(topical), "flag": flag})
        out.append(item)
    return out


def build_report(topic, description, nomination, verified, unverified, rejected, previews):
    seeds = {x["arxiv_id"]: x["title"] for x in verified if x["category"] == "foundational"}
    anchors = {x["arxiv_id"]: x["title"] for x in verified if x["category"] == "recent_influential"}
    queries = [q["query"] for net in previews for q in net["queries"]]
    return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "topic": topic,
            "description": description, "verified_anchors": verified,
            "proposed_but_unverified": unverified, "rejected_or_ambiguous": rejected,
            "keyword_nets": previews, "copy_ready_snippet":
            {"SEEDS": seeds, "ANCHORS": anchors, "KW": queries},
            "live_board_created": False, "database_written": False}


def render_markdown(report):
    def rows(items, fields):
        return "\n".join("- " + " | ".join(str(item.get(f, "")) for f in fields) for item in items) or "- (none)"
    lines = [f"# Topic bootstrap review: {report['topic']}", "", report["description"], "",
             "## Verified anchors", rows(report["verified_anchors"], ["category", "arxiv_id", "title", "published", "match_scores"]),
             "", "## Proposed but unverified", rows(report["proposed_but_unverified"], ["category", "reason", "nominated"]),
             "", "## Rejected or ambiguous", rows(report["rejected_or_ambiguous"], ["category", "reason", "nominated"]),
             "", "## Keyword-net preview"]
    for net in report["keyword_nets"]:
        lines.append(f"### {net['label']} — {net['coverage']}")
        for q in net["queries"]:
            lines.append(f"- `{q['query']}`: {q['result_count']} results; flag={q['flag']}; {q['representative_titles']}")
    lines += ["", "## Copy-ready snippet", "```python", f"SEEDS = {report['copy_ready_snippet']['SEEDS']!r}",
              f"ANCHORS = {report['copy_ready_snippet']['ANCHORS']!r}", f"KW = {report['copy_ready_snippet']['KW']!r}", "```",
              "", "No approval or live-board/database action was performed."]
    return "\n".join(lines) + "\n"


def render_html(report):
    md = render_markdown(report)
    body = "<pre style='white-space:pre-wrap;font:14px system-ui'>" + html.escape(md) + "</pre>"
    return "<!doctype html><meta charset='utf-8'><title>Topic bootstrap review</title>" + body


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--topic", required=True)
    p.add_argument("--describe", required=True)
    p.add_argument("--foundational-n", type=int, default=12)
    p.add_argument("--recent-n", type=int, default=12)
    p.add_argument("--queries-per-flavor", type=int, default=3)
    p.add_argument("--output", default=None, help="output basename; .json/.md/.html are appended")
    p.add_argument("--html-review", action="store_true")
    p.add_argument("--model", default="gpt-5.6-sol")
    args = p.parse_args(argv)
    base = Path(args.output or f"daily_bot/output/{args.topic}_bootstrap")
    base.parent.mkdir(parents=True, exist_ok=True)
    nomination = nominate(args.topic, args.describe, args.model, args.foundational_n,
                          args.recent_n, args.queries_per_flavor)
    verified, unverified, rejected = [], [], []
    for category, limit in (("foundational", args.foundational_n), ("recent_influential", args.recent_n)):
        for candidate in (nomination.get(category) or [])[:max(0, limit)]:
            bucket, evidence = verify_candidate(candidate, category)
            {"verified_anchors": verified, "proposed_but_unverified": unverified,
             "rejected_or_ambiguous": rejected}[bucket].append(evidence)
    previews = preview_keywords(nomination.get("keyword_nets"), args.topic, args.queries_per_flavor)
    report = build_report(args.topic, args.describe, nomination, verified, unverified, rejected, previews)
    report["nomination_error"] = nomination.get("_error")
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    html_path = None
    if args.html_review:
        html_path = base.with_suffix(".html")
        html_path.write_text(render_html(report), encoding="utf-8")
    print(json.dumps({"verified": len(verified), "proposed_but_unverified": len(unverified),
                      "rejected_or_ambiguous": len(rejected), "json": str(json_path),
                      "markdown": str(md_path), "html": str(html_path) if html_path else None},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
