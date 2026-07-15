import time

from config import ARXIV_QUERIES, DAILY_FETCH_LIMIT, ENRICHMENT_BATCH
from arxiv_client import fetch_new_papers
from enrichers import enrich_paper
from database import (
    get_connection,
    get_seen_ids,
    get_pending_enrichment,
    insert_paper,
    update_enriched,
)


def run_crawl(conn):
    """抓取所有查询的新论文并入库。返回新入库的论文数。"""
    seen_ids = get_seen_ids(conn)
    inserted = 0
    for query in ARXIV_QUERIES:
        papers = fetch_new_papers(query, DAILY_FETCH_LIMIT, seen_ids)
        for paper in papers:
            insert_paper(conn, paper)
            # 本轮已入库的也纳入去重集，避免跨查询重复
            seen_ids.add(paper["arxiv_id"])
            inserted += 1
        print(f"[crawl] query 命中 {len(papers)} 篇新论文: {query}")
        time.sleep(5)
    return inserted


def run_enrichment(conn):
    """对未增强的论文批量补充字段。返回处理的论文数。"""
    rows = get_pending_enrichment(conn, ENRICHMENT_BATCH)
    for arxiv_id, title, enriched in rows:
        # enriched=TRUE 说明已尝试过一次 → 这是 OpenAlex 重试，不再重复请求 HF
        data = enrich_paper(arxiv_id, title, include_hf=not enriched)
        update_enriched(conn, arxiv_id, data)
        # 限速已由 enrich_paper 内部逐请求处理，这里不再额外 sleep
    return len(rows)


def main():
    conn = get_connection()
    try:
        crawled = run_crawl(conn)
        print(f"[main] 本次新入库 {crawled} 篇")
        enriched = run_enrichment(conn)
        print(f"[main] 本次增强 {enriched} 篇")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
