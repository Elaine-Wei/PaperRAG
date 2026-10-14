#!/usr/bin/env python3
"""Generate and persist cheap Pass-1 outlines for the rolling daily board."""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db  # noqa: E402
import deep_study  # noqa: E402

READER_SCHEMA = os.path.join(HERE, "schema_reader.sql")


def _connection():
    return db.ensure(db.get_connection())


def _checkpoint_fingerprint(arxiv_id, model):
    path = deep_study._cp_path(arxiv_id, model)
    checkpoint = deep_study._cp_load(path)
    return checkpoint.get("fingerprint") if checkpoint else None


def outline_document(result):
    """Keep the public outline small while retaining source text for Pass-2."""
    chunks = {}
    sections = []
    for section_index, section in enumerate(result.get("outline") or []):
        mapped = []
        for chunk in section.get("chunks") or []:
            idx = int(chunk.get("_idx", len(chunks)))
            item = {
                "index": idx,
                "label": chunk.get("label", ""),
                "title": chunk.get("title", ""),
                "text": chunk.get("text", ""),
            }
            chunks[idx] = item
            mapped.append({"index": idx, "label": item["label"],
                           "title": item["title"]})
        sections.append({"index": section_index, "title": section.get("title", ""),
                         "chunks": mapped})
    return {
        "hook": result.get("hook", ""),
        "takeaway": result.get("takeaway", ""),
        "big_sections": sections,
        "source_chunks": [chunks[i] for i in sorted(chunks)],
        "excluded": result.get("excluded") or [],
        "split_method": result.get("split_method"),
    }


def _upsert_outline(conn, arxiv_id, outline, fingerprint, model):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO reader_outline
                 (arxiv_id, outline, source_fingerprint, model)
               VALUES (%s, %s::jsonb, %s, %s)
               ON CONFLICT (arxiv_id) DO UPDATE SET
                 outline=EXCLUDED.outline,
                 source_fingerprint=EXCLUDED.source_fingerprint,
                 model=EXCLUDED.model,
                 created_at=NOW()""",
            (arxiv_id, json.dumps(outline, ensure_ascii=False), fingerprint, model))
    conn.commit()


def _existing_outline(conn, arxiv_id):
    with conn.cursor() as cur:
        cur.execute("SELECT source_fingerprint, model FROM reader_outline WHERE arxiv_id=%s",
                    (arxiv_id,))
        row = cur.fetchone()
    return {"source_fingerprint": row[0], "model": row[1]} if row else None


def generate_outlines(top=10, window=7, model="ds-direct"):
    conn = _connection()
    try:
        with open(READER_SCHEMA, encoding="utf-8") as handle:
            with conn.cursor() as cur:
                cur.execute(handle.read())
            conn.commit()
        rows = [row for row in db.get_window_batch(conn, window)
                if row.get("composite") is not None][:max(0, top)]
    finally:
        conn.close()

    results = {"generated": [], "skipped": [], "failed": []}
    for row in rows:
        aid = row["arxiv_id"]
        try:
            conn = _connection()
            try:
                existing = _existing_outline(conn, aid)
            finally:
                conn.close()
            current_fp = _checkpoint_fingerprint(aid, model)
            if existing and existing["model"] == model and current_fp == existing["source_fingerprint"]:
                results["skipped"].append(aid)
                continue

            result = deep_study.generate_study(aid, model=model, outline_only=True)
            if result.get("mode") != "outline-only":
                raise RuntimeError(result.get("mode") or "outline generation failed")
            fingerprint = _checkpoint_fingerprint(aid, model)
            if not fingerprint:
                raise RuntimeError("outline checkpoint has no source fingerprint")
            outline = outline_document(result)
            conn = _connection()
            try:
                _upsert_outline(conn, aid, outline, fingerprint, model)
            finally:
                conn.close()
            results["generated"].append(aid)
        except Exception as exc:
            results["failed"].append({"arxiv_id": aid, "error": str(exc)[:240]})
            print(f"[reader-outline][WARN] {aid}: {exc}", flush=True)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--window", type=int, default=7)
    parser.add_argument("--model", default="ds-direct")
    args = parser.parse_args(argv)
    result = generate_outlines(args.top, args.window, args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
