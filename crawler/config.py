import os

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

# Semantic Scholar API key（可选）：设置了就走认证请求，否则匿名 + 限速
S2_API_KEY = os.environ.get("S2_API_KEY")
