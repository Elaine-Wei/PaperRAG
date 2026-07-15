import os


def _load_env_file(path):
    """
    简单手动解析 crawler/.env（无需 python-dotenv），与 daily_bot 用的是同一套模式。
    每行 KEY=VALUE，忽略空行与 # 注释，去掉可选引号。
    用 setdefault：shell 中已导出的变量优先，不会被 .env 覆盖。
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except Exception as e:
        print(f"[WARN] 读取 .env 失败 ({path}): {e}")


# 在读取任何环境变量前，先加载 crawler/.env
_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# 数据库连接：优先读环境变量，未设置时回退到本地默认值
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://user:password@localhost:5432/papers_db",
)

ARXIV_QUERIES = [
    'cat:cs.AI AND (ti:agent OR ti:"multi-agent" OR abs:agentic)',
    'cat:cs.CL AND (ti:"large language model" OR ti:LLM OR ti:RAG)',
    'cat:cs.LG AND ti:"reinforcement learning" AND (ti:agent OR ti:policy)',
]

DAILY_FETCH_LIMIT = 100   # 每个查询每天最多抓多少篇
ARXIV_BATCH_SIZE  = 100   # 单次请求最大条数（arXiv 上限）
ARXIV_DELAY       = 3.0   # arXiv 要求请求间隔 ≥ 3 秒
ENRICHMENT_DELAY  = 1.5   # 增强请求间隔（每次外部请求后都 sleep 这么久）
ENRICHMENT_BATCH  = 50    # 每次增强处理多少篇

# OpenAlex（替代原 Semantic Scholar）。都从 env 读取（可放在 crawler/.env）。
# 每个请求都要带上 api_key 和 mailto（见 enrichers.py）。二者都可选，但没有 key
# OpenAlex 现在会限流/拒绝，建议配置。
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY")
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO")
