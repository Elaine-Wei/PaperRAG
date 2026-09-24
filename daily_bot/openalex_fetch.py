"""OpenAlex discovery fallback for the daily arXiv fetch path."""

import datetime as _dt
import os
import re
import sys
import time
import json
import urllib.error
import urllib.parse
import urllib.request


_HERE = os.path.dirname(os.path.abspath(__file__))
_CRAWLER_DIR = os.path.join(os.path.dirname(_HERE), "crawler")
if _CRAWLER_DIR not in sys.path:
    sys.path.insert(0, _CRAWLER_DIR)

try:
    from config import OPENALEX_API_KEY, OPENALEX_MAILTO
except Exception:  # pragma: no cover - usable even if crawler config is unavailable
    OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY")
    OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO")


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
USER_AGENT = "PaperRAG-daily-bot/0" + (
    f" (+mailto:{OPENALEX_MAILTO})" if OPENALEX_MAILTO else ""
)
PER_PAGE = 100
DATE_WINDOW_DAYS = 7

_ARXIV_ID_RE = re.compile(
    r"(?i)(?:arxiv\.org/(?:abs|pdf|format)/|10\.48550/arxiv\.)"
    r"((?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v\d+)?)"
)
_TERM_RE = re.compile(r"(?:ti|abs):(?:\"([^\"]+)\"|([^\s()]+))", re.I)


def _auth_params():
    params = {"mailto": OPENALEX_MAILTO} if OPENALEX_MAILTO else {}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    return params


def _fallback_terms(query):
    terms = [a or b for a, b in _TERM_RE.findall(query or "")]
    if terms:
        return terms
    q = (query or "").lower()
    if any(cat in q for cat in ("q-fin.tr", "q-fin.pm", "q-fin.cp", "q-fin.st")):
        return ["quantitative finance", "trading", "portfolio", "market microstructure"]
    if "cs.dc" in q or "cs.pf" in q:
        return ["distributed computing", "parallel computing", "performance", "low latency"]
    if "math.oc" in q:
        return ["optimization", "operations research", "optimal control"]
    # Topic-board callers may provide a bare quoted paper title, optionally
    # followed by an author surname, rather than arXiv ti:/abs: syntax.
    literal = (query or "").strip()
    if literal.startswith('"'):
        closing = literal.find('"', 1)
        if closing > 0:
            literal = (literal[1:closing] + literal[closing + 1:]).strip()
    return [literal] if literal else []


def translate_query(query):
    terms = _fallback_terms(query)
    rendered = []
    for term in terms:
        term = term.strip()
        if term:
            rendered.append(f'"{term}"' if " " in term else term)
    return " OR ".join(rendered)


def _date_window(today=None, date_window_days=DATE_WINDOW_DAYS):
    if date_window_days is None:
        return None, None
    if date_window_days <= 0:
        raise ValueError("date_window_days must be positive or None")
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    return today - _dt.timedelta(days=date_window_days - 1), today


def _request_json(params, endpoint=OPENALEX_WORKS_URL):
    request_params = _auth_params()
    request_params.update(params)
    headers = {"User-Agent": USER_AGENT}
    last_error = None
    for attempt in range(2):
        try:
            url = endpoint + "?" + urllib.parse.urlencode(request_params)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                response_headers = response.headers
                payload = json.loads(response.read().decode("utf-8"))
            if status == 429:
                if attempt:
                    raise RuntimeError("OpenAlex returned HTTP 429")
                retry_after = response_headers.get("Retry-After")
                try:
                    delay = max(1.0, min(float(retry_after), 60.0)) if retry_after else 5.0
                except (TypeError, ValueError):
                    delay = 5.0
                time.sleep(delay)
                continue
            if status >= 500:
                raise RuntimeError(f"OpenAlex returned HTTP {status}")
            return payload or {}
        except urllib.error.HTTPError as exc:
            last_error = exc
            if attempt:
                break
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = max(1.0, min(float(retry_after), 60.0)) if retry_after else 5.0
                except (TypeError, ValueError):
                    delay = 5.0
                time.sleep(delay)
            elif exc.code >= 500:
                time.sleep(2.0)
            else:
                break
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt:
                break
            time.sleep(2.0)
    raise RuntimeError(f"OpenAlex request failed: {last_error}")


def _request(query_text, start_date=None, end_date=None):
    params = _auth_params()
    filters = ["indexed_in:arxiv"]
    if start_date is not None and end_date is not None:
        filters.extend([
            f"from_publication_date:{start_date.isoformat()}",
            f"to_publication_date:{end_date.isoformat()}",
        ])
    params.update({
        "search": query_text,
        "filter": ",".join(filters),
        "sort": "publication_date:desc",
        "per_page": PER_PAGE,
        "select": (
            "id,display_name,title,abstract_inverted_index,authorships,publication_date,"
            "ids,doi,indexed_in,locations,primary_location,best_oa_location"
        ),
    })
    return (_request_json(params).get("results") or [])


def fetch_works_by_arxiv_ids(arxiv_ids):
    """Batch lookup works using their canonical arXiv DOI aliases.

    OpenAlex's filter syntax uses one field followed by OR-separated values;
    it is distinct from the search-based fallback request above.
    """
    ids = [str(aid).strip() for aid in arxiv_ids if str(aid).strip()]
    if not ids:
        return {}
    doi_values = "|".join(f"10.48550/arXiv.{aid}" for aid in ids)
    payload = _request_json({
        "filter": f"doi:{doi_values}",
        "per_page": min(PER_PAGE, len(ids)),
        "select": (
            "id,ids,display_name,publication_date,publication_year,authorships,"
            "abstract_inverted_index,cited_by_count,counts_by_year,"
            "fwci,citation_normalized_percentile"
        ),
    })
    out = {}
    for work in payload.get("results") or []:
        aid = extract_arxiv_id(work)
        if aid:
            out[aid] = work
    return out


def _extract_id_from_text(value):
    if not isinstance(value, str):
        return None
    match = _ARXIV_ID_RE.search(value)
    return match.group(1).split("v", 1)[0] if match else None


def _locations(work, names):
    for name in names:
        value = work.get(name)
        if isinstance(value, list):
            for loc in value:
                if isinstance(loc, dict):
                    yield loc
        elif isinstance(value, dict):
            yield value


def extract_arxiv_id(work):
    """Extract explicit arXiv IDs only; never infer one from an OpenAlex ID."""
    for loc in _locations(work, ["locations"]):
        aid = _extract_id_from_text(loc.get("landing_page_url"))
        if aid:
            return aid
    for loc in _locations(work, ["locations"]):
        aid = _extract_id_from_text(loc.get("pdf_url"))
        if aid:
            return aid
    for loc in _locations(work, ["primary_location", "best_oa_location"]):
        for key in ("landing_page_url", "pdf_url"):
            aid = _extract_id_from_text(loc.get(key))
            if aid:
                return aid
    for value in (work.get("doi"), (work.get("ids") or {}).get("doi")):
        aid = _extract_id_from_text(value)
        if aid:
            return aid
    for value in work.values():
        if isinstance(value, str):
            aid = _extract_id_from_text(value)
            if aid:
                return aid
    return None


def _abstract(inverted):
    if not isinstance(inverted, dict):
        return ""
    positions = {}
    for word, indexes in inverted.items():
        for index in indexes if isinstance(indexes, list) else []:
            try:
                positions[int(index)] = word
            except (TypeError, ValueError):
                continue
    return " ".join(positions[i] for i in sorted(positions))


def _normalize(work, arxiv_id):
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") if isinstance(authorship, dict) else None
        name = author.get("display_name") if isinstance(author, dict) else None
        if name:
            authors.append(name)
    return {
        "arxiv_id": arxiv_id,
        "title": work.get("display_name") or work.get("title") or "",
        "abstract": _abstract(work.get("abstract_inverted_index")),
        "authors": authors,
        "categories": [],
        "published": work.get("publication_date") or "",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "arxiv_comment": None,
        "journal_ref": None,
        "fetch_source": "openalex-fallback",
    }


def fetch_fallback(query, date_window_days=DATE_WINDOW_DAYS):
    """Return normalized papers; ``None`` searches the full indexed-in-arXiv history."""
    search = translate_query(query)
    counters = {"results": 0, "accepted": 0,
                "discarded_no_arxiv_id": 0, "duplicate_arxiv_ids": 0}
    if not search:
        return [], counters
    start_date, end_date = _date_window(date_window_days=date_window_days)
    works = _request(search, start_date, end_date)
    counters["results"] = len(works)
    accepted, seen = [], set()
    for work in works:
        if not isinstance(work, dict):
            counters["discarded_no_arxiv_id"] += 1
            continue
        arxiv_id = extract_arxiv_id(work)
        if not arxiv_id:
            counters["discarded_no_arxiv_id"] += 1
            continue
        if arxiv_id in seen:
            counters["duplicate_arxiv_ids"] += 1
            continue
        seen.add(arxiv_id)
        accepted.append(_normalize(work, arxiv_id))
    counters["accepted"] = len(accepted)
    return accepted, counters
