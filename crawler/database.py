import psycopg2

from config import DATABASE_URL


def get_connection():
    """从 DATABASE_URL 返回 psycopg2 连接。"""
    return psycopg2.connect(DATABASE_URL)


def get_seen_ids(conn):
    """返回库中已有的全部 arxiv_id 集合，供抓取端去重。"""
    with conn.cursor() as cur:
        cur.execute("SELECT arxiv_id FROM papers;")
        return {row[0] for row in cur.fetchall()}


def insert_paper(conn, paper):
    """INSERT 一篇论文；arxiv_id 冲突时忽略。"""
    sql = """
        INSERT INTO papers (
            arxiv_id, title, abstract, authors, categories, published, pdf_url
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
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
            ),
        )
    conn.commit()


def get_unenriched(conn, limit):
    """查 enriched=FALSE 的 arxiv_id 列表。"""
    sql = """
        SELECT arxiv_id
        FROM papers
        WHERE enriched = FALSE
        ORDER BY created_at ASC
        LIMIT %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        return [row[0] for row in cur.fetchall()]


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
