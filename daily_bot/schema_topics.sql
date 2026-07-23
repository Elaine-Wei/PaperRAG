-- 一次性【主题综述榜单】通用表 —— 与每日流水线(daily_*)和因子榜单(board_*)【完全隔离】。
-- 以 topic 作为命名空间，未来任意一次性主题综述都复用这三张表（不同 topic 值）。
-- 论文元数据仍可复用共享 papers 表（只读，不受影响）。

CREATE TABLE IF NOT EXISTS topic_paper (
    topic        TEXT NOT NULL,               -- 命名空间，如 'selfevo'
    arxiv_id     TEXT NOT NULL,
    is_seed      BOOLEAN DEFAULT FALSE,        -- 种子/锚点(按 ID 取+校验标题) → TRUE
    seed_name    TEXT,                         -- 种子/锚点名（普通候选为 NULL）
    source       TEXT,                         -- seed | anchor | keyword
    published    DATE,
    judge_keep   BOOLEAN,                      -- sol 判定 is_self_evolving_agent
    judge_reason TEXT,                         -- sol 判定理由（种子含 sanity 备注）
    importance   NUMERIC(4,1),                 -- sol 便宜「领域重要性」预排(1-10)，用于 shortlist
    importance_reason TEXT,
    shortlist    BOOLEAN DEFAULT FALSE,         -- 是否进入 full-score 短名单（锚点始终 full-score）
    added_at     TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (topic, arxiv_id)
);

CREATE TABLE IF NOT EXISTS topic_score (
    topic                   TEXT NOT NULL,
    arxiv_id                TEXT NOT NULL,
    freshness_score         NUMERIC(3,1),
    freshness_mode          TEXT DEFAULT 'steep',   -- 本主题统一 steep（无 relaxed）
    repro_score             NUMERIC(2,1),
    novelty_total           NUMERIC(2,1),
    paper_type              TEXT,
    domain_relevance_score  NUMERIC(2,1),
    authority_score         NUMERIC(2,1),
    authority_na            BOOLEAN,
    authority_venue         TEXT,
    composite_score         NUMERIC(3,1),
    composite_reason        TEXT,
    score_path              TEXT,
    study_path              TEXT,                    -- per-section 深读
    themed_path             TEXT,                    -- theme-block 概览版
    study_complete          BOOLEAN DEFAULT FALSE,
    scored_at               TIMESTAMP,
    PRIMARY KEY (topic, arxiv_id)
);

-- 主题榜单推送去重（与 daily / board 分开）
CREATE TABLE IF NOT EXISTS topic_push (
    key        TEXT PRIMARY KEY,               -- 'selfevo_2026-07-21' 等
    status     TEXT NOT NULL,
    pushed_at  TIMESTAMP DEFAULT NOW(),
    detail     TEXT
);
