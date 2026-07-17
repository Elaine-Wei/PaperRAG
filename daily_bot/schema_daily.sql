-- daily_bot 数据表（与 crawler 的 papers 表同库；均 FK 到 papers(arxiv_id)）
-- 幂等：全部 IF NOT EXISTS，可重复执行。不改动 papers 表结构。

-- 1) 每篇论文的流水线状态 + 甲/乙筛选结果 + 产物路径（一 arxiv_id 一行）
CREATE TABLE IF NOT EXISTS daily_paper (
    arxiv_id        TEXT PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    -- 甲/乙 筛选结果
    area            TEXT,        -- stage-B 判定：quant/ai4math/lob/hpc/agent | not_relevant | NULL(未筛)
    filter_reason   TEXT,        -- 一句话理由
    is_relevant     BOOLEAN,     -- 便捷标志（area 属于关注方向）；未筛时为 NULL
    -- 流水线阶段：时间戳非空即代表该阶段已完成（同时记录发生时间）
    fetched_at      TIMESTAMP DEFAULT NOW(),
    filtered_at     TIMESTAMP,
    digest_at       TIMESTAMP,
    deep_study_at   TIMESTAMP,
    scored_at       TIMESTAMP,
    pushed_at       TIMESTAMP,   -- 成功推送的镜像（权威记录在 daily_push）
    -- 产物路径
    digest_path     TEXT,
    deep_study_path TEXT,
    score_path      TEXT,
    updated_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_daily_paper_unfiltered
    ON daily_paper (filtered_at) WHERE filtered_at IS NULL;      -- 待筛队列
CREATE INDEX IF NOT EXISTS idx_daily_paper_digest
    ON daily_paper (is_relevant, digest_at);                     -- 待导读队列
CREATE INDEX IF NOT EXISTS idx_daily_paper_scored
    ON daily_paper (is_relevant, scored_at);                     -- 待打分队列

-- 2) 打分结果（最新一版，重打分做 UPSERT）
CREATE TABLE IF NOT EXISTS daily_score (
    arxiv_id             TEXT PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    freshness_score      NUMERIC(2,1),   -- 0.0–5.0（代码算）
    freshness_days       INTEGER,        -- 距发表天数，未知为 NULL
    freshness_label      TEXT,
    repro_score          NUMERIC(2,1),   -- 代码按子项加权算的总分 0–5
    repro_overall_reason TEXT,
    repro_subitems       JSONB,          -- {code_available:{score,reason,weight}, ...}
    novelty_score        SMALLINT,       -- 0–5
    novelty_reason       TEXT,
    domain               JSONB,          -- [{"area":"Quant","pct":60}, ...]
    model                TEXT,
    cross_checked        BOOLEAN DEFAULT FALSE,
    cross_notes          JSONB,          -- 采纳的 DeepSeek 复核意见（字符串数组）
    scored_at            TIMESTAMP DEFAULT NOW()
);

-- 3) 推送记录 + 去重（一篇最多成功推送一次）
CREATE TABLE IF NOT EXISTS daily_push (
    id         SERIAL PRIMARY KEY,
    arxiv_id   TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    status     TEXT NOT NULL,          -- 'success' | 'failed'
    pushed_at  TIMESTAMP DEFAULT NOW(),
    detail     TEXT                    -- WeCom 消息 id / 错误信息
);
-- 部分唯一索引：同一 arxiv_id 只能有一条 success → 推过一次就不会再推
CREATE UNIQUE INDEX IF NOT EXISTS uniq_daily_push_success
    ON daily_push (arxiv_id) WHERE status = 'success';
CREATE INDEX IF NOT EXISTS idx_daily_push_arxiv
    ON daily_push (arxiv_id, status);

-- 新颖度升级后追加的列（幂等）：类型自适应的总分/类型/子项
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS novelty_total NUMERIC(2,1);
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS paper_type TEXT;
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS novelty_subitems JSONB;

-- arXiv 作者自填的发表信号（幂等，可空）：权威性/发表质量维度的最直接来源
--   arxiv_comment：<arxiv:comment>，常含 "Accepted at NeurIPS 2025" / "Under review" 等
--   journal_ref：<arxiv:journal_ref>，已正式发表才有
ALTER TABLE papers ADD COLUMN IF NOT EXISTS arxiv_comment TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS journal_ref TEXT;

-- 两个新的雷达维度（幂等，可空）：
--   领域相关性 domain_relevance —— LLM 按我们关注方向打 0-5（与 domain 占比构成不同）
--   权威性 authority —— LLM 依据全部作者+机构+发表信息打 0-5；信息不足 authority_na=TRUE（不评分，非 0）
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS domain_relevance_score NUMERIC(2,1);
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS domain_relevance_reason TEXT;
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS authority_score NUMERIC(2,1);
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS authority_reason TEXT;
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS authority_institutions JSONB;
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS authority_venue TEXT;
ALTER TABLE daily_score ADD COLUMN IF NOT EXISTS authority_na BOOLEAN;
