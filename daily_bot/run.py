#!/usr/bin/env python3
"""
Daily paper digest bot — v0

端到端最小闭环：fetch → pick → 中文导读（relay LLM）→ 套用 skill 风格生成 HTML → 落地本地文件。
不涉及调度 / 数据库 / 推送 / 复杂筛选。仅用标准库（urllib），无需 pip 安装。

运行： python daily_bot/run.py
"""

import datetime
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# 路径 / 配置
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
CRAWLER_DIR = os.path.join(PROJECT_ROOT, "crawler")
CSS_PATH = os.path.join(
    PROJECT_ROOT,
    "academic-html-skill",
    "unpacked",
    "academic-html-article",
    "references",
    "full-css.css",
)
OUTPUT_DIR = os.path.join(HERE, "output")


def load_env_file(path=None):
    """
    简单手动解析 daily_bot/.env（无需 python-dotenv）。
    每行 KEY=VALUE，忽略空行与 # 注释，去掉可选的引号。
    用 setdefault：已在 shell 中导出的变量优先，不会被 .env 覆盖。
    """
    path = path or os.path.join(HERE, ".env")
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


load_env_file()

# 筛选模块：甲（关键词粗召回）、乙（stage-B AI 分类）、共享 relay 调用、DB 层
sys.path.insert(0, CRAWLER_DIR)  # 便于 paper_filter 复用 crawler/config.py
import paper_filter
import stage_b
import relay
import db  # 数据库层（Supabase）：ingest / 队列 / 持久化筛选结果

# LLM relay（OpenAI 兼容）——默认值统一引用 relay 模块的单一真源，避免分叉
RELAY_API_KEY = os.environ.get("RELAY_API_KEY")
RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", relay.DEFAULT_BASE_URL)
RELAY_MODEL = os.environ.get("RELAY_MODEL", relay.DEFAULT_MODEL)

# arXiv
ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_DELAY = 3.0  # arXiv 要求请求间隔 ≥ 3 秒
NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# 抓取参数：每个查询最多抓多少篇（给筛选留足材料）
FETCH_PER_QUERY = 30

# 各阶段每轮上限（成本控制；均可用 --xxx-limit=N 覆盖；<=0 表示不限）：
DIGEST_LIMIT = 5   # 导读
SCORE_LIMIT = 10   # 打分（cross-check ON，每篇 3 次调用）
STUDY_LIMIT = 3    # 深度精读（multi-pass，很慢）
PUSH_LIMIT = 10    # 推送（组装 + 发到企业微信）


# ---------------------------------------------------------------------------
# Step 2 — 抓取（standalone，镜像 crawler/arxiv_client.py 的解析逻辑）
# ---------------------------------------------------------------------------

def parse_arxiv_xml(xml_data):
    """
    解析 Atom XML，返回论文 dict 列表。优先用 ElementTree；
    若本机 expat 不可用（某些 Homebrew py3.14 构建有此缺陷），退回正则解析。
    """
    try:
        return _parse_with_et(xml_data)
    except ImportError as e:
        print(f"[WARN] ElementTree/expat 不可用（{e}），改用正则解析 arXiv feed")
        return _parse_with_regex(xml_data)


def _parse_with_et(xml_data):
    """标准解析路径（依赖 expat）。"""
    papers = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        print(f"[WARN] parse_arxiv_xml: 无法解析 XML: {e}")
        return papers

    for entry in root.findall("atom:entry", NAMESPACES):
        id_el = entry.find("atom:id", NAMESPACES)
        if id_el is None or not id_el.text:
            continue
        raw_id = id_el.text.strip()
        arxiv_id = raw_id.split("/abs/")[-1].split("v")[0]

        title_el = entry.find("atom:title", NAMESPACES)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        abstract_el = entry.find("atom:summary", NAMESPACES)
        abstract = (
            abstract_el.text.strip()
            if abstract_el is not None and abstract_el.text
            else ""
        )

        authors = []
        for author in entry.findall("atom:author", NAMESPACES):
            name_el = author.find("atom:name", NAMESPACES)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        categories = []
        for cat in entry.findall("atom:category", NAMESPACES):
            term = cat.get("term")
            if term:
                categories.append(term)

        published_el = entry.find("atom:published", NAMESPACES)
        published = ""
        if published_el is not None and published_el.text:
            published = published_el.text.strip()[:10]

        pdf_url = ""
        for link in entry.findall("atom:link", NAMESPACES):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
                break
        if not pdf_url and arxiv_id:
            pdf_url = f"http://arxiv.org/pdf/{arxiv_id}"

        comment_el = entry.find("arxiv:comment", NAMESPACES)
        arxiv_comment = " ".join(comment_el.text.split()) \
            if comment_el is not None and comment_el.text else None
        jref_el = entry.find("arxiv:journal_ref", NAMESPACES)
        journal_ref = " ".join(jref_el.text.split()) \
            if jref_el is not None and jref_el.text else None

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "categories": categories,
                "published": published,
                "pdf_url": pdf_url,
                "arxiv_comment": arxiv_comment,
                "journal_ref": journal_ref,
            }
        )
    return papers


def _clean(text):
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _parse_with_regex(xml_data):
    """
    正则解析 Atom feed（expat 不可用时的兜底）。arXiv 的 feed 是机器生成、结构规整，
    正则在此场景足够可靠；仅用于兜底，健康环境仍走 ElementTree。
    """
    if isinstance(xml_data, bytes):
        xml_data = xml_data.decode("utf-8", errors="replace")

    papers = []
    for m in re.finditer(r"<entry>(.*?)</entry>", xml_data, re.S):
        block = m.group(1)

        id_m = re.search(r"<id>(.*?)</id>", block, re.S)
        if not id_m:
            continue
        raw_id = _clean(id_m.group(1))
        arxiv_id = raw_id.split("/abs/")[-1].split("v")[0]

        title_m = re.search(r"<title>(.*?)</title>", block, re.S)
        title = _clean(title_m.group(1)) if title_m else ""

        sum_m = re.search(r"<summary>(.*?)</summary>", block, re.S)
        abstract = _clean(sum_m.group(1)) if sum_m else ""

        authors = [_clean(a) for a in re.findall(r"<name>(.*?)</name>", block, re.S)]

        categories = re.findall(r'<category[^>]*\bterm="([^"]+)"', block)

        pub_m = re.search(r"<published>(.*?)</published>", block, re.S)
        published = _clean(pub_m.group(1))[:10] if pub_m else ""

        pdf_url = ""
        for lm in re.finditer(r"<link\b[^>]*>", block):
            tag = lm.group(0)
            if 'title="pdf"' in tag or "application/pdf" in tag:
                href = re.search(r'href="([^"]+)"', tag)
                if href:
                    pdf_url = href.group(1)
                    break
        if not pdf_url and arxiv_id:
            pdf_url = f"http://arxiv.org/pdf/{arxiv_id}"

        cmt_m = re.search(r"<arxiv:comment>(.*?)</arxiv:comment>", block, re.S)
        arxiv_comment = _clean(cmt_m.group(1)) if cmt_m else None
        jref_m = re.search(r"<arxiv:journal_ref>(.*?)</arxiv:journal_ref>", block, re.S)
        journal_ref = _clean(jref_m.group(1)) if jref_m else None

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "categories": categories,
                "published": published,
                "pdf_url": pdf_url,
                "arxiv_comment": arxiv_comment,
                "journal_ref": journal_ref,
            }
        )
    return papers


def fetch_arxiv(query, max_results):
    """单次 arXiv 查询，失败返回空列表（不抛异常）。"""
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PaperRAG-daily-bot/0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as e:
        print(f"[WARN] fetch_arxiv 失败 (query={query!r}): {e}")
        return []
    return parse_arxiv_xml(data)


def fetch_recent_papers(queries, per_query=FETCH_PER_QUERY):
    """
    遍历（拓宽后的）查询集合，按 submittedDate 降序抓取，累计去重（by arxiv_id）。
    单个查询失败不影响其余（fetch_arxiv 已 try/except）。
    """
    collected = {}
    for i, query in enumerate(queries):
        batch = fetch_arxiv(query, per_query)
        for p in batch:
            collected.setdefault(p["arxiv_id"], p)
        print(f"[fetch] query 命中 {len(batch)} 篇（累计去重 {len(collected)} 篇）: {query}")
        if i < len(queries) - 1:
            time.sleep(ARXIV_DELAY)  # 尊重 arXiv 限速
    return list(collected.values())


# 挑选逻辑（Step 3 粗筛 + Step 4 细筛）已移入 paper_filter 模块。


# ---------------------------------------------------------------------------
# Step 4 — 中文导读（relay LLM）
# ---------------------------------------------------------------------------

DIGEST_SYSTEM_PROMPT = (
    "你是帮助量化研究者快速理解 AI / Agent 论文的助手。"
    "基于给定论文的标题与摘要，生成中英双语的简洁导读（提炼，不是逐句翻译摘要）。"
    "两种语言内容一致、结构一致，只是语言不同（来自对论文的同一份理解）。"
    "中文：简洁易读，专有名词保留英文（如 LLM、RAG、Transformer）。"
    "英文：natural, concise English。"
    "必须严格忠于摘要：中文和英文都不得编造摘要里没有的结果、数字或结论。"
    "只输出一个 JSON 对象，不要任何额外解释或 markdown 代码块围栏。"
    "结构："
    '{"zh": {"hook","problem","method","significance","summary"}, '
    '"en": {"hook","problem","method","significance","summary"}}。'
    "每个字段是一段简洁文字：hook 是一句话副标题钩子，problem 是这篇论文要解决什么问题，"
    "method 是核心方法 / 思路，significance 是为什么重要，summary 是一句话总结。"
)


def call_relay(paper):
    """调用 relay 生成导读，返回 (content, usage)。复用 relay.relay_chat，不再自造 HTTP。"""
    user_prompt = (
        f"论文标题：{paper['title']}\n\n"
        f"分类：{', '.join(paper.get('categories') or [])}\n\n"
        f"摘要：\n{paper['abstract']}"
    )
    return relay.relay_chat(DIGEST_SYSTEM_PROMPT, user_prompt, temperature=0.3)


def generate_digest(paper):
    """
    生成结构化中文导读 dict。
    成功：{"degraded": False, "hook","problem","method","significance","summary"}
    失败：{"degraded": True, ...}（用摘要兜底，保证 HTML 仍可生成）
    """
    def _one_lang(obj):
        obj = obj if isinstance(obj, dict) else {}
        return {
            "hook": obj.get("hook", ""),
            "problem": obj.get("problem", ""),
            "method": obj.get("method", ""),
            "significance": obj.get("significance", ""),
            "summary": obj.get("summary", ""),
        }

    try:
        content, usage = call_relay(paper)
        parsed = relay.extract_json(content)
        if not parsed:
            raise ValueError("模型输出无法解析为 JSON")
        if not isinstance(parsed.get("zh"), dict) or not isinstance(parsed.get("en"), dict):
            raise ValueError("JSON 缺少 zh / en 双语字段")
        return {
            "degraded": False,
            "zh": _one_lang(parsed["zh"]),
            "en": _one_lang(parsed["en"]),
            "usage": usage,
        }
    except Exception as e:
        print(f"[WARN] 生成导读失败 ({paper['arxiv_id']}): {e}；改用摘要兜底")
        return {
            "degraded": True,
            "raw_abstract": paper.get("abstract", ""),
        }


# ---------------------------------------------------------------------------
# Step 5 — 套用 skill 风格生成 HTML
# ---------------------------------------------------------------------------

def load_css():
    try:
        with open(CSS_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] 无法读取 skill CSS ({CSS_PATH}): {e}；HTML 将缺少样式")
        return ""


def _paras(text):
    """把多段文本拆成若干 <p>（转义），空文本返回空串。"""
    if not text:
        return ""
    blocks = [b.strip() for b in str(text).split("\n") if b.strip()]
    return "\n".join(f"        <p>{html.escape(b)}</p>" for b in blocks)


# 每种语言的章节标题 / 标签（section key 对应 digest dict 的字段）
LANG_CFG = {
    "zh": {
        "sections": [
            ("问题背景 · Problem", "problem"),
            ("核心方法 · Method", "method"),
            ("为何重要 · Significance", "significance"),
        ],
        "toc_label": "目录",
        "summary_label": "一句话总结",
    },
    "en": {
        "sections": [
            ("Problem", "problem"),
            ("Method", "method"),
            ("Why It Matters", "significance"),
        ],
        "toc_label": "Contents",
        "summary_label": "In One Sentence",
    },
}

# 语言切换的样式：小、克制、右上角，配色沿用 skill 的 CSS 变量
TOGGLE_CSS = """
/* language toggle (daily_bot) */
.lang-toggle {
    position: fixed; top: 18px; right: 18px; z-index: 100;
    display: flex; border: 1px solid var(--border-strong); border-radius: 4px;
    overflow: hidden; background: var(--bg);
    font-family: 'IBM Plex Mono', monospace;
}
.lang-toggle button {
    border: none; background: var(--bg); color: var(--ink-secondary);
    font-family: inherit; font-size: 0.75rem; letter-spacing: 0.05em;
    padding: 5px 11px; cursor: pointer;
}
.lang-toggle button + button { border-left: 1px solid var(--border); }
.lang-toggle button.active { background: var(--ink); color: #fff; }
.lang-toggle button:not(.active):hover { background: var(--bg-warm); }
body.show-zh .l-en { display: none; }
body.show-en .l-zh { display: none; }
"""

# 语言切换的 JS：切 body class + 高亮当前按钮
TOGGLE_JS = """
<script>
(function () {
    var btns = document.querySelectorAll('.lang-toggle button');
    btns.forEach(function (b) {
        b.addEventListener('click', function () {
            document.body.className = 'show-' + b.dataset.lang;
            btns.forEach(function (x) { x.classList.toggle('active', x === b); });
        });
    });
})();
</script>
"""


def _render_lang_block(lang_code, d):
    """生成某一语言的正文块（TOC + 章节 + 一句话总结），锚点按语言命名以免冲突。"""
    cfg = LANG_CFG[lang_code]
    toc, parts = [], []
    for idx, (heading, key) in enumerate(cfg["sections"], start=1):
        sid = f"{lang_code}-s{idx}"
        toc.append(f'        <li><a href="#{sid}">{html.escape(heading)}</a></li>')
        parts.append(
            f'<section id="{sid}">\n'
            f'<h2><span class="section-num">{idx}</span>{html.escape(heading)}</h2>\n'
            f'{_paras(d.get(key, ""))}\n'
            f'</section>'
        )

    summary = d.get("summary", "")
    summary_box = ""
    if summary:
        summary_box = (
            '\n<div class="box success">\n'
            f'    <div class="box-label">{html.escape(cfg["summary_label"])}</div>\n'
            f'    <p><strong>{html.escape(summary)}</strong></p>\n'
            '</div>'
        )

    toc_html = (
        '<nav class="toc">\n'
        f'    <div class="toc-label">{html.escape(cfg["toc_label"])}</div>\n'
        '    <ol>\n' + "\n".join(toc) + '\n    </ol>\n</nav>'
    )
    body = toc_html + "\n\n" + "\n\n".join(parts) + summary_box
    return f'<div class="lang-block l-{lang_code}">\n{body}\n</div>'


def render_html(paper, digest, css):
    """按 skill 的 template.html 结构生成自包含 HTML 字符串（中英双语 + 切换）。"""
    title = paper.get("title", "").strip()
    authors = paper.get("authors") or []
    authors_str = ", ".join(authors[:8]) + (" 等" if len(authors) > 8 else "")
    categories = paper.get("categories") or []
    primary_cat = categories[0] if categories else "arXiv"
    arxiv_id = paper.get("arxiv_id", "")
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    published = paper.get("published", "")

    doc_title = f"论文导读 · {title}" if title else "论文导读"

    if digest.get("degraded"):
        # 兜底：仅展示英文原始摘要 + 提示条，不显示语言切换
        raw = digest.get("raw_abstract", "")
        subtitle_html = ('    <p class="header-subtitle">LLM digest generation failed — '
                         'showing the raw arXiv abstract instead.</p>')
        toggle_html = ""
        body_class = ""
        content_html = (
            '<div class="box danger">\n'
            '    <div class="box-label">注意 · Notice</div>\n'
            '    <p>本篇导读的 LLM 生成步骤失败（通常是 <code>RELAY_API_KEY</code> 未设置或 relay 不可达）；'
            '下方直接展示 arXiv 原始摘要。设置好 key 后重跑即可得到中英双语导读。</p>\n'
            '</div>\n\n'
            '<section id="s1">\n'
            '<h2><span class="section-num">1</span>Abstract</h2>\n'
            f'{_paras(raw)}\n'
            '</section>'
        )
    else:
        zh, en = digest.get("zh", {}), digest.get("en", {})
        subtitle_html = (
            f'    <p class="header-subtitle l-zh">{html.escape(zh.get("hook", ""))}</p>\n'
            f'    <p class="header-subtitle l-en">{html.escape(en.get("hook", ""))}</p>'
        )
        toggle_html = (
            '<div class="lang-toggle">\n'
            '    <button type="button" data-lang="zh" class="active">中</button>\n'
            '    <button type="button" data-lang="en">EN</button>\n'
            '</div>'
        )
        body_class = "show-zh"  # 默认中文
        content_html = _render_lang_block("zh", zh) + "\n\n" + _render_lang_block("en", en)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(doc_title)}</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Libre+Baskerville:ital,wght@0,700;1,400&family=Noto+Sans+SC:wght@400;700&family=Noto+Serif+SC:wght@700&family=Source+Sans+3:wght@400;700&display=swap" rel="stylesheet">

<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
    onload="renderMathInElement(document.body, {{
        delimiters: [
            {{left: '$$', right: '$$', display: true}},
            {{left: '$', right: '$', display: false}}
        ],
        throwOnError: false
    }});"></script>

<style>
{css}
{TOGGLE_CSS}
</style>
</head>
<body class="{body_class}">

<div class="container">
{toggle_html}

<header class="header">
    <div class="header-meta">{html.escape(primary_cat)} · 每日论文导读 / Daily Digest</div>
    <h1>{html.escape(title)}</h1>
{subtitle_html}
    <div class="paper-ref">
        <span class="paper-title">{html.escape(title)}</span><br>
        {html.escape(authors_str)} · arXiv {html.escape(published)} · <a href="{html.escape(abs_url)}">arXiv:{html.escape(arxiv_id)}</a>
    </div>
</header>

{content_html}

<footer class="footer">
    <p>原论文 / Paper：<a href="{html.escape(abs_url)}">{html.escape(title)}</a></p>
    <p style="margin-top: 6px;">PaperRAG · 每日论文导读（v0）</p>
</footer>

</div>
{TOGGLE_JS}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_filter(conn):
    """
    对 backlog（daily_paper.filtered_at IS NULL）做一次性筛选：
    甲（逐篇打 loose area 标签）→ 乙（stage-B 分类候选）→ 把结果持久化（set_filter_result）。
    每篇只筛一次；下次运行不会重筛。返回统计 dict。
    """
    backlog_ids = db.get_unfiltered(conn)
    if not backlog_ids:
        return {"backlog": 0}
    backlog = db.get_papers(conn, backlog_ids)

    # 甲：逐篇 loose 匹配
    for p in backlog:
        p["area_signals"] = paper_filter.match_signals(p)
        p["areas"] = list(p["area_signals"].keys())
    candidates = [p for p in backlog if p["areas"]]
    noncandidates = [p for p in backlog if not p["areas"]]
    print(f"[甲] backlog {len(backlog)} 篇 → 候选 {len(candidates)}，甲未命中 {len(noncandidates)}")

    sb = stage_b.classify(candidates) if candidates else {"verdicts": {}}
    if candidates and not sb.get("verdicts"):
        # 乙 整体失败（如 relay 不可达）：本轮不落库，backlog 保留，下次重试
        print("[乙][WARN] stage-B 整体失败，本轮不落库，backlog 保留待下次重试。")
        return {"backlog": len(backlog), "aborted": True}

    conn = db.ensure(conn)  # stage-B 批量调用耗时较久，落库前确保连接可用
    # 甲未命中 → not_relevant（确定性，直接落库）
    for p in noncandidates:
        db.set_filter_result(conn, p["arxiv_id"], "not_relevant", "甲：分类/关键词未命中", False)

    per_area, n_notrel, n_uncertain = {}, 0, 0
    for p in candidates:
        area = p.get("stage_b_area")
        reason = p.get("stage_b_reason", "") or ""
        if area in paper_filter.FOCUS_AREAS:
            db.set_filter_result(conn, p["arxiv_id"], area, reason, True)
            per_area[area] = per_area.get(area, 0) + 1
        elif area == "not_relevant":
            db.set_filter_result(conn, p["arxiv_id"], "not_relevant", reason, False)
            n_notrel += 1
        else:
            # 乙未判定(uncertain)：保守记 not_relevant，保证 filter-once（不无限重筛）
            db.set_filter_result(conn, p["arxiv_id"], "not_relevant", "乙：未判定(uncertain)", False)
            n_uncertain += 1

    return {"backlog": len(backlog), "candidates": len(candidates),
            "noncandidates": len(noncandidates), "relevant_per_area": per_area,
            "not_relevant": len(noncandidates) + n_notrel, "uncertain": n_uncertain}


def run_digest(conn, limit):
    """
    处理导读 backlog：is_relevant=TRUE AND digest_at IS NULL（仅前 limit 篇，成本控制）。
    每篇生成中英双语导读 HTML → 写 output/ → 落库（digest_path + digest_at）。
    仅在导读成功时落库；LLM 失败（degraded）不落库，留待下次重试。返回统计。
    """
    limit = None if (limit is not None and limit <= 0) else limit
    # 方向轮转取 backlog：跨方向交替，避免 agent 独占（小方向 lob/ai4math 也能尽快导读）
    rr = db.get_undigested_round_robin(conn, paper_filter.A_CLASS_ORDER,
                                       limit=(limit or 100000))
    if not rr:
        return {"picked": 0, "done": 0, "degraded": 0}
    ids = [aid for aid, _ in rr]
    by_id = {p["arxiv_id"]: p for p in db.get_papers(conn, ids)}
    ordered = [by_id[i] for i in ids if i in by_id]  # 保持轮转顺序

    css = load_css()
    today = datetime.date.today().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    done, degraded = 0, 0
    for paper in ordered:
        print(f"[digest] {paper['arxiv_id']} [{ (db_area(conn, paper['arxiv_id'])) }] — {(paper.get('title') or '')[:50]}")
        digest = generate_digest(paper)
        if digest.get("degraded"):
            degraded += 1
            print(f"[digest][WARN] {paper['arxiv_id']}: LLM 失败(degraded)，不落库，下次重试")
            continue
        doc = render_html(paper, digest, css)
        safe_id = paper["arxiv_id"].replace("/", "_")
        path = os.path.join(OUTPUT_DIR, f"{today}_{safe_id}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        conn = db.ensure(conn)  # 导读 LLM 后确保连接可用
        db.mark_stage(conn, paper["arxiv_id"], "digest", path)  # digest_at + digest_path
        done += 1
        zh = digest.get("zh", {})
        print(f"    -> 成功，已落库 digest_path；zh: {(zh.get('hook') or '')[:60]}")
    return {"picked": len(ordered), "done": done, "degraded": degraded}


def db_area(conn, arxiv_id):
    with conn.cursor() as cur:
        cur.execute("SELECT area FROM daily_paper WHERE arxiv_id=%s;", (arxiv_id,))
        r = cur.fetchone()
        return r[0] if r else "?"


def _score_dict(scorer, res):
    """把 scorer.generate_score 的返回整理成 db.upsert_score 需要的字段。"""
    norm, fresh = res["norm"], res["fresh"]
    return {
        "freshness_score": fresh["score"], "freshness_days": fresh["days"],
        "freshness_label": fresh["label"],
        "repro_score": res["repro_total"], "repro_overall_reason": norm["repro_overall"],
        "repro_subitems": {k: {"score": v["score"], "reason": v["reason"],
                               "weight": scorer.REPRO_WEIGHTS[k]}
                           for k, v in norm["repro_subs"].items()},
        "novelty_score": int(round(res["novelty_total"])),
        "novelty_reason": norm["novelty_overall"],
        "novelty_total": res["novelty_total"], "paper_type": norm["paper_type"],
        "novelty_subitems": {k: {"score": v["score"], "reason": v["reason"]}
                             for k, v in norm["novelty_subs"].items()},
        "domain": norm["domain"], "model": scorer.MAIN_MODEL,
        "cross_checked": True, "cross_notes": res["cross_notes"],
        # 两个新维度：领域相关性 + 权威性（authority na → 不写分数，标 na 非 0）
        "domain_relevance_score": (res.get("domain_relevance") or {}).get("score")
        if res.get("domain_relevance") else None,
        "domain_relevance_reason": (res.get("domain_relevance") or {}).get("reason")
        if res.get("domain_relevance") else None,
        "authority_score": None if (res.get("authority") or {}).get("na")
        else (res.get("authority") or {}).get("score"),
        "authority_reason": (res.get("authority") or {}).get("reason"),
        "authority_institutions": (res.get("authority") or {}).get("institutions") or [],
        "authority_venue": (res.get("authority") or {}).get("venue"),
        "authority_na": bool((res.get("authority") or {"na": True}).get("na", True)),
    }


def run_score(conn, limit):
    """打分 backlog：相关+已导读+未打分（前 limit 篇）。复用 scorer；结果落库 + 标记 scored_at。"""
    import scorer
    ids = db.get_scorable(conn, limit if limit and limit > 0 else 100000)
    if not ids:
        return {"picked": 0, "done": 0}
    done = 0
    for aid in ids:
        print(f"[score] {aid} …（cross-check ON）")
        try:
            res = scorer.generate_score(aid, cross_check_on=True)
        except Exception as e:
            print(f"[score][WARN] {aid} 打分失败：{e}")
            continue
        conn = db.ensure(conn)  # 打分 LLM 耗时较久，写库前确保连接仍可用
        db.upsert_score(conn, aid, _score_dict(scorer, res))
        db.mark_stage(conn, aid, "score", res["path"])
        done += 1
        norm, fresh = res["norm"], res["fresh"]
        print(f"    -> {os.path.basename(res['path'])}  fresh {fresh['score']} · "
              f"repro {res['repro_total']} · novelty {res['novelty_total']}({norm['paper_type']})")
    return {"picked": len(ids), "done": done}


def run_study(conn, limit):
    """深度精读 backlog：相关+已导读+未精读，方向轮转取 limit 篇。multi-pass（gpt-5.6-luna）。"""
    import deep_study
    rr = db.get_unstudied_round_robin(conn, paper_filter.A_CLASS_ORDER,
                                      limit=(limit if limit and limit > 0 else 100000))
    studied = []
    for aid, area in rr:
        print(f"[study] {aid} [{area}] …（multi-pass / {deep_study.DEFAULT_MODEL}，较慢）")
        try:
            sres = deep_study.generate_study(aid, model=deep_study.DEFAULT_MODEL)
        except Exception as e:
            print(f"[study][WARN] {aid} 失败：{e}")
            continue
        path = sres.get("path")
        conn = db.ensure(conn)  # 深度精读极慢，写库前确保连接仍可用
        db.mark_stage(conn, aid, "deep_study", path)
        studied.append(aid)
        print(f"    -> {os.path.basename(path) if path else '?'}")
    return studied


# ---------------------------------------------------------------------------
# 以「推送批次」为核心的一致化交付：digest / score / deep-study 都围绕同一批最新论文
# ---------------------------------------------------------------------------

def _ensure_digest(conn, aid, css):
    """确保该篇有导读；缺则生成并落库。返回 (conn, has_digest)。"""
    if db.get_stage_status(conn, aid)["digest"]:
        return conn, True
    paper = (db.get_papers(conn, [aid]) or [None])[0]
    if not paper:
        return conn, False
    digest = generate_digest(paper)
    if digest.get("degraded"):
        print(f"    [digest][WARN] {aid} degraded，不落库（下轮重试）")
        return conn, False
    doc = render_html(paper, digest, css)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR,
                        f"{datetime.date.today().isoformat()}_{aid.replace('/', '_')}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    conn = db.ensure(conn)  # 导读 LLM 后确保连接可用
    db.mark_stage(conn, aid, "digest", path)
    print(f"    [digest] {aid} 生成")
    return conn, True


def _ensure_score(conn, aid):
    """确保该篇有评分；缺则生成并落库。返回 (conn, has_score)。"""
    import scorer
    if db.get_stage_status(conn, aid)["scored"]:
        return conn, True
    try:
        res = scorer.generate_score(aid, cross_check_on=True)
    except Exception as e:
        print(f"    [score][WARN] {aid}: {e}")
        return conn, False
    conn = db.ensure(conn)  # 打分 LLM 耗时较久，写库前确保连接可用
    db.upsert_score(conn, aid, _score_dict(scorer, res))
    db.mark_stage(conn, aid, "score", res["path"])
    norm, fresh = res["norm"], res["fresh"]
    print(f"    [score] {aid} fresh {fresh['score']} · repro {res['repro_total']} · "
          f"novelty {res['novelty_total']}({norm['paper_type']})")
    return conn, True


def _ensure_study(conn, aid):
    """确保该篇有深度精读；缺则生成（multi-pass，gpt-5.6-luna）并落库。返回 (conn, has_study)。"""
    import deep_study
    if db.get_stage_status(conn, aid)["studied"]:
        return conn, True
    try:
        sres = deep_study.generate_study(aid, model=deep_study.DEFAULT_MODEL)
    except Exception as e:
        print(f"    [study][WARN] {aid}: {e}")
        return conn, False
    path = sres.get("path")
    conn = db.ensure(conn)  # 深度精读极慢，写库前确保连接可用
    db.mark_stage(conn, aid, "deep_study", path)
    print(f"    [study] {aid} -> {os.path.basename(path) if path else '?'}")
    return conn, True


def _ensure_composite(conn, aid):
    """确保该篇有综评；缺则读 5 个真实子分 → scorer.generate_composite → 落库（只写 composite 两列）。
    未打分则跳过。返回 (conn, has_composite)。绝不改 5 个子分。"""
    import scorer
    sc = db.get_score(conn, aid)
    if not sc:
        return conn, False                       # 未打分，不能综评
    if sc.get("composite_score") is not None:
        return conn, True                        # 增量：已综评
    meta = (db.get_papers(conn, [aid]) or [{}])[0]
    row = db.get_daily_row(conn, aid) or {}
    meta = dict(meta); meta["area"] = row.get("area")

    def fv(x):
        return float(x) if x is not None else None
    sub = {
        "freshness": fv(sc.get("freshness_score")),
        "reproducibility": fv(sc.get("repro_score")),
        "novelty": fv(sc.get("novelty_total")),
        "paper_type": sc.get("paper_type"),
        "domain_relevance": fv(sc.get("domain_relevance_score")),
        "authority": ("N/A" if sc.get("authority_na") else fv(sc.get("authority_score"))),
    }
    res = scorer.generate_composite(meta, sub)   # 单次 Claude；失败 → na（不伪造 0）
    conn = db.ensure(conn)
    db.upsert_composite(conn, aid, res["score"], res["reason"] or None)
    print(f"    [composite] {aid} -> {res['score'] if not res['na'] else 'N/A'}"
          f"  {res['reason'][:44]}")
    return conn, (not res["na"])


def _count_big_sections(path):
    """完整性度量：数深读 HTML 里的 <section id="s..>（每个=一个大章节）。文件缺失→0。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().count('<section id="s')
    except Exception:
        return 0


def _classify_study_error(exc):
    """区分失败类型：relay（限流/超时，退避重试）vs content（内容问题，3 次后放弃该篇）。"""
    s = str(exc).lower()
    relay_sig = ("503", "429", "502", "504", "timed out", "timeout",
                 "temporarily", "service unavailable", "bad gateway", "gateway time")
    return "relay" if any(k in s for k in relay_sig) else "content"


def run_study_with_backoff(conn, target_ids, cap_hours=5.0,
                           backoff_min=(10, 15, 20, 25, 30),
                           min_big=4, max_content_attempts=3):
    """
    无人值守地把 target_ids（综评前 N 篇）跑成【完整】深读，扛住 relay 503 限流：
      - 逐篇跑，跑完查完整性：n_big_sections >= min_big 才算成功（1-2 段=跳过正文=失败）。
      - relay 503/超时：退避 10→15→20→25→30 分（连续失败递增，成功清零），然后换下一篇（round-robin），
        失败的下一轮再回来；relay 失败不计入放弃次数（5h 内无限重试）。
      - content（不完整/坏 PDF/400）：同一篇累计 max_content_attempts 次后放弃该篇。
      - 安全阀：总时长 cap_hours 封顶，且 6 篇全完成即提前停；每次 sleep 都不超过剩余额度。
      - 增量：已完整的深读直接跳过（可断点续跑）。
      - 每次尝试写一行日志（时间戳/论文/rest/结果）到 top30_study_retry.log，供事后调参。
    只包住深读步骤；打分/综评不受影响。返回 {completed, gave_up, pending, log_path}。
    """
    import deep_study
    log_path = os.path.join(HERE, "top30_study_retry.log")

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    start = time.time()
    CAP = cap_hours * 3600
    BACKOFF = [m * 60 for m in backoff_min]

    def elapsed_m():
        return int((time.time() - start) / 60)

    def remaining():
        return CAP - (time.time() - start)

    pending = list(target_ids)
    content_fails = {a: 0 for a in pending}
    completed, gave_up = [], []
    consec_relay = 0

    # 增量：已完整的直接算完成（断点续跑）
    for aid in list(pending):
        row = db.get_daily_row(conn, aid) or {}
        sp = row.get("deep_study_path")
        if db.get_stage_status(conn, aid).get("studied") and sp and os.path.exists(sp) \
                and _count_big_sections(sp) >= min_big:
            completed.append(aid)
            pending.remove(aid)
            logln(f"paper={aid} outcome=already-complete nbig={_count_big_sections(sp)}")

    logln(f"START targets={list(target_ids)} to-do={pending} cap={cap_hours}h "
          f"min_big={min_big} backoff={list(backoff_min)}min")

    rnd = 0
    while pending and remaining() > 0:
        rnd += 1
        logln(f"round={rnd} start pending={pending}")
        for aid in list(pending):
            if remaining() <= 0:
                break
            att = content_fails[aid] + 1
            try:
                res = deep_study.generate_study(aid)
                path = res.get("path") if isinstance(res, dict) else None
                nbig = _count_big_sections(path) if path else 0
                if nbig >= min_big:
                    conn = db.ensure(conn)  # 循环含长 sleep，写库前重连
                    db.mark_stage(conn, aid, "deep_study", path)
                    pending.remove(aid)
                    completed.append(aid)
                    consec_relay = 0  # 成功 → 退避清零
                    logln(f"round={rnd} paper={aid} attempt={att} outcome=success "
                          f"nbig={nbig} rest=0m elapsed={elapsed_m()}m")
                else:  # 跑完但不完整（跳过正文）→ content 失败
                    content_fails[aid] += 1
                    tag = " -> GIVE-UP(content)" if content_fails[aid] >= max_content_attempts else ""
                    if content_fails[aid] >= max_content_attempts:
                        pending.remove(aid)
                        gave_up.append(aid)
                    logln(f"round={rnd} paper={aid} attempt={content_fails[aid]} "
                          f"outcome=incomplete nbig={nbig} rest=0m elapsed={elapsed_m()}m{tag}")
            except Exception as e:
                if _classify_study_error(e) == "relay":
                    consec_relay += 1
                    rest = BACKOFF[min(consec_relay - 1, len(BACKOFF) - 1)]
                    rest = min(rest, max(0, remaining()))  # cap-aware
                    logln(f"round={rnd} paper={aid} attempt={att} outcome=503/timeout "
                          f"nbig=- rest={int(rest / 60)}m elapsed={elapsed_m()}m ({str(e)[:60]})")
                    if rest > 0:
                        time.sleep(rest)  # 歇完换下一篇（round-robin）；本篇留在 pending 下轮再来
                else:  # content 异常（400 / 坏 PDF 等）
                    content_fails[aid] += 1
                    tag = " -> GIVE-UP(content)" if content_fails[aid] >= max_content_attempts else ""
                    if content_fails[aid] >= max_content_attempts:
                        pending.remove(aid)
                        gave_up.append(aid)
                    logln(f"round={rnd} paper={aid} attempt={content_fails[aid]} "
                          f"outcome=content-error nbig=- rest=0m elapsed={elapsed_m()}m{tag} "
                          f"({str(e)[:60]})")

    reason = "all-complete" if not pending else ("cap-hit" if remaining() <= 0 else "stopped")
    logln(f"FINISHED reason={reason} completed={completed} gave_up={gave_up} "
          f"still_pending={pending} rounds={rnd} elapsed={elapsed_m()}m")
    return {"completed": completed, "gave_up": gave_up, "pending": pending, "log_path": log_path}


def run_top30(conn, fetch_n=30, study_top=6):
    """
    每日 Top-N 榜单模式：选最新相关 N 篇 → 真实 5 维打分（增量）→ 综评 0-10（增量）→
    按综评降序 → 深读前 study_top 篇（增量）→ 生成一页概览 → 推送（概览每天一次 + 各深读文件一次）。
    5 个子分为既有 scorer 的真实结果，综评为加法层，绝不改写子分。
    """
    import assemble
    import wecom
    conn = db.ensure(conn)
    batch = db.get_top30_batch(conn, fetch_n)
    if not batch:
        print("  无相关论文可选（先跑筛选）。")
        return {"candidates": 0}
    ids = [a for a, _ in batch]
    print(f"[top30] 候选 {len(ids)} 篇（最新相关）：{', '.join(ids)}")

    # 1) 打分（真实 5 维，含 Claude→luna→Claude 交叉复核；增量）
    print("\n[top30] 打分（scorer.generate_score，增量）…")
    for aid in ids:
        conn, _ = _ensure_score(conn, aid)
    # 2) 综评（读 5 子分 → 0-10 + 中文理由；增量）
    print("\n[top30] 综评（composite，增量）…")
    for aid in ids:
        conn, _ = _ensure_composite(conn, aid)
    # 3) 排序：综评降序，N/A 置底
    comps = {}
    for aid in ids:
        sc = db.get_score(conn, aid)
        comps[aid] = (float(sc["composite_score"])
                      if sc and sc.get("composite_score") is not None else None)
    ranked = sorted(ids, key=lambda a: (comps[a] is not None, comps[a] or 0.0), reverse=True)
    print("\n[top30] 综评排序：" + " | ".join(
        f"{a}={comps[a] if comps[a] is not None else 'N/A'}" for a in ranked))
    # 4) 深读综评前 study_top（backoff + round-robin + 完整性门槛；无人值守，最长 5h）
    print(f"\n[top30] 深度精读综评前 {study_top} 篇（backoff+round-robin，完整性≥4 段，最长 5h）…")
    conn = db.ensure(conn)
    sres = run_study_with_backoff(conn, ranked[:study_top])
    studied = sres["completed"]
    conn = db.ensure(conn)  # 退避循环含长 sleep，之后重连
    print(f"[top30] 深读完成 {len(studied)} 篇；放弃 {len(sres['gave_up'])} 篇"
          f"（内容问题）；日志 {sres['log_path']}")
    # 5) 概览
    date_str = datetime.date.today().isoformat()
    conn = db.ensure(conn)
    overview = assemble.assemble_overview(conn, ranked, studied, date_str)
    print(f"\n[top30] 概览：{overview}")

    # 6) 推送：概览每天一次 + 每个深读文件一次（各自去重）
    pushed = {"overview": False, "studies": 0, "skipped": 0, "failed": 0}
    if not wecom.WEBHOOK:
        print("[top30][push][WARN] 未设置 WECOM_WEBHOOK_URL，跳过推送（文件已生成）。")
        return {"candidates": len(ids), "ranked": ranked, "comps": comps,
                "studied": studied, "overview_path": overview, "pushed": pushed}
    conn = db.ensure(conn)
    ov_key = f"overview_{date_str}"
    if db.top30_already_pushed(conn, ov_key):
        print("[top30][push] 概览今日已推，跳过。")
        pushed["skipped"] += 1
    else:
        ok, detail = wecom.push_file(
            f"📊 今日 Top{len(ranked)} 论文精选 · {date_str}（附综评前 {len(studied)} 篇深读）", overview)
        conn = db.ensure(conn)
        db.top30_record_push(conn, ov_key, "success" if ok else "failed", str(detail)[:400])
        pushed["overview"] = ok
        print(f"[top30][push] 概览 -> {'成功' if ok else '失败'}：{detail}")
        time.sleep(4)
    for aid in studied:
        conn = db.ensure(conn)
        skey = f"study_{aid}"
        if db.top30_already_pushed(conn, skey):
            pushed["skipped"] += 1
            continue
        row = db.get_daily_row(conn, aid) or {}
        sp = row.get("deep_study_path")
        if not sp or not os.path.exists(sp):
            continue
        meta = (db.get_papers(conn, [aid]) or [{}])[0]
        ok, detail = wecom.push_file(f"📄 深读：{meta.get('title') or aid}", sp)
        conn = db.ensure(conn)
        db.top30_record_push(conn, skey, "success" if ok else "failed", str(detail)[:400])
        pushed["studies"] += 1 if ok else 0
        pushed["failed"] += 0 if ok else 1
        print(f"[top30][push] 深读 {aid} -> {'成功' if ok else '失败'}：{detail}")
        time.sleep(4)

    return {"candidates": len(ids), "ranked": ranked, "comps": comps,
            "studied": studied, "overview_path": overview, "pushed": pushed}


def run_deliverables(conn, push_limit, study_limit):
    """
    以「推送批次」为核心，让三件产物围绕同一批最新论文，输出一致的完整交付物：
      1) 选批次 = 最新 relevant 且尚未成功推送的论文（round-robin 跨方向），上限 push_limit；
      2) 对整批补齐 digest + score（缺则生成）；
      3) 对批次【前 study_limit 篇】补齐 deep-study（同一批，不再独立轮转选别的论文）；
      4) 逐篇组装完整报告并推送（去重：已成功推过的绝不重推）。
    这样每篇被推送的报告都是「digest + score(+ 前几篇还带 deep-study)」围绕同一批的完整交付物。
    """
    import assemble
    import wecom

    conn = db.ensure(conn)
    pl = push_limit if push_limit and push_limit > 0 else 100000
    batch = db.get_push_batch(conn, paper_filter.A_CLASS_ORDER, pl)
    if not batch:
        print("  本轮无新可推送论文（增量：最新的相关论文都推过了）。")
        return {"batch": 0, "studied": 0, "pushed": 0, "failed": 0, "skipped": 0,
                "assembled": []}

    batch_ids = [aid for aid, _ in batch]
    area_by_id = {aid: area for aid, area in batch}
    sl = len(batch_ids) if (study_limit is None or study_limit <= 0) \
        else min(study_limit, len(batch_ids))
    study_ids = set(batch_ids[:sl])  # 深读 = 批次前 sl 篇（与推送同一批）

    print(f"[batch] 本轮推送批次 {len(batch_ids)} 篇（前 {len(study_ids)} 篇做深度精读）：")
    for aid in batch_ids:
        print(f"    {aid} [{area_by_id[aid]}]" + ("  ★deep-study" if aid in study_ids else ""))

    # ---- 补齐三件产物：digest + score（全批）；deep-study（前 sl 篇，同一批）----
    css = load_css()
    print("\n[batch] 补齐 digest / score / deep-study …")
    for aid in batch_ids:
        conn, has_digest = _ensure_digest(conn, aid, css)
        if has_digest:
            conn, _ = _ensure_score(conn, aid)
            if aid in study_ids:
                conn, _ = _ensure_study(conn, aid)

    # ---- 组装完整报告 ----
    print("\n[batch] 组装完整报告 …")
    assembled = []
    for aid in batch_ids:
        conn = db.ensure(conn)
        full = assemble.assemble_full(conn, aid)
        if not full:
            print(f"    [assemble][WARN] {aid} 无 digest，跳过（未生成完整报告）")
            continue
        assembled.append((aid, full))
        print(f"    [assemble] {aid} -> {os.path.basename(full)}")

    if not wecom.WEBHOOK:
        print("[push][WARN] WECOM_WEBHOOK_URL 未设置，跳过推送（完整报告已生成）。")
        return {"batch": len(batch_ids), "studied": len(study_ids), "pushed": 0,
                "failed": 0, "skipped": len(assembled),
                "assembled": [p for _, p in assembled]}

    # ---- 推送（去重、限流；仅推 digest+score 齐备的，深读 best-effort）----
    print("\n[batch] 推送企业微信 …")
    pushed = failed = skipped = deferred = 0
    for aid, full in assembled:
        conn = db.ensure(conn)
        if db.already_pushed(conn, aid):  # 去重：已成功推过的绝不重推
            skipped += 1
            print(f"    [push] {aid} 已推过，跳过")
            continue
        # 完整性门槛：digest+score 必须齐备才推（避免推「暂无评分」的半成品并被 record_push 锁死）。
        # 缺评分通常是本轮打分瞬时失败——不推、不记录，留在批次里下轮重试补齐后再推。深读为 best-effort，缺不挡推。
        if not db.get_stage_status(conn, aid)["scored"]:
            deferred += 1
            print(f"    [push] {aid} 缺评分（本轮打分失败），本轮不推，下轮补齐后再推")
            continue
        meta = (db.get_papers(conn, [aid]) or [{}])[0]
        row = db.get_daily_row(conn, aid) or {}
        sc = db.get_score(conn, aid)
        summary = assemble.extract_hook(row.get("digest_path") or "") or "（无摘要）"
        scores_text = wecom.format_scores(sc)  # 含领域相关性 + 权威性（得体披露）
        print(f"    [push] {aid} → 企业微信 …")
        ok, detail = wecom.push_paper(meta.get("title") or aid, area_by_id.get(aid, "?"),
                                      summary, scores_text, full)
        conn = db.ensure(conn)  # 推送含 HTTP+sleep，记录前确保连接可用
        db.record_push(conn, aid, "success" if ok else "failed", str(detail)[:500])
        if ok:
            pushed += 1
            print(f"        -> 成功：{detail}")
        else:
            failed += 1
            print(f"        -> 失败：{detail}")
        time.sleep(4)  # 论文之间也间隔，配合 20/min 限流

    return {"batch": len(batch_ids), "studied": len(study_ids), "pushed": pushed,
            "failed": failed, "skipped": skipped, "deferred": deferred,
            "assembled": [p for _, p in assembled]}


def run_assemble_push(conn, push_limit, studied_ids):
    """组装完整报告（推送集 ∪ 本轮精读集），并把未推过的推到企业微信。"""
    import assemble
    import wecom
    push_ids = db.get_pushable(conn, push_limit if push_limit and push_limit > 0 else 100000)
    # 组装：推送目标 + 本轮精读的（后者即使不推也生成完整报告便于查看）
    assemble_ids = list(dict.fromkeys(push_ids + [a for a in studied_ids]))
    full_by_id = {}
    for aid in assemble_ids:
        p = assemble.assemble_full(conn, aid)
        if p:
            full_by_id[aid] = p
            print(f"[assemble] {aid} -> {os.path.basename(p)}")

    pushed = failed = skipped = 0
    if not wecom.WEBHOOK:
        print("[push][WARN] WECOM_WEBHOOK_URL 未设置，跳过推送。")
        return {"assembled": len(full_by_id), "pushed": 0, "failed": 0,
                "skipped": len(push_ids), "full_by_id": full_by_id}
    for aid in push_ids:
        if db.already_pushed(conn, aid):
            skipped += 1
            continue
        full = full_by_id.get(aid) or assemble.assemble_full(conn, aid)
        if not full:
            print(f"[push][WARN] {aid} 无完整报告可推，跳过")
            continue
        meta = (db.get_papers(conn, [aid]) or [{}])[0]
        row = db.get_daily_row(conn, aid) or {}
        sc = db.get_score(conn, aid)
        summary = assemble.extract_hook(row.get("digest_path") or "") or "（无摘要）"
        if sc:
            scores_text = (f"新鲜度 {sc['freshness_score']} · 可复现 {sc['repro_score']} · "
                           f"新颖度 {sc['novelty_total']}（{sc['paper_type']}）")
        else:
            scores_text = "（暂无评分）"
        print(f"[push] {aid} → 企业微信 …")
        ok, detail = wecom.push_paper(meta.get("title") or aid, row.get("area") or "?",
                                      summary, scores_text, full)
        conn = db.ensure(conn)  # 推送含 HTTP+sleep，记录前确保连接可用
        db.record_push(conn, aid, "success" if ok else "failed", str(detail)[:500])
        if ok:
            pushed += 1
            print(f"    -> 成功：{detail}")
        else:
            failed += 1
            print(f"    -> 失败：{detail}")
        time.sleep(4)  # 论文之间也间隔，配合 20/min 限流
    return {"assembled": len(full_by_id), "pushed": pushed, "failed": failed,
            "skipped": skipped, "full_by_id": full_by_id}


def _arg_int(name, default):
    for a in sys.argv[1:]:
        if a.startswith(name + "="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                pass
    return default


def main():
    limit = int(os.environ.get("DAILY_DIGEST_LIMIT", _arg_int("--digest-limit", DIGEST_LIMIT)))
    score_limit = _arg_int("--score-limit", SCORE_LIMIT)
    study_limit = _arg_int("--study-limit", STUDY_LIMIT)
    push_limit = _arg_int("--push-limit", PUSH_LIMIT)
    top30_mode = "--top30" in sys.argv
    top30_fetch = _arg_int("--top30-fetch", 30)
    top30_study = _arg_int("--top30-study", 6)

    conn = db.get_connection()
    db.ensure_schema(conn)  # 幂等，确保表就位

    # ---- Step 1: 抓取 + 入库（ingest）----
    print("== Step 1: 抓取 + 入库（ingest）==")
    papers = fetch_recent_papers(paper_filter.FETCH_QUERIES)
    print(f"[fetch] 抓取 {len(papers)} 篇（去重后）")
    new = 0
    for p in papers:
        db.upsert_paper(conn, p)                       # 写入共享 papers（冲突忽略）
        if db.upsert_daily_paper(conn, p["arxiv_id"]):  # 新建 daily_paper 行（阶段时间戳全 NULL）
            new += 1
    print(f"[ingest] 新登记 daily_paper {new} 篇；其余 {len(papers) - new} 篇已存在（增量，跳过）")

    # ---- Step 2: 筛选 backlog（仅未筛过的；甲→乙；结果落库）----
    print("\n== Step 2: 筛选 backlog（甲 → 乙，仅 filtered_at IS NULL）==")
    conn = db.ensure(conn)
    stats = run_filter(conn)

    print("\n== 筛选结果 ==")
    if stats.get("backlog", 0) == 0:
        print("  本轮无待筛论文（增量：backlog 为空，说明都筛过了）。")
    elif stats.get("aborted"):
        print("  本轮 stage-B 失败，未落库（backlog 保留）。")
    else:
        rel = stats["relevant_per_area"]
        print(f"  backlog {stats['backlog']}：候选 {stats['candidates']} / 甲未命中 {stats['noncandidates']}")
        print(f"  判定相关 {sum(rel.values())} 篇 — " + (", ".join(f"{a}:{c}" for a, c in rel.items()) or "无"))
        print(f"  not_relevant {stats['not_relevant']}，uncertain {stats['uncertain']}")

    # ---- Step 3（模式分支）----
    if top30_mode:
        # Top-N 榜单模式：打分 → 综评 → 排序 → 深读前 N → 概览 → 推送
        print(f"\n== Step 3: Top-{top30_fetch} 榜单（打分→综评→排序→深读前 {top30_study}→概览→推送）==")
        conn = db.ensure(conn)
        t = run_top30(conn, fetch_n=top30_fetch, study_top=top30_study)
        print("\n== Top 榜单结果 ==")
        if t.get("candidates", 0) == 0:
            print("  无候选（先确保有相关论文）。")
        else:
            p = t["pushed"]
            print(f"  候选 {t['candidates']} 篇；深读 {len(t['studied'])} 篇；概览 {t['overview_path']}")
            print(f"  推送：概览 {'成功' if p['overview'] else ('跳过' if p['skipped'] else '失败')}，"
                  f"深读文件成功 {p['studies']}，失败 {p['failed']}，已推过跳过 {p['skipped']}。")
        print("\n[done] Top 榜单：ingest → 筛选 → 打分 → 综评 → 排序 → 深读 → 概览 → 推送。")
        conn.close()
        return

    # ---- Step 3: 交付批次（一致化）：选批次 → 补齐 digest+score+deep-study → 组装 → 推送 ----
    # 三件产物围绕【同一批最新论文】：先定推送批次，再对这批补齐导读/打分，
    # 并对批次前 study_limit 篇做深度精读——避免深读跑到不在推送批次里的论文上。
    print(f"\n== Step 3: 交付批次（最新未推送，round-robin；推送上限 {push_limit}，"
          f"深读上限 {study_limit}）==")
    conn = db.ensure(conn)
    dl = run_deliverables(conn, push_limit, study_limit)
    print("\n== 交付结果 ==")
    if dl["batch"] == 0:
        print("  本轮无新可推送论文（增量：最新的相关论文都推过了）。")
    else:
        print(f"  批次 {dl['batch']} 篇（其中 {dl['studied']} 篇带深度精读）；"
              f"组装完整报告 {len(dl['assembled'])} 篇；"
              f"推送成功 {dl['pushed']}，失败 {dl['failed']}，已推过跳过 {dl['skipped']}，"
              f"缺评分延后 {dl.get('deferred', 0)}。")

    print("\n[done] 全流程：ingest → 筛选 → （同一批）导读+打分+深读 → 组装 → 推送"
          "（增量、去重、同批一致）。")
    conn.close()


if __name__ == "__main__":
    main()
