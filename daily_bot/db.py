#!/usr/bin/env python3
"""
db —— daily_bot 的数据库层（Supabase / 同一套 crawler 的 PostgreSQL）。

- 连接：复用 crawler.database.get_connection（稳健解析 DATABASE_URL + 远程库自动 SSL）。
- papers：复用 crawler.database.insert_paper（ON CONFLICT DO NOTHING，只写 arXiv 原生列）。
- daily_paper / daily_score / daily_push：本模块的增删查（见 schema_daily.sql）。

设计要点：以 arxiv_id 为主键/外键；打分最新一版（UPSERT）；推送按 arxiv_id 去重（推过不再推）。
"""

import json
import os
import sys

import psycopg2.extras

# 复用 crawler 的连接与 papers 写入（daily_bot 依赖 crawler，不反向依赖）
_HERE = os.path.dirname(os.path.abspath(__file__))
_CRAWLER = os.path.join(os.path.dirname(_HERE), "crawler")
if _CRAWLER not in sys.path:
    sys.path.insert(0, _CRAWLER)
from config import DATABASE_URL            # noqa: E402  (触发 .env 加载)
from database import get_connection, insert_paper  # noqa: E402  (稳健连接 + 幂等写 papers)

SCHEMA_SQL = os.path.join(_HERE, "schema_daily.sql")
CRAWLER_SCHEMA_SQL = os.path.join(_CRAWLER, "schema.sql")


def ensure(conn):
    """
    返回一个可用连接：ping（SELECT 1），若已被服务器/连接池断开则重连并返回新连接。
    用于「长时间 LLM 调用后再写库」的场景——Supabase pooler 会关闭空闲连接，
    握着同一条连接跨越数分钟 LLM 调用后再写会触发 server closed connection。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return get_connection()


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def ensure_schema(conn):
    """确保 papers（若缺则按 crawler/schema.sql 建）+ 三张 daily_ 表就位。幂等。"""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.papers');")
        if cur.fetchone()[0] is None:
            with open(CRAWLER_SCHEMA_SQL, "r", encoding="utf-8") as f:
                cur.execute(f.read())
            print("[db] 已按 crawler/schema.sql 创建 papers 表")
        else:
            print("[db] papers 表已存在，跳过")
        with open(SCHEMA_SQL, "r", encoding="utf-8") as f:
            cur.execute(f.read())
        print("[db] 已执行 schema_daily.sql（daily_paper / daily_score / daily_push）")
    conn.commit()


# ---------------------------------------------------------------------------
# papers + daily_paper 写入
# ---------------------------------------------------------------------------

def upsert_paper(conn, paper):
    """把一篇论文写进共享 papers 表（只写 arXiv 原生列；冲突忽略）。复用 crawler.insert_paper。"""
    insert_paper(conn, paper)  # 内部已 commit


def upsert_daily_paper(conn, arxiv_id):
    """登记一篇进入 daily_bot 流水线（已存在则忽略）。返回是否为新登记。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO daily_paper (arxiv_id) VALUES (%s) "
            "ON CONFLICT (arxiv_id) DO NOTHING;", (arxiv_id,))
        new = cur.rowcount == 1
    conn.commit()
    return new


def set_filter_result(conn, arxiv_id, area, reason, is_relevant):
    """写入甲/乙筛选结果，标记 filtered_at。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE daily_paper SET area=%s, filter_reason=%s, is_relevant=%s, "
            "filtered_at=NOW(), updated_at=NOW() WHERE arxiv_id=%s;",
            (area, reason, is_relevant, arxiv_id))
    conn.commit()


_STAGE_COLS = {
    "digest": ("digest_at", "digest_path"),
    "deep_study": ("deep_study_at", "deep_study_path"),
    "score": ("scored_at", "score_path"),
}


def mark_stage(conn, arxiv_id, stage, path=None):
    """标记某产物阶段完成（digest / deep_study / score），可附带产物路径。"""
    if stage not in _STAGE_COLS:
        raise ValueError(f"未知 stage: {stage}")
    at_col, path_col = _STAGE_COLS[stage]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE daily_paper SET {at_col}=NOW(), {path_col}=COALESCE(%s, {path_col}), "
            f"updated_at=NOW() WHERE arxiv_id=%s;", (path, arxiv_id))
    conn.commit()


# ---------------------------------------------------------------------------
# daily_score 写入（UPSERT，最新一版）
# ---------------------------------------------------------------------------

def upsert_score(conn, arxiv_id, s):
    """
    写入/更新打分。s 为 dict：
      freshness_score, freshness_days, freshness_label,
      repro_score, repro_overall_reason, repro_subitems(dict),
      novelty_score, novelty_reason, domain(list), model, cross_checked(bool), cross_notes(list)
    """
    J = psycopg2.extras.Json
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_score (
                arxiv_id, freshness_score, freshness_days, freshness_label,
                repro_score, repro_overall_reason, repro_subitems,
                novelty_score, novelty_reason, novelty_total, paper_type, novelty_subitems,
                domain, model, cross_checked, cross_notes,
                domain_relevance_score, domain_relevance_reason,
                authority_score, authority_reason, authority_institutions,
                authority_venue, authority_na, scored_at
            ) VALUES (%s,%s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,
                      %s,%s, %s,%s,%s,%s,%s, NOW())
            ON CONFLICT (arxiv_id) DO UPDATE SET
                freshness_score=EXCLUDED.freshness_score,
                freshness_days=EXCLUDED.freshness_days,
                freshness_label=EXCLUDED.freshness_label,
                repro_score=EXCLUDED.repro_score,
                repro_overall_reason=EXCLUDED.repro_overall_reason,
                repro_subitems=EXCLUDED.repro_subitems,
                novelty_score=EXCLUDED.novelty_score,
                novelty_reason=EXCLUDED.novelty_reason,
                novelty_total=EXCLUDED.novelty_total,
                paper_type=EXCLUDED.paper_type,
                novelty_subitems=EXCLUDED.novelty_subitems,
                domain=EXCLUDED.domain,
                model=EXCLUDED.model,
                cross_checked=EXCLUDED.cross_checked,
                cross_notes=EXCLUDED.cross_notes,
                domain_relevance_score=EXCLUDED.domain_relevance_score,
                domain_relevance_reason=EXCLUDED.domain_relevance_reason,
                authority_score=EXCLUDED.authority_score,
                authority_reason=EXCLUDED.authority_reason,
                authority_institutions=EXCLUDED.authority_institutions,
                authority_venue=EXCLUDED.authority_venue,
                authority_na=EXCLUDED.authority_na,
                scored_at=NOW();
            """,
            (arxiv_id, s.get("freshness_score"), s.get("freshness_days"),
             s.get("freshness_label"), s.get("repro_score"), s.get("repro_overall_reason"),
             J(s.get("repro_subitems") or {}), s.get("novelty_score"),
             s.get("novelty_reason"), s.get("novelty_total"), s.get("paper_type"),
             J(s.get("novelty_subitems") or {}),
             J(s.get("domain") or []), s.get("model"),
             bool(s.get("cross_checked")), J(s.get("cross_notes") or []),
             s.get("domain_relevance_score"), s.get("domain_relevance_reason"),
             s.get("authority_score"), s.get("authority_reason"),
             J(s.get("authority_institutions") or []), s.get("authority_venue"),
             bool(s.get("authority_na"))))
    conn.commit()
    # scored_at 由调用方 mark_stage(..., "score", path) 设置（带产物路径）


# ---------------------------------------------------------------------------
# 队列查询（"哪些还没处理 / 没推送"）
# ---------------------------------------------------------------------------

def get_papers(conn, arxiv_ids):
    """按 arxiv_id 批量取 papers 元数据（供筛选/导读用）。published 转成字符串。"""
    if not arxiv_ids:
        return []
    cols = ["arxiv_id", "title", "abstract", "authors", "categories", "published", "pdf_url"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT arxiv_id, title, abstract, authors, categories, published, pdf_url "
            "FROM papers WHERE arxiv_id = ANY(%s);", (list(arxiv_ids),))
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["published"] = d["published"].isoformat() if d["published"] else ""
            out.append(d)
        return out


def get_unfiltered(conn, limit=500):
    with conn.cursor() as cur:
        cur.execute("SELECT arxiv_id FROM daily_paper WHERE filtered_at IS NULL "
                    "ORDER BY fetched_at LIMIT %s;", (limit,))
        return [r[0] for r in cur.fetchall()]


def get_undigested(conn, limit=500):
    with conn.cursor() as cur:
        cur.execute("SELECT arxiv_id FROM daily_paper "
                    "WHERE is_relevant IS TRUE AND digest_at IS NULL "
                    "ORDER BY fetched_at LIMIT %s;", (limit,))
        return [r[0] for r in cur.fetchall()]


def get_undigested_round_robin(conn, area_order, limit=500):
    """
    未导读的相关论文，按【方向轮转】取：每个方向排一队（队内 oldest-first），
    然后一轮一个地跨方向交替（第 1 名各方向各取一篇，再第 2 名……），避免大方向（agent）
    把小方向（lob/ai4math）挤到很后面。area_order 决定同一轮内的方向先后。
    返回 [(arxiv_id, area), ...]。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT arxiv_id, area FROM (
                SELECT arxiv_id, area,
                       row_number() OVER (PARTITION BY area
                                          ORDER BY fetched_at, arxiv_id) AS rn
                FROM daily_paper
                WHERE is_relevant IS TRUE AND digest_at IS NULL
            ) t
            ORDER BY rn, array_position(%s::text[], area), arxiv_id
            LIMIT %s;
            """, (list(area_order), limit))
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_unscored(conn, limit=500):
    with conn.cursor() as cur:
        cur.execute("SELECT arxiv_id FROM daily_paper "
                    "WHERE is_relevant IS TRUE AND scored_at IS NULL "
                    "ORDER BY fetched_at LIMIT %s;", (limit,))
        return [r[0] for r in cur.fetchall()]


def get_scorable(conn, limit=500):
    """相关 + 已导读 + 尚未打分的论文（打分阶段的 backlog）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT arxiv_id FROM daily_paper "
                    "WHERE is_relevant IS TRUE AND digest_at IS NOT NULL AND scored_at IS NULL "
                    "ORDER BY fetched_at LIMIT %s;", (limit,))
        return [r[0] for r in cur.fetchall()]


def get_unstudied_round_robin(conn, area_order, limit=500):
    """相关 + 已导读 + 尚未做 deep-study 的论文，按方向轮转取（同 get_undigested_round_robin）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT arxiv_id, area FROM (
                SELECT arxiv_id, area,
                       row_number() OVER (PARTITION BY area
                                          ORDER BY fetched_at, arxiv_id) AS rn
                FROM daily_paper
                WHERE is_relevant IS TRUE AND digest_at IS NOT NULL AND deep_study_at IS NULL
            ) t
            ORDER BY rn, array_position(%s::text[], area), arxiv_id
            LIMIT %s;
            """, (list(area_order), limit))
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_pushable(conn, limit=500):
    """相关 + 已生成导读 + 尚未成功推送过的论文（避免重复推送）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dp.arxiv_id
            FROM daily_paper dp
            WHERE dp.is_relevant IS TRUE AND dp.digest_at IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM daily_push p
                  WHERE p.arxiv_id = dp.arxiv_id AND p.status = 'success')
            ORDER BY dp.fetched_at LIMIT %s;
            """, (limit,))
        return [r[0] for r in cur.fetchall()]


def get_push_batch(conn, area_order, limit=500):
    """
    本轮「推送批次」：相关 + 尚未成功推送过的论文，按【最新优先 + 方向轮转】取 limit 篇。
    与 get_pushable 不同——这里 freshest-first（按 published 降序）、跨方向轮转，
    且不要求已导读（导读/打分/精读会对这一批按需补齐）。用于让三件产物围绕同一批最新论文。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT arxiv_id, area FROM (
                SELECT dp.arxiv_id AS arxiv_id, dp.area AS area,
                       row_number() OVER (PARTITION BY dp.area
                            ORDER BY pa.published DESC NULLS LAST, dp.arxiv_id DESC) AS rn
                FROM daily_paper dp JOIN papers pa ON pa.arxiv_id = dp.arxiv_id
                WHERE dp.is_relevant IS TRUE
                  AND NOT EXISTS (SELECT 1 FROM daily_push p
                                  WHERE p.arxiv_id = dp.arxiv_id AND p.status = 'success')
            ) t
            ORDER BY rn, array_position(%s::text[], area), arxiv_id
            LIMIT %s;
            """, (list(area_order), limit))
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_stage_status(conn, arxiv_id):
    """返回 {digest, scored, studied} 三个布尔：各阶段是否已完成（对应 *_at 是否非空）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT digest_at IS NOT NULL, scored_at IS NOT NULL, "
                    "deep_study_at IS NOT NULL FROM daily_paper WHERE arxiv_id=%s;",
                    (arxiv_id,))
        r = cur.fetchone()
    if not r:
        return {"digest": False, "scored": False, "studied": False}
    return {"digest": bool(r[0]), "scored": bool(r[1]), "studied": bool(r[2])}


def get_daily_row(conn, arxiv_id):
    """取 daily_paper 一行（area / 各产物路径），供组装用。"""
    cols = ["arxiv_id", "area", "is_relevant", "digest_path", "deep_study_path",
            "score_path"]
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM daily_paper WHERE arxiv_id=%s;",
                    (arxiv_id,))
        r = cur.fetchone()
        return dict(zip(cols, r)) if r else None


def get_score(conn, arxiv_id):
    """取 daily_score 一行（供推送消息用；跨轮次也能读回）。"""
    cols = ["freshness_score", "freshness_label", "repro_score", "novelty_total",
            "paper_type", "domain",
            "domain_relevance_score", "authority_score", "authority_na",
            "authority_institutions", "authority_venue"]
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM daily_score WHERE arxiv_id=%s;",
                    (arxiv_id,))
        r = cur.fetchone()
        return dict(zip(cols, r)) if r else None


# ---------------------------------------------------------------------------
# 推送去重 / 记录
# ---------------------------------------------------------------------------

def already_pushed(conn, arxiv_id):
    """该论文是否已成功推送过（去重检查）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM daily_push WHERE arxiv_id=%s AND status='success' "
                    "LIMIT 1;", (arxiv_id,))
        return cur.fetchone() is not None


def record_push(conn, arxiv_id, status, detail=None):
    """
    记录一次推送。status='success'|'failed'。
    成功记录受部分唯一索引保护：重复成功会被数据库拒绝（IntegrityError），
    从而在数据库层面兜底"推一次"。返回是否新插入了 success 记录。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO daily_push (arxiv_id, status, detail) "
                        "VALUES (%s,%s,%s);", (arxiv_id, status, detail))
            if status == "success":
                cur.execute("UPDATE daily_paper SET pushed_at=NOW(), updated_at=NOW() "
                            "WHERE arxiv_id=%s;", (arxiv_id,))
        conn.commit()
        return True
    except psycopg2.errors.UniqueViolation:
        conn.rollback()  # 已推送过，唯一索引拒绝——静默视为已完成
        return False


if __name__ == "__main__":
    # 直接运行 = 迁移 + 连通性/往返自检
    conn = get_connection()
    print("[db] 已连接")
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM daily_paper;")
        print("[db] daily_paper 行数:", cur.fetchone()[0])
    conn.close()
