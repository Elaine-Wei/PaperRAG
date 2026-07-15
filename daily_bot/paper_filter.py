"""
paper_filter —— daily_bot 的论文筛选（版本 甲）

只用「已在内存里的信号」筛选：arXiv 分类、标题/摘要关键词、recency。
不涉及 enrichment / HF upvotes / code links / OpenAlex / 数据库（留给后续版本）。

想调整关注方向 / 抓取范围 / 选取上限，改本文件顶部的配置即可。
"""

import os
import re
import sys

# ---------------------------------------------------------------------------
# 配置区（易改）
# ---------------------------------------------------------------------------

# 广义分类：太大，单靠它命中会把整个 cs.LG/cs.AI/cs.CL 都算进来 → 不能仅凭它打标，
# 必须叠加关键词。其余（q-fin.*、cs.MA、cs.DC、cs.PF、math.OC）视为“专指分类”，命中即算。
BROAD_CATEGORIES = {"cs.LG", "cs.AI", "cs.CL"}

# A 类关注方向（团队核心兴趣）。
# 匹配规则（见 match_signals）：命中 = 专指分类命中 OR 关键词命中（标题+摘要，大小写不敏感）。
#   - 专指分类（不在 BROAD_CATEGORIES 里的）单独命中即可。
#   - 广义分类（cs.LG/cs.AI/cs.CL）单独不算；但关键词命中与分类无关，
#     所以一篇发在 cs.LG 的 AI-for-quant / AI-for-math 仍能靠强关键词被捞到（保召回）。
# 关键词力求“专指但不过窄”：宁可放进一点可肉眼剔除的噪声，也不要漏掉真正相关的论文。
FOCUS_AREAS = {
    "quant": {
        # q-fin.* 都是专指分类，命中即算。关键词收紧了 factor/alpha/portfolio 这类泛词，
        # 但保留足够强的量化词，让发在 cs.LG 的 AI-for-quant 也能被捞到。
        "categories": ["q-fin.TR", "q-fin.PM", "q-fin.CP", "q-fin.ST"],
        "keywords": ["quantitative trading", "statistical arbitrage", "trading strategy",
                     "market making", "backtest", "alpha factor", "factor model",
                     "portfolio optimization", "portfolio management", "asset pricing",
                     "order execution", "market impact", "volatility forecasting"],
    },
    "ai4math": {
        # math.OC 专指（命中即算）；cs.LG/cs.AI 广义（需关键词）。关键词覆盖形式化/定理证明/
        # 数学推理等真正的 AI-for-math 方向，保持对 cs.LG 论文的召回。
        "categories": ["math.OC", "cs.LG", "cs.AI"],
        "keywords": ["AI for math", "theorem proving", "automated theorem proving",
                     "formal proof", "proof assistant", "autoformalization",
                     "symbolic reasoning", "mathematical reasoning", "math reasoning",
                     "mathematical problem", "formal mathematics", "olympiad",
                     "competition math"],
    },
    "lob": {
        "categories": ["q-fin.TR"],
        "keywords": ["limit order book", "LOB", "matching engine", "order book",
                     "microstructure", "market microstructure", "order flow"],
    },
    "hpc": {
        # cs.DC/cs.PF 专指，命中即算；关键词再补一些系统性能相关词。
        "categories": ["cs.DC", "cs.PF"],
        "keywords": ["high-frequency", "low-latency", "high-performance", "ZeroMQ",
                     "DAG scheduling", "distributed training", "inference serving",
                     "kernel optimization"],
    },
    "agent": {
        # cs.MA 专指（多智能体系统），命中即算；cs.AI/cs.CL 广义需关键词。
        # 关键词保持宽松，让真正的 agent/LLM 论文（多在 cs.AI/cs.CL）都能被捞到。
        "categories": ["cs.AI", "cs.CL", "cs.MA"],
        "keywords": ["agent", "multi-agent", "agentic", "LLM", "large language model",
                     "RAG", "retrieval-augmented", "tool use", "tool-calling",
                     "function calling"],
    },
}

# A 类方向的处理/汇报顺序（越靠前优先级越高，用于占用全局上限时的先后）
A_CLASS_ORDER = ["quant", "ai4math", "lob", "hpc", "agent"]

# 选取上限
MAX_PER_AREA = 2      # 每个方向最多留几篇
MAX_A_CLASS = 5       # A 类总上限
MAX_B_CLASS = 2       # B 类兜底最多几篇
DAILY_TARGET_MAX = 6  # 每天总量大致上限（A + B）

# 抓取范围（版本 甲 拓宽）：复用 crawler/config.py 里已有的 agent 查询，
# 再补上 q-fin.* / cs.DC / cs.PF / math.OC，让 quant/lob/hpc/math 方向也有候选。
_HERE = os.path.dirname(os.path.abspath(__file__))
_CRAWLER_DIR = os.path.join(os.path.dirname(_HERE), "crawler")
if _CRAWLER_DIR not in sys.path:
    sys.path.insert(0, _CRAWLER_DIR)
try:
    from config import ARXIV_QUERIES as _BASE_QUERIES
except Exception as e:  # pragma: no cover - 导入失败也能跑
    print(f"[WARN] paper_filter: 无法导入 crawler/config.py 的 ARXIV_QUERIES: {e}")
    _BASE_QUERIES = [
        'cat:cs.AI AND (ti:agent OR ti:"multi-agent" OR abs:agentic)',
    ]

# 按分类拓宽的补充查询（arXiv API 语法；sortBy=submittedDate 由抓取端负责）
_EXTRA_QUERIES = [
    "cat:q-fin.TR OR cat:q-fin.PM OR cat:q-fin.CP OR cat:q-fin.ST",  # quant / lob
    "cat:cs.DC OR cat:cs.PF",                                        # hpc
    "cat:math.OC",                                                   # ai4math
]

FETCH_QUERIES = list(_BASE_QUERIES) + _EXTRA_QUERIES


# ---------------------------------------------------------------------------
# 关键词匹配（词边界，避免 "LOB" 命中 "global" 这类子串误伤）
# ---------------------------------------------------------------------------

def _compile_keyword_patterns(keywords):
    """每个关键词单独编译（带词边界），以便命中时知道是哪个词。"""
    return [(k, re.compile(r"\b" + re.escape(k.lower()) + r"\b")) for k in keywords]


_AREA_KW = {area: _compile_keyword_patterns(cfg["keywords"])
            for area, cfg in FOCUS_AREAS.items()}
# 每个方向的“专指分类”（广义分类不单独计入）
_AREA_SPECIFIC_CATS = {
    area: [c for c in cfg["categories"] if c not in BROAD_CATEGORIES]
    for area, cfg in FOCUS_AREAS.items()
}


def _paper_text(paper):
    return ((paper.get("title") or "") + " " + (paper.get("abstract") or "")).lower()


def match_signals(paper):
    """
    返回 {area: signal} —— 该论文命中的方向及“为何命中”的可读信号。
    signal 形如 'cat:q-fin.TR'、'kw:market making' 或 'cat:cs.MA+kw:agent'。
    命中 = 专指分类命中 OR 关键词命中；广义分类（cs.LG/cs.AI/cs.CL）单独不算。
    """
    cats = set(paper.get("categories") or [])
    text = _paper_text(paper)
    out = {}
    for area in FOCUS_AREAS:
        cat_hit = next((c for c in _AREA_SPECIFIC_CATS[area] if c in cats), None)
        kw_hit = next((k for k, pat in _AREA_KW[area] if pat.search(text)), None)
        if cat_hit and kw_hit:
            out[area] = f"cat:{cat_hit}+kw:{kw_hit}"
        elif cat_hit:
            out[area] = f"cat:{cat_hit}"
        elif kw_hit:
            out[area] = f"kw:{kw_hit}"
    return out


def matched_areas(paper):
    """命中的方向名列表（match_signals 的键）。"""
    return list(match_signals(paper).keys())


# ---------------------------------------------------------------------------
# Step 3 + 4：粗筛（打标 + 分池） → 细筛（按方向、按 recency 选取）
# ---------------------------------------------------------------------------

def _recency_key(paper):
    return paper.get("published") or ""


def select(papers):
    """
    对已抓取的论文做筛选，返回一个 report dict：
        {
          "area_reports": [{"area","matched","picked","note"}...],
          "b_fallback":   [paper...],
          "selected":     [paper...],   # 去重后 A+B 的最终列表
          "a_count":      int,
        }
    每篇论文会被就地打上 "areas" 标签（命中的方向列表）。
    """
    for p in papers:
        p["area_signals"] = match_signals(p)
        p["areas"] = list(p["area_signals"].keys())

    a_pool = [p for p in papers if p["areas"]]       # 命中 ≥1 A 类方向
    b_pool = [p for p in papers if not p["areas"]]   # 未命中任何 A 类 → B 类兜底

    selected_ids = set()
    selected = []

    # 每方向的候选排序：先按 recency 新→旧，再把「专指分类命中」排到「仅关键词命中」之前
    # （分类命中是更强的信号，避免像 "high-frequency" 这种松散关键词的误报占掉该方向的名额）。
    # 两次 sort 都稳定，故同一档内仍保持 recency 顺序。不丢任何候选，只是重排。
    def _cat_first(area, p):
        return 0 if p["area_signals"].get(area, "").startswith("cat") else 1

    area_matched = {}
    for area in A_CLASS_ORDER:
        cand = [p for p in a_pool if area in p["areas"]]
        cand.sort(key=_recency_key, reverse=True)
        cand.sort(key=lambda p: _cat_first(area, p))
        area_matched[area] = cand
    area_picked = {area: [] for area in A_CLASS_ORDER}

    def _take_one(area):
        """给某方向选 1 篇尚未被选的最新论文；成功返回 True。"""
        if len(selected) >= MAX_A_CLASS or len(area_picked[area]) >= MAX_PER_AREA:
            return False
        for p in area_matched[area]:
            if p["arxiv_id"] not in selected_ids:
                selected_ids.add(p["arxiv_id"])
                selected.append(p)
                area_picked[area].append(p)
                return True
        return False

    # Round 1：每个非空方向先各取最新 1 篇（保证 agent/hpc 等不被前面的方向饿死）
    for area in A_CLASS_ORDER:
        _take_one(area)
    # Round 2：若仍有名额，按优先级给各方向补第 2 篇
    for area in A_CLASS_ORDER:
        if len(selected) >= MAX_A_CLASS:
            break
        _take_one(area)

    area_reports = []
    for area in A_CLASS_ORDER:
        matched = area_matched[area]
        picked = area_picked[area]
        if not matched:
            note = "今日无新论文 / no new papers today"
        elif not picked:
            note = "匹配到但已达上限 / matched but cap reached"
        else:
            note = ""
        area_reports.append({"area": area, "matched": len(matched),
                             "picked": picked, "note": note})

    a_count = len(selected)

    # B 类兜底：保证 digest 不空。版本 甲 只按 recency 取最新；
    # TODO(后续版本)：接入 enrichment 后，这里改用 HF 热度 / 引用等信号，而不仅是最新。
    b_fallback = []
    b_quota = max(0, min(MAX_B_CLASS, DAILY_TARGET_MAX - len(selected)))
    for p in sorted(b_pool, key=_recency_key, reverse=True):
        if len(b_fallback) >= b_quota:
            break
        if p["arxiv_id"] in selected_ids:
            continue
        selected_ids.add(p["arxiv_id"])
        b_fallback.append(p)
        selected.append(p)

    return {
        "area_reports": area_reports,
        "b_fallback": b_fallback,
        "selected": selected,
        "a_count": a_count,
    }


def format_report(report, fetched_count=None):
    """把 select() 的结果格式化成可读的选择报告字符串。"""
    lines = ["== 选择报告 / Selection report =="]
    if fetched_count is not None:
        lines.append(f"抓取候选 {fetched_count} 篇（去重后）")
    lines.append("A 类（团队核心方向）：")
    for ar in report["area_reports"]:
        area = ar["area"]
        if ar["matched"] == 0:
            lines.append(f"  [{area}] 今日无新论文 / no new papers today")
        elif ar["picked"]:
            lines.append(f"  [{area}] 命中 {ar['matched']} 篇，选中 {len(ar['picked'])} 篇：")
            for p in ar["picked"]:
                sig = (p.get("area_signals") or {}).get(area, "?")
                lines.append(f"      · {p['arxiv_id']} ({p.get('published')}) "
                             f"[{sig}] — {p.get('title')}")
        else:
            lines.append(f"  [{area}] 命中 {ar['matched']} 篇，但{ar['note']}")

    lines.append("B 类兜底（general AI，按最新）：")
    if report["b_fallback"]:
        for p in report["b_fallback"]:
            lines.append(f"      · {p['arxiv_id']} ({p.get('published')}) — {p.get('title')}")
    else:
        lines.append("      （无 / none）")

    lines.append(f"合计选中 {len(report['selected'])} 篇"
                 f"（A 类 {report['a_count']} + B 类 {len(report['b_fallback'])}）")
    return "\n".join(lines)
