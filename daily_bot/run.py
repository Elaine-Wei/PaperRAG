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

# 筛选模块：甲（关键词粗召回）、乙（stage-B AI 分类）、共享 relay 调用
sys.path.insert(0, CRAWLER_DIR)  # 便于 paper_filter 复用 crawler/config.py
import paper_filter
import stage_b
import relay

# LLM relay（OpenAI 兼容）
RELAY_API_KEY = os.environ.get("RELAY_API_KEY")
RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", "https://a6.a6api.com/v1")
RELAY_MODEL = os.environ.get("RELAY_MODEL", "claude-fable-5")

# arXiv
ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_DELAY = 3.0  # arXiv 要求请求间隔 ≥ 3 秒
NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# 抓取参数：每个查询最多抓多少篇（给筛选留足材料）
FETCH_PER_QUERY = 30


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

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "categories": categories,
                "published": published,
                "pdf_url": pdf_url,
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

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "categories": categories,
                "published": published,
                "pdf_url": pdf_url,
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

def main():
    today = datetime.date.today().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not RELAY_API_KEY:
        print("[WARN] RELAY_API_KEY 未设置：导读步骤会失败并用摘要兜底。"
              "设置后重跑可得到真正的中文导读。")

    print("== Step 2: 抓取（拓宽后的查询集）==")
    papers = fetch_recent_papers(paper_filter.FETCH_QUERIES)
    print(f"[fetch] 共获得 {len(papers)} 篇（去重后）")
    if not papers:
        print("[ERROR] 没有抓到任何论文，退出。")
        return

    print("== Step 3: 甲（宽松粗召回）==")
    candidates = paper_filter.coarse_candidates(papers)
    print(f"[甲] 候选池 {len(candidates)} 篇交给 stage-B")
    if not candidates:
        print("[ERROR] 粗召回后没有候选，退出。")
        return
    # 候选池的 loose area 覆盖（一篇可带多个 tag）：确认小众方向也进了 stage-B
    area_cover = {}
    for p in candidates:
        for a in p.get("areas") or []:
            area_cover[a] = area_cover.get(a, 0) + 1
    cover_str = ", ".join(f"{a}:{area_cover.get(a, 0)}" for a in paper_filter.A_CLASS_ORDER)
    print(f"[甲] 候选池 loose 方向覆盖（可重叠）：{cover_str}")

    print("\n== Step 4: 乙（stage-B / LLM 分类）==")
    sb = stage_b.classify(candidates)
    if not sb["verdicts"]:
        # stage-B 整体失败（如无 key / relay 不可达）→ 回退到甲的关键词精选
        print("[WARN] stage-B 未产出任何判定，回退到甲的关键词精选。")
        report = paper_filter.select(papers)
        print(paper_filter.format_report(report, fetched_count=len(papers)))
        picked = report["selected"]
    else:
        print(stage_b.format_report(candidates, sb))
        final = paper_filter.final_select_by_area(candidates)
        print()
        print(paper_filter.format_final_selection(final))
        picked = final["selected"]

    if not picked:
        print("[ERROR] 筛选后没有任何论文可导读，退出。")
        return

    css = load_css()

    print("\n== Step 5: 导读 + HTML ==")
    written = []
    for paper in picked:
        tag = paper.get("stage_b_area") or (",".join(paper.get("areas") or []) or "fallback")
        print(f"[paper] {paper['arxiv_id']} [{tag}] — {paper.get('title')}")
        digest = generate_digest(paper)

        # 导读结果小结（成功 / 兜底、中英预览、token 用量）
        if digest.get("degraded"):
            print(f"[digest] {paper['arxiv_id']}: 失败，已用摘要兜底")
        else:
            zh, en = digest.get("zh", {}), digest.get("en", {})
            zh_prev = " / ".join(s for s in (zh.get("hook"), zh.get("problem")) if s)
            en_prev = " / ".join(s for s in (en.get("hook"), en.get("problem")) if s)
            print(f"[digest] {paper['arxiv_id']}: 成功（中英双语）")
            print(f"         zh: {zh_prev[:100]}")
            print(f"         en: {en_prev[:100]}")
            usage = digest.get("usage")
            if usage:
                print(f"[usage]  {paper['arxiv_id']}: {json.dumps(usage, ensure_ascii=False)}")
            else:
                print(f"[usage]  {paper['arxiv_id']}: relay 未返回 usage 字段")

        try:
            doc = render_html(paper, digest, css)
            safe_id = paper["arxiv_id"].replace("/", "_")
            path = os.path.join(OUTPUT_DIR, f"{today}_{safe_id}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(doc)
            written.append(path)
            print(f"[html] 已写出 {path}"
                  + ("（兜底：摘要）" if digest.get("degraded") else ""))
        except Exception as e:
            print(f"[WARN] 生成 HTML 失败 ({paper['arxiv_id']}): {e}")

    print("\n== 完成 ==")
    print(f"抓取 {len(papers)} 篇 → 挑选 {len(picked)} 篇 → 生成 {len(written)} 个 HTML：")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()
