-- 主题榜单（一次性）专用表 —— 与每日流水线【完全隔离】：绝不写 daily_paper/daily_score/daily_push。
-- 论文元数据仍可复用共享 papers 表（滚动窗口只读 daily_paper，不受影响）。

CREATE TABLE IF NOT EXISTS board_paper (
    arxiv_id   TEXT NOT NULL,
    board      TEXT NOT NULL,              -- 'A'(传统) | 'B'(LLM)
    section    TEXT NOT NULL,              -- 'classics' | 'latest'
    is_seed    BOOLEAN DEFAULT FALSE,
    seed_name  TEXT,
    source     TEXT,                       -- seed | keyword | broad
    published  DATE,
    clf_reason TEXT,                       -- LLM 分类理由（LLM-or-not）
    clf_board  TEXT,                       -- 分类器判定的 board（sanity；种子以人工为准）
    added_at   TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (arxiv_id, board)
);

CREATE TABLE IF NOT EXISTS board_score (
    arxiv_id                TEXT NOT NULL,
    board                   TEXT NOT NULL,
    freshness_score         NUMERIC(3,1),
    freshness_mode          TEXT,          -- 'steep' | 'relaxed'（经典区用 relaxed）
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
    study_path              TEXT,          -- per-section 深读
    themed_path             TEXT,          -- theme-block 概览版
    study_complete          BOOLEAN DEFAULT FALSE,
    scored_at               TIMESTAMP,
    PRIMARY KEY (arxiv_id, board)
);

-- 榜单推送去重（与 daily_top30_push 分开）
CREATE TABLE IF NOT EXISTS board_push (
    key        TEXT PRIMARY KEY,           -- 'boardA_2026-07-21' 等
    status     TEXT NOT NULL,
    pushed_at  TIMESTAMP DEFAULT NOW(),
    detail     TEXT
);
