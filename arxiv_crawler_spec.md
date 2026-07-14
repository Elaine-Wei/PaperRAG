# arXiv 论文爬虫 + 数据增强模块

## 目标

每天定时抓取 AI / Agent 方向最新论文，入库 PostgreSQL，并从三个补充来源异步填充 arXiv 没有的字段。

---

## 项目结构

```
crawler/
├── config.py          # 查询关键词、数据库连接、限速参数
├── arxiv_client.py    # arXiv API 请求与 XML 解析
├── enrichers.py       # Semantic Scholar / Papers with Code / HF Papers
├── database.py        # 数据库读写操作
├── pipeline.py        # 主流程：抓取 → 入库 → 增强
└── requirements.txt
```

---

## 数据库表

```sql
CREATE TABLE papers (
    id                    SERIAL PRIMARY KEY,
    arxiv_id              TEXT UNIQUE NOT NULL,
    title                 TEXT NOT NULL,
    abstract              TEXT,
    authors               TEXT[],
    categories            TEXT[],
    published             DATE,
    pdf_url               TEXT,

    -- Semantic Scholar
    citation_count        INTEGER,
    influential_citations INTEGER,
    s2_paper_id           TEXT,

    -- Papers with Code
    code_urls             TEXT[],
    github_stars          INTEGER,

    -- HF Papers
    hf_upvotes            INTEGER,

    -- 状态控制
    enriched              BOOLEAN DEFAULT FALSE,
    enriched_at           TIMESTAMP,
    created_at            TIMESTAMP DEFAULT NOW()
);
```

---

## config.py

```python
DATABASE_URL = "postgresql://user:password@localhost:5432/papers_db"

ARXIV_QUERIES = [
    'cat:cs.AI AND (ti:agent OR ti:"multi-agent" OR abs:agentic)',
    'cat:cs.CL AND (ti:"large language model" OR ti:LLM OR ti:RAG)',
    'cat:cs.LG AND ti:"reinforcement learning" AND (ti:agent OR ti:policy)',
]

DAILY_FETCH_LIMIT  = 100   # 每个查询每天最多抓多少篇
ARXIV_BATCH_SIZE   = 100   # 单次请求最大条数（arXiv 上限）
ARXIV_DELAY        = 3.0   # arXiv 要求请求间隔 ≥ 3 秒
ENRICHMENT_DELAY   = 1.0   # 增强请求间隔
ENRICHMENT_BATCH   = 50    # 每次增强处理多少篇
```

---

## arxiv_client.py

### 功能

- `fetch_arxiv(query, start, max_results)` — 单次 HTTP GET，返回解析后的论文列表
- `parse_arxiv_xml(xml_data)` — 解析 Atom XML，提取所有字段
- `fetch_all_recent(query, total)` — 分页抓取，内置限速 sleep
- `fetch_new_papers(query, total, seen_ids)` — 调用上一个函数，过滤已见过的 arxiv_id

### 关键细节

- 请求 URL：`http://export.arxiv.org/api/query`
- 参数：`search_query`, `start`, `max_results`, `sortBy=submittedDate`, `sortOrder=descending`
- XML 命名空间必须带：`{"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}`
- arxiv_id 从 `<id>` 标签提取，去掉版本号：`raw_id.split("/abs/")[-1].split("v")[0]`
- 每次翻页后 `time.sleep(ARXIV_DELAY)`

### 返回结构

每篇论文返回一个 dict：

```python
{
    "arxiv_id":   str,
    "title":      str,
    "abstract":   str,
    "authors":    list[str],
    "categories": list[str],
    "published":  str,   # YYYY-MM-DD
    "pdf_url":    str,
}
```

---

## enrichers.py

三个独立函数，任何一个失败只返回空 dict，不抛异常。

### `enrich_semantic_scholar(arxiv_id: str) -> dict`

- URL：`https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}`
- 请求字段：`citationCount,influentialCitationCount,paperId`
- 返回：`{"citation_count": int, "influential_citations": int, "s2_paper_id": str}`
- 404 表示论文太新尚未收录，返回空 dict，不报错

### `enrich_papers_with_code(arxiv_id: str) -> dict`

- URL：`https://paperswithcode.com/api/v1/papers/?arxiv_id={arxiv_id}`
- 遍历 `results[].repositories[]`，收集所有 `url`，取最大 `stars`
- 返回：`{"code_urls": list[str], "github_stars": int}`

### `enrich_hf_papers(arxiv_id: str) -> dict`

- URL：`https://huggingface.co/api/papers/{arxiv_id}`
- 返回：`{"hf_upvotes": int}`
- 404 表示该论文未被 HF 社区收录，返回空 dict

### `enrich_paper(arxiv_id: str) -> dict`

- 依次调用以上三个函数，合并结果
- 每次调用后 `time.sleep(ENRICHMENT_DELAY)`

---

## database.py

### 函数列表

- `get_connection()` — 从 `DATABASE_URL` 返回 psycopg2 连接
- `insert_paper(conn, paper: dict)` — INSERT，冲突（arxiv_id 重复）时忽略
- `get_unenriched(conn, limit: int) -> list[str]` — 查 `enriched=FALSE` 的 arxiv_id 列表
- `update_enriched(conn, arxiv_id: str, data: dict)` — UPDATE，用 `COALESCE` 只写入非 None 的字段，最后设 `enriched=TRUE`，`enriched_at=NOW()`

---

## pipeline.py

### `run_crawl()`

```
for query in ARXIV_QUERIES:
    papers = fetch_new_papers(query, total=DAILY_FETCH_LIMIT)
    for paper in papers:
        insert_paper(conn, paper)
    sleep(5)
```

### `run_enrichment()`

```
arxiv_ids = get_unenriched(conn, limit=ENRICHMENT_BATCH)
for arxiv_id in arxiv_ids:
    data = enrich_paper(arxiv_id)
    update_enriched(conn, arxiv_id, data)
    sleep(ENRICHMENT_DELAY)
```

### `main()`

顺序调用：`run_crawl()` → `run_enrichment()`，打印每步结果条数。

---

## requirements.txt

```
requests>=2.31.0
psycopg2-binary>=2.9.9
```

---

## 给 Claude Code 的执行指令

> 按照本文档实现 `crawler/` 目录下所有文件。
>
> 要求：
> 1. 严格按照文档中的函数签名和返回结构实现，不要自行增减参数
> 2. 所有外部请求加 `try/except`，失败时打印 warning 并返回空结果，不中断流程
> 3. 数据库操作用参数化查询（`%s` 占位符），不要拼接 SQL 字符串
> 4. `pipeline.py` 的 `main()` 可以直接 `python pipeline.py` 运行
> 5. 写一个 `README.md`，说明如何配置 `DATABASE_URL`、建表、跑第一次抓取
> 6. 不需要写测试文件
