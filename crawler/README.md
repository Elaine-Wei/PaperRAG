# arXiv 论文爬虫 + 数据增强模块

每天定时抓取 AI / Agent 方向最新论文，入库 PostgreSQL，并从两个补充来源
（Semantic Scholar / Hugging Face Papers）异步填充 arXiv 没有的字段。

## 目录结构

```
crawler/
├── config.py          # 查询关键词、数据库连接、限速参数
├── arxiv_client.py    # arXiv API 请求与 XML 解析
├── enrichers.py       # Semantic Scholar / HF Papers
├── database.py        # 数据库读写操作
├── pipeline.py        # 主流程：抓取 → 入库 → 增强
├── requirements.txt
└── README.md
```

## 1. 安装依赖

建议用虚拟环境：

```bash
cd crawler
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 配置 DATABASE_URL

程序从环境变量 `DATABASE_URL` 读取连接串，未设置时回退到
`postgresql://user:password@localhost:5432/papers_db`。

```bash
export DATABASE_URL="postgresql://<用户>:<密码>@<主机>:5432/<数据库>"
```

（可选）Semantic Scholar API key —— 设置后走认证请求，不设置则匿名 + 限速：

```bash
export S2_API_KEY="<你的-s2-key>"
```

## 3. 建库 + 建表

先创建数据库（如果还没有）：

```bash
createdb papers_db
```

再执行建表语句（用上面同一个 `DATABASE_URL` 指向的库）：

```bash
psql "$DATABASE_URL" <<'SQL'
CREATE TABLE papers (
    id                         SERIAL PRIMARY KEY,
    arxiv_id                   TEXT UNIQUE NOT NULL,
    title                      TEXT NOT NULL,
    abstract                   TEXT,
    authors                    TEXT[],
    categories                 TEXT[],
    published                  DATE,
    pdf_url                    TEXT,

    -- Semantic Scholar
    citation_count             INTEGER,
    influential_citation_count INTEGER,
    reference_count            INTEGER,
    year                       INTEGER,

    -- 代码实现链接（暂未填充，预留给后续从 Hugging Face 抓取）
    code_urls                  TEXT[],

    -- HF Papers
    hf_upvotes                 INTEGER,

    -- 状态控制
    enriched                   BOOLEAN DEFAULT FALSE,
    enriched_at                TIMESTAMP,
    created_at                 TIMESTAMP DEFAULT NOW()
);
SQL
```

> 所有增强字段（`citation_count` … `hf_upvotes`）均可空。增强器把“未知”报告为
> `NULL`，`update_enriched` 用 `COALESCE(%s, 原列)` 写入 —— `NULL` 表示“不覆盖，
> 保留原值”，因此 `citation_count = 0` 明确代表“Semantic Scholar 报告 0 次引用”，
> 与“尚不知道”（`NULL`）区分开。

## 4. 跑第一次抓取

```bash
python pipeline.py
```

它会依次：

1. `run_crawl` —— 遍历 `config.ARXIV_QUERIES` 的每个查询，抓取最新论文，
   跳过库里已有的 `arxiv_id`，入库；
2. `run_enrichment` —— 取一批 `enriched=FALSE` 的论文，依次请求两个来源
   （Semantic Scholar → Hugging Face，每次请求后间隔 `ENRICHMENT_DELAY`）补充字段，
   标记 `enriched=TRUE`。

每一步会打印命中/入库/增强的条数。

## 5. 定时运行

调度由外部负责（cron / GitHub Actions 等），本模块只提供可直接运行的
`pipeline.py`。例如每天 08:00 用 cron：

```cron
0 8 * * *  cd /path/to/crawler && /path/to/.venv/bin/python pipeline.py >> crawl.log 2>&1
```

## 配置参数（config.py）

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `DAILY_FETCH_LIMIT` | 每个查询每天最多抓多少篇 | 100 |
| `ARXIV_BATCH_SIZE` | 单次请求最大条数（arXiv 上限） | 100 |
| `ARXIV_DELAY` | arXiv 请求间隔（秒，≥ 3） | 3.0 |
| `ENRICHMENT_DELAY` | 增强请求间隔（秒，每次外部请求后 sleep） | 1.5 |
| `ENRICHMENT_BATCH` | 每次增强处理多少篇 | 50 |

## 备注

- 所有外部请求都包了 `try/except`：单个请求失败只打印 warning 并返回空结果，
  不会中断整个流程。
- 所有 SQL 都用参数化查询（`%s` 占位符）。
- 抓取去重：`pipeline` 先 `SELECT arxiv_id` 得到已见集合传给爬虫，入库时再用
  `ON CONFLICT (arxiv_id) DO NOTHING` 兜底。
- `code_urls` 列目前不写入、保持 `NULL`，预留给后续从 Hugging Face 抓取代码实现链接。
