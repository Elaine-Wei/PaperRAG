"""OpenAlex enrichment and versioned Classic-board scoring."""

import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CRAWLER = os.path.join(os.path.dirname(_HERE), "crawler")
if _CRAWLER not in sys.path:
    sys.path.insert(0, _CRAWLER)

import openalex_fetch


SCHEMA_PATH = os.path.join(_HERE, "schema_impact.sql")
CLASSIC_SCORE_VERSION = "v2.1"
BATCH_SIZE = 100
REFRESH_DAYS = 7


def ensure_schema(conn):
    with conn.cursor() as cur:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
            cur.execute(handle.read())
    conn.commit()


def _complete_years(published, today):
    """All complete calendar years after publication, through last year."""
    first = published.year + 1
    last = today.year - 1
    return list(range(first, last + 1)) if last >= first else []


def _counts_by_year(work):
    rows = work.get("counts_by_year") or []
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            year = int(row.get("year"))
            count = int(row.get("cited_by_count") or 0)
        except (TypeError, ValueError):
            continue
        out[year] = max(0, count)
    return out


def _impact_pct(work):
    percentile = work.get("citation_normalized_percentile")
    if isinstance(percentile, dict):
        percentile = percentile.get("value")
    try:
        value = float(percentile)
    except (TypeError, ValueError):
        value = None
    if value is not None:
        # OpenAlex normally returns a 0..1 percentile value; tolerate 0..100.
        if value > 1.0:
            value /= 100.0
        return max(0.0, min(1.0, value))
    try:
        fwci = max(0.0, float(work.get("fwci")))
    except (TypeError, ValueError):
        return None
    return fwci / (1.0 + fwci)


def _has_impact_signal(work):
    if not isinstance(work, dict):
        return False
    percentile = work.get("citation_normalized_percentile")
    if isinstance(percentile, dict):
        percentile = percentile.get("value")
    try:
        if percentile is not None:
            float(percentile)
            return True
    except (TypeError, ValueError):
        pass
    try:
        if work.get("fwci") is not None:
            float(work.get("fwci"))
            return True
    except (TypeError, ValueError):
        pass
    return any(count > 0 for count in _counts_by_year(work).values())


def calculate_classic_score(work, published, today=None):
    """Return v2 fields; eligibility is driven by OpenAlex data, not age."""
    today = today or dt.date.today()
    counts = _counts_by_year(work)
    complete_years = _complete_years(published, today)
    active = sum(counts.get(year, 0) > 0 for year in complete_years)
    impact = _impact_pct(work)
    eligible = _has_impact_signal(work)
    if not eligible or impact is None:
        return {"active_citation_years": active, "classic_score": None, "eligible": eligible}

    # OpenAlex exposes annual counts, so only complete calendar years can
    # contribute. Young papers simply omit these optional components.
    components = [(impact, 0.85)]
    if complete_years:
        persistence = active / len(complete_years)
        components.append((persistence, 0.10))
        recent_years = complete_years[-2:]
        recent_activity = sum(counts.get(year, 0) > 0 for year in recent_years) / len(recent_years)
        components.append((recent_activity, 0.05))
    total_weight = sum(weight for _, weight in components)
    score = 100.0 * sum(value * weight for value, weight in components) / total_weight
    return {
        "active_citation_years": active,
        "classic_score": round(score, 2),
        "eligible": eligible,
    }


def _due(row, today):
    if row["refreshed_at"] is None or row["classic_score_version"] != CLASSIC_SCORE_VERSION:
        return True
    refreshed = row["refreshed_at"].date() if hasattr(row["refreshed_at"], "date") else row["refreshed_at"]
    return (today - refreshed).days >= REFRESH_DAYS


def _corpus_rows(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.arxiv_id, p.published, pi.refreshed_at, pi.classic_score_version
            FROM papers p
            LEFT JOIN paper_impact pi ON pi.arxiv_id=p.arxiv_id
            WHERE p.published IS NOT NULL
            ORDER BY p.published, p.arxiv_id
        """)
        return [{"paper_id": r[0], "arxiv_id": r[1], "published": r[2],
                 "refreshed_at": r[3], "classic_score_version": r[4]} for r in cur.fetchall()]


def _upsert(conn, row, work, today):
    fields = calculate_classic_score(work or {}, row["published"], today)
    openalex_id = (work or {}).get("id")
    citation_count = (work or {}).get("cited_by_count")
    counts = (work or {}).get("counts_by_year") or []
    fwci = (work or {}).get("fwci")
    percentile = (work or {}).get("citation_normalized_percentile")
    if isinstance(percentile, dict):
        percentile = percentile.get("value")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO paper_impact
              (paper_id, arxiv_id, openalex_id, citation_count, counts_by_year, fwci,
               citation_normalized_percentile, active_citation_years, classic_score,
               classic_score_version, eligible, openalex_updated_at, refreshed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    CASE WHEN %s IS NULL THEN NULL ELSE NOW() END, NOW())
            ON CONFLICT (arxiv_id) DO UPDATE SET
              paper_id=EXCLUDED.paper_id, openalex_id=EXCLUDED.openalex_id,
              citation_count=EXCLUDED.citation_count, counts_by_year=EXCLUDED.counts_by_year,
              fwci=EXCLUDED.fwci, citation_normalized_percentile=EXCLUDED.citation_normalized_percentile,
              active_citation_years=EXCLUDED.active_citation_years, classic_score=EXCLUDED.classic_score,
              classic_score_version=EXCLUDED.classic_score_version, eligible=EXCLUDED.eligible,
              openalex_updated_at=EXCLUDED.openalex_updated_at, refreshed_at=EXCLUDED.refreshed_at
        """, (row["paper_id"], row["arxiv_id"], openalex_id, citation_count,
              json.dumps(counts), fwci, percentile, fields["active_citation_years"],
              fields["classic_score"], CLASSIC_SCORE_VERSION, fields["eligible"], openalex_id))


def refresh(conn, today=None, batch_size=BATCH_SIZE):
    """Refresh due eligible papers and return enrichment statistics."""
    today = today or dt.date.today()
    rows = _corpus_rows(conn)
    due = [row for row in rows if _due(row, today)]
    stats = {"corpus": len(rows), "due": len(due), "enriched": 0,
             "usable_signal": 0, "scored": 0, "no_openalex_match": 0,
             "no_impact_signal": 0, "failed": 0, "batches": 0}
    for start in range(0, len(due), batch_size):
        batch = due[start:start + batch_size]
        stats["batches"] += 1
        try:
            works = openalex_fetch.fetch_works_by_arxiv_ids([r["arxiv_id"] for r in batch])
        except Exception as exc:
            stats["failed"] += len(batch)
            print(f"[classic][WARN] OpenAlex batch failed ({len(batch)} papers): {exc}")
            continue
        for row in batch:
            work = works.get(row["arxiv_id"])
            if work is None:
                stats["no_openalex_match"] += 1
                _upsert(conn, row, {}, today)
            else:
                stats["enriched"] += 1
                _upsert(conn, row, work, today)
                if _has_impact_signal(work):
                    stats["usable_signal"] += 1
                else:
                    stats["no_impact_signal"] += 1
                if calculate_classic_score(work, row["published"], today)["classic_score"] is not None:
                    stats["scored"] += 1
        conn.commit()
    return stats


def classic_top(conn, limit=10):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pi.arxiv_id, p.title, p.published, pi.classic_score,
                   pi.citation_count, pi.active_citation_years,
                   pi.citation_normalized_percentile, pi.fwci
            FROM paper_impact pi JOIN papers p USING (arxiv_id)
            WHERE pi.eligible IS TRUE AND pi.classic_score IS NOT NULL
            ORDER BY pi.classic_score DESC, p.published ASC, pi.arxiv_id DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def dry_run(conn, limit=10, today=None):
    """Refresh due records, then print the real current Classic Top-N."""
    ensure_schema(conn)
    stats = refresh(conn, today=today)
    rows = classic_top(conn, limit)
    print("\n===== DRY-RUN 经典沉淀榜 =====")
    print(f"corpus={stats['corpus']} enriched={stats['enriched']} "
          f"usable_signal={stats['usable_signal']} scored={stats['scored']} "
          f"no_match={stats['no_openalex_match']} no_signal={stats['no_impact_signal']} "
          f"failed={stats['failed']} version={CLASSIC_SCORE_VERSION}")
    for i, row in enumerate(rows, 1):
        aid, title, published, score, citations, active, percentile, fwci = row
        print(f"{i:>2}. {aid} score={score} published={published} "
              f"citations={citations} active_years={active} title={title}")
    return {"stats": stats, "rows": rows}
