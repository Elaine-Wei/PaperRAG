#!/usr/bin/env python3
"""Review-gated topic bootstrap engine.

The review invocation delegates nomination, arXiv verification, and keyword
previewing to topic_bootstrap.py.  It writes only a review artifact.  A later
--promote invocation merges that artifact into the topic bootstrap JSON and,
for a new topic, topics_config.json after validating the result with the real
topic_config loader.
"""

import argparse
import copy
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import topic_bootstrap  # noqa: E402
import topic_config  # noqa: E402


OUTPUT_DIR = HERE / "output" / "topic_engine"
MANUAL_DIR = HERE / "manual_topic_bootstrap"
TOPICS_CONFIG = HERE / "topics_config.json"
ALIASES = {"期权": "options", "cta": "cta", "高频": "hft", "hft": "hft"}


def _slug(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value)
    return value.strip("_.") or "topic"


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def _topic_key(topic):
    """Resolve a human label to an existing config key, or return a new slug."""
    raw = str(topic or "").strip()
    candidate = ALIASES.get(raw.lower(), ALIASES.get(raw, _slug(raw)))
    config = _read_json(TOPICS_CONFIG).get("topics", {})
    if candidate in config:
        return candidate
    for key, item in config.items():
        label = str(item.get("label") or "").split("/", 1)[0].strip().lower()
        if raw.lower() in {key.lower(), label}:
            return key
    for path in MANUAL_DIR.glob("*.json"):
        try:
            artifact = _read_json(path)
        except (OSError, ValueError):
            continue
        if str(artifact.get("topic") or "").strip().lower() in {raw.lower(), candidate.lower()}:
            return path.stem
    return candidate


def _review_paths(topic_key):
    base = OUTPUT_DIR / f"{_slug(topic_key)}_review"
    return base.with_suffix(".json"), base.with_suffix(".md")


def _artifact_path(topic_key):
    return MANUAL_DIR / f"{_slug(topic_key)}.json"


def _existing_artifact(topic_key):
    path = _artifact_path(topic_key)
    return path, (_read_json(path) if path.exists() else None)


def _verified_ids(artifact):
    return {str(item.get("arxiv_id") or "").strip()
            for item in (artifact or {}).get("verified_anchors", [])
            if str(item.get("arxiv_id") or "").strip()}


def _query_strings(artifact):
    queries = []
    for net in (artifact or {}).get("keyword_nets", []) or []:
        for item in (net.get("queries") or []) if isinstance(net, dict) else []:
            query = item.get("query") if isinstance(item, dict) else item
            query = str(query or "").strip()
            if query:
                queries.append(query)
    return queries


def _merge_keyword_nets(existing, proposed):
    """Merge by label while preserving existing queries and removing duplicates."""
    merged = copy.deepcopy(existing or [])
    by_label = {str(net.get("label") or "unnamed"): net for net in merged
                if isinstance(net, dict)}
    for net in proposed or []:
        if not isinstance(net, dict):
            continue
        label = str(net.get("label") or "unnamed")
        target = by_label.get(label)
        if target is None:
            target = {"label": label, "coverage": net.get("coverage") or "", "queries": []}
            merged.append(target)
            by_label[label] = target
        seen = set(_query_strings({"keyword_nets": [target]}))
        for item in net.get("queries") or []:
            query = item.get("query") if isinstance(item, dict) else item
            query = str(query or "").strip()
            if query and query not in seen:
                target.setdefault("queries", []).append(query)
                seen.add(query)
    return merged


def _merge_artifacts(topic_key, description, existing, review):
    existing = existing or {"topic": topic_key, "verified_anchors": [], "keyword_nets": []}
    merged = copy.deepcopy(existing)
    merged["topic"] = existing.get("topic") or topic_key
    if description and not merged.get("description"):
        merged["description"] = description
    anchors = list(merged.get("verified_anchors") or [])
    seen = _verified_ids(merged)
    added = []
    for item in review.get("verified_anchors") or []:
        aid = str(item.get("arxiv_id") or "").strip()
        if not aid or aid in seen:
            continue
        anchors.append(item)
        added.append(item)
        seen.add(aid)
    merged["verified_anchors"] = anchors
    merged["keyword_nets"] = _merge_keyword_nets(merged.get("keyword_nets"),
                                                   review.get("keyword_nets"))
    seeds = {item["arxiv_id"]: item.get("title", "") for item in anchors
             if item.get("category") == "foundational"}
    anchors_map = {item["arxiv_id"]: item.get("title", "") for item in anchors
                   if item.get("category") != "foundational"}
    merged["copy_ready_snippet"] = {
        "SEEDS": seeds,
        "ANCHORS": anchors_map,
        "KW": _query_strings(merged),
    }
    merged["engine_last_review"] = review.get("generated_at")
    merged["engine_review_artifact"] = str(_review_paths(topic_key)[0])
    return merged, added


def _prefilter_terms(artifact, topic_key):
    """Derive required loader terms from approved keyword nets for new topics."""
    terms = []
    for query in _query_strings(artifact):
        if any(marker in query for marker in ("ti:", "abs:", "(", ")")):
            continue
        if query not in terms:
            terms.append(query)
    return terms[:30] or [topic_key]


def _new_topic_config(topic_key, description, artifact):
    return {
        "label": topic_key,
        "description": description or artifact.get("description") or topic_key,
        "bootstrap_json": f"manual_topic_bootstrap/{_slug(topic_key)}.json",
        "prefilter_terms": _prefilter_terms(artifact, topic_key),
        "weights": {"freshness": 0.2, "reproducibility": 0.2, "novelty": 0.2,
                    "domain_relevance": 0.2, "authority": 0.2},
        "top_n": 10,
        "study_top": 6,
        "shortlist_top": 30,
        "foundational_days": 365,
    }


def _validate_candidate_config(config, topic_key, artifact_path):
    with tempfile.TemporaryDirectory(prefix="topic-engine-") as temp_dir:
        temp_dir = Path(temp_dir)
        temp_artifact = temp_dir / artifact_path.name
        _write_json(temp_artifact, config["topics"][topic_key]["_artifact"])
        candidate_config = copy.deepcopy(config)
        candidate_config["topics"][topic_key] = copy.deepcopy(config["topics"][topic_key])
        candidate_config["topics"][topic_key]["bootstrap_json"] = str(temp_artifact)
        candidate_config["topics"][topic_key].pop("_artifact", None)
        fd, temp_config_name = tempfile.mkstemp(
            prefix="topics_config-", suffix=".json", dir=TOPICS_CONFIG.parent)
        Path(temp_config_name).unlink(missing_ok=True)
        temp_config = Path(temp_config_name)
        try:
            _write_json(temp_config, candidate_config)
            # Keep the candidate config beside the real config so all other
            # relative bootstrap_json paths resolve exactly as in production.
            topic_config.load_topics_config(temp_config)
        finally:
            temp_config.unlink(missing_ok=True)


def _atomic_promote(topic_key, description, review):
    artifact_path, existing = _existing_artifact(topic_key)
    merged, added = _merge_artifacts(topic_key, description, existing, review)
    config = _read_json(TOPICS_CONFIG)
    topics = config.setdefault("topics", {})
    if topic_key not in topics:
        topics[topic_key] = _new_topic_config(topic_key, description, merged)
    topics[topic_key]["_artifact"] = merged
    _validate_candidate_config(config, topic_key, artifact_path)
    topics[topic_key].pop("_artifact", None)

    artifact_tmp = artifact_path.with_suffix(".json.tmp")
    config_tmp = TOPICS_CONFIG.with_suffix(".json.tmp")
    try:
        _write_json(artifact_tmp, merged)
        _write_json(config_tmp, config)
        artifact_tmp.replace(artifact_path)
        config_tmp.replace(TOPICS_CONFIG)
    finally:
        artifact_tmp.unlink(missing_ok=True)
        config_tmp.unlink(missing_ok=True)
    topic_config.load_topics_config()
    return artifact_path, added


def _print_preview(topic_key, review, merged, added):
    print(f"Topic: {review.get('topic') or topic_key} (config key: {topic_key})")
    print(f"Verified: {len(review.get('verified_anchors') or [])}; "
          f"proposed-but-unverified: {len(review.get('proposed_but_unverified') or [])}; "
          f"rejected/ambiguous: {len(review.get('rejected_or_ambiguous') or [])}")
    print("Verified candidates:")
    for item in review.get("verified_anchors") or []:
        print(f"  - {item.get('arxiv_id')}: {item.get('title')}")
    print(f"Existing verified seeds: {len(_verified_ids(merged)) - len(added)}")
    print(f"New seeds to add: {len(added)}")
    for item in added:
        print(f"  + {item.get('arxiv_id')}: {item.get('title')}")
    print("Merged seed IDs:")
    print("  " + ", ".join(sorted(_verified_ids(merged))))
    print("No config or board file was written. Review artifact is required for --promote.")


def _run_review(args, topic_key):
    nomination = topic_bootstrap.nominate(args.topic, args.describe, args.model,
                                          args.foundational_n, args.recent_n,
                                          args.queries_per_flavor)
    verified, unverified, rejected = [], [], []
    buckets = {"verified_anchors": verified, "proposed_but_unverified": unverified,
               "rejected_or_ambiguous": rejected}
    for category, limit in (("foundational", args.foundational_n),
                            ("recent_influential", args.recent_n)):
        for candidate in (nomination.get(category) or [])[:max(0, limit)]:
            bucket, evidence = topic_bootstrap.verify_candidate(candidate, category)
            buckets[bucket].append(evidence)
    previews = topic_bootstrap.preview_keywords(nomination.get("keyword_nets"),
                                                args.topic, args.queries_per_flavor)
    report = topic_bootstrap.build_report(args.topic, args.describe, nomination,
                                          verified, unverified, rejected, previews)
    report["nomination_error"] = nomination.get("_error")
    report["engine_topic_key"] = topic_key
    report["engine_review_only"] = True
    json_path, md_path = _review_paths(topic_key)
    _write_json(json_path, report)
    md_path.write_text(topic_bootstrap.render_markdown(report), encoding="utf-8")
    merged, added = _merge_artifacts(topic_key, args.describe,
                                     _existing_artifact(topic_key)[1], report)
    _print_preview(topic_key, report, merged, added)
    print(f"Review JSON: {json_path}")
    print(f"Review Markdown: {md_path}")


def _promote(topic_key):
    json_path, _ = _review_paths(topic_key)
    if not json_path.exists():
        raise SystemExit(f"No review artifact at {json_path}; run without --promote first.")
    review = _read_json(json_path)
    artifact_path, added = _atomic_promote(topic_key, review.get("description"), review)
    print(f"Promoted {len(added)} new verified seed(s) into {artifact_path}")
    print(f"Validated through topic_config.load_topics_config(): {TOPICS_CONFIG}")
    print(f"Next dry-run: python daily_bot/run_topic.py --topic {topic_key} --dry-run")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--describe")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--foundational-n", type=int, default=10)
    parser.add_argument("--recent-n", type=int, default=10)
    parser.add_argument("--queries-per-flavor", type=int, default=3)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args(argv)
    topic_key = _topic_key(args.topic)
    if args.promote:
        _promote(topic_key)
        return 0
    if not args.describe:
        parser.error("--describe is required unless --promote is used")
    _run_review(args, topic_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
