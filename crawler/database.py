import urllib.parse

import psycopg2

from config import DATABASE_URL


def get_connection():
    """
    从 DATABASE_URL 建连接。稳健解析：把 URI 拆成 keyword 参数再连接，避免密码里未做
    percent-encode 的特殊字符破坏 libpq 的 URI 解析（Supabase pooler 连接串常有此问题）。
    远程库（非 localhost，如 Supabase）默认启用 SSL（sslmode=require），本地保持原行为。
    """
    p = urllib.parse.urlparse(DATABASE_URL)
    if p.scheme and p.hostname:  # URI 形式
        q = urllib.parse.parse_qs(p.query or "")
        kw = {
            "host": p.hostname,
            "port": p.port or 5432,
            "dbname": (p.path or "/postgres").lstrip("/") or "postgres",
            "user": p.username,
            "password": p.password,
        }
        sslmode = q.get("sslmode", [None])[0]
        if sslmode:
            kw["sslmode"] = sslmode
        elif p.hostname not in ("localhost", "127.0.0.1", "::1"):
            kw["sslmode"] = "require"  # 远程库默认要求 SSL
        return psycopg2.connect(**kw)
    return psycopg2.connect(DATABASE_URL)  # 非 URI（本地 DSN 等）保持原样


def get_seen_ids(conn):
    """返回库中已有的全部 arxiv_id 集合，供抓取端去重。"""
    with conn.cursor() as cur:
        cur.execute("SELECT arxiv_id FROM papers;")
        return {row[0] for row in cur.fetchall()}


def insert_paper(conn, paper):
    """INSERT 一篇论文；arxiv_id 冲突时忽略。"""
    sql = """
        INSERT INTO papers (
            arxiv_id, title, abstract, authors, categories, published, pdf_url,
            arxiv_comment, journal_ref
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (arxiv_id) DO NOTHING;
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                paper.get("arxiv_id"),
                paper.get("title"),
                paper.get("abstract"),
                paper.get("authors"),
                paper.get("categories"),
                paper.get("published") or None,
                paper.get("pdf_url"),
                paper.get("arxiv_comment"),
                paper.get("journal_ref"),
            ),
        )
    conn.commit()


def get_pending_enrichment(conn, limit):
    """
    待增强论文的 (arxiv_id, title, enriched) 列表。包含两类：
      1. 从未尝试过的（enriched = FALSE）—— 首次增强，两个源都跑；
      2. 已尝试过但仍缺 OpenAlex 数据、且发表在 7 天内的（enriched = TRUE
         AND citation_count IS NULL AND published 在窗口内）—— 只重试 OpenAlex。
    超过 7 天仍无 OpenAlex 数据的论文不再被选中（彻底放弃，不无限重试）。
    citation_count IS NULL 表示 OpenAlex 什么都没给；真实的 0 视为已拿到数据、不再重试。
    enriched 标志在此语义下 = “至少尝试过一次”，供上层决定是否还要跑 HF。
    """
    sql = """
        SELECT arxiv_id, title, enriched
        FROM papers
        WHERE enriched = FALSE
           OR (citation_count IS NULL
               AND published >= CURRENT_DATE - INTERVAL '7 days')
        ORDER BY enriched ASC, created_at ASC
        LIMIT %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def update_enriched(conn, arxiv_id, data):
    """
    用 COALESCE 只写入非 None 的字段：None 表示“未知”，保留列中已有值/默认值。
    最后无条件设 enriched=TRUE, enriched_at=NOW()。
    """
    sql = """
        UPDATE papers
        SET
            citation_count             = COALESCE(%s, citation_count),
            influential_citation_count = COALESCE(%s, influential_citation_count),
            reference_count            = COALESCE(%s, reference_count),
            year                       = COALESCE(%s, year),
            code_urls                  = COALESCE(%s, code_urls),
            hf_upvotes                 = COALESCE(%s, hf_upvotes),
            enriched             = TRUE,
            enriched_at          = NOW()
        WHERE arxiv_id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                data.get("citation_count"),
                data.get("influential_citation_count"),
                data.get("reference_count"),
                data.get("year"),
                data.get("code_urls"),
                data.get("hf_upvotes"),
                arxiv_id,
            ),
        )
    conn.commit()
