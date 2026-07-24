#!/usr/bin/env python3
"""
deep_study —— 单篇论文的深度精读讲义生成器（独立能力，与每日 digest 无关）。

多趟（multi-pass）流水线（默认 gpt-5.6-luna，复用 relay.py）：
  Pass 1（大纲）：全文 + 检测到的原文小节标签 → 让模型规划「大教学章节」并映射到原文小节。
  Pass 2（逐节讲解）：按原文结构切块，对每个原文小节单独讲解，逐字保存。
  Pass 3（衔接）：对每个大章节，只让模型写 intro/outro/过渡句；正文由「代码」拼接 Pass-2
    的逐字讲解——LLM 输出永远不经手正文，从物理上杜绝改写/压缩/误差叠加。
  最终组装：代码统一编号 + 目录（杜绝乱序目录）、CSS/KaTeX、一句话总结、页脚免责声明。
若 PDF 结构无法可靠切分（toc→字体→正则 都失败），回退到单趟生成并高声告警。

指令保持极简（绝不教模型"通俗/加类比/加图"——它自己做得好，过度指令会跑题）。

用法： python daily_bot/deep_study.py <arxiv_id> [model] [--outline-only]
"""

import datetime
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# 复用 run.py（导入即加载 daily_bot/.env、arXiv 解析器）与共享 relay
import run
import relay
import katex_inline

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
CSS_PATH = os.path.join(PROJECT_ROOT, "academic-html-skill", "unpacked",
                        "academic-html-article", "references", "full-css.css")
OUTPUT_DIR = os.path.join(HERE, "output")
CHECKPOINT_DIR = os.path.join(OUTPUT_DIR, ".checkpoint")  # 断点续跑：每(论文,模型)一个 json

ARXIV_API_URL = "http://export.arxiv.org/api/query"
UA = {"User-Agent": "PaperRAG-deep-study/0 (mailto:elaine.wei@xpef.org)"}

# 全文字符上限（安全阀；正常论文远小于此，超出才截断并提示）。不担心 token 成本。
MAX_TEXT_CHARS = 600_000

# 模型：默认 gpt-5.6-luna（须能完整生成长的多趟输出而不被 relay 静默截断；
# claude-fable-5 在长输出时会被截断）。可用第二个位置参数 / 环境变量 DEEP_STUDY_MODEL 覆盖切换。
DEFAULT_MODEL = "gpt-5.6-luna"

# 输出上限：深度精读很长，放开到 60k。不同模型的实际上限可能不同——
# 若模型拒绝过大的 max_tokens（HTTP 400），按 OUTPUT_TOKENS_LADDER 自动降档重试。
OUTPUT_MAX_TOKENS = 60_000
OUTPUT_TOKENS_LADDER = [60_000, 32_000, 16_384, 8_192]


# ---------------------------------------------------------------------------
# 取元数据 / 下载 PDF / 抽全文
# ---------------------------------------------------------------------------

def fetch_metadata(arxiv_id):
    """按 id 查 arXiv，拿到 title/authors/abstract/pdf_url（复用 run 的 XML 解析）。"""
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(
        {"id_list": arxiv_id, "max_results": 1})
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    papers = run.parse_arxiv_xml(data)
    return papers[0] if papers else None


def download_pdf(pdf_url):
    req = urllib.request.Request(pdf_url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def extract_pdf_text(pdf_bytes):
    """用 PyMuPDF 抽全文，返回 (text, page_count)。"""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = [page.get_text() for page in doc]
    n = doc.page_count
    doc.close()
    return "\n".join(parts), n


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位顶级的论文讲解老师，擅长把艰深论文"泡开"讲给聪明的非专家听。
给你的是一篇论文的【完整正文】。请写一篇**长而通俗、循序渐进**的中文精读讲义 HTML，
目标是让一个有基础但没读过这篇论文的读者，能**快速、轻松地真正学懂**它的核心思想。

【风格：通俗 + 泡开（最重要）】
- 通俗易懂：用大白话讲直觉，大量使用**日常生活的类比和比喻**，把抽象概念落到具体画面上。
  像给聪明的朋友讲解，而不是写论文。专有名词保留英文，并在首次出现时用一句话解释。
- 泡开：不要只"复述"论文各节——要**展开、铺陈**。每个关键概念都遵循：
  先讲直觉/动机（为什么需要它）→ 它是什么 → 一步步推导/说明 → 洞见与工程启示。
  **要解释 WHY，而不只是 WHAT。**
- 深度与长度：对标一篇高质量的长篇讲义，**要比一般摘要长得多**，可以有很多章节、很多小节。
  宁可啰嗦讲透，不要惜字如金。公式要**一步步推导、不跳步**；给出**具体的 worked example /
  数值例子 / 反例构造**来印证结论。
- 允许补充：为帮助理解，你可以加入论文里没有的**说明性类比、例子、直觉**（这是通俗讲义，
  不必逐句忠于原文，严谨性以原论文为准）；但不要歪曲论文的核心主张与结论。

【慷慨使用可视化（只要有助理解就用，别怕多）】
- 概念 / 流程 / 几何直觉 / 对比关系，能画就画**内联 SVG**（<svg viewBox=...>），让读者"看见"。
  一篇好讲义常有多张图——该画就画，别吝啬（但也不要纯装饰）。
- 方法对比、优缺点、复杂度、假设 → 用**表格**。
- 有算法给伪代码 <div class="algo">；有定理就把**证明一步步写出来**
  <div class="theorem-block proof">，并在证明前用大白话讲清证明思路。
- 关键推导/操作步骤用 <ol class="steps">；重点/易错/结论用彩色 <div class="box ...">。
- 数学用 KaTeX（行内 $...$，独立 $$...$$，直接写 LaTeX）；公式可以多，但**每个公式旁都要有
  中文解释它在说什么**。

【可用组件 class（沿用）】
- <section id="s1"><h2><span class="section-num">1</span>标题</h2>…</section>，子节 <h3>。
- <div class="box info|success|danger"><div class="box-label">标签</div><p>…</p></div>
- <div class="theorem-block"><div class="theorem-label">THEOREM 1（名字）</div>…</div>
  / <div class="theorem-block proof"><div class="theorem-label">PROOF</div>…</div>
- <ol class="steps"><li>…</li></ol>；<table><thead>…</thead><tbody>…</tbody></table>
- <div class="algo"><div class="algo-label">ALGORITHM 1 — 名字</div>
  <span class="kw">for</span> … <span class="cm">// 注释</span><br>…</div>
- <span class="tag red">…</span> / <span class="tag green">…</span>
- 内联 <svg viewBox="0 0 720 320" style="max-width:680px">…</svg>，
  颜色用 #c0392b(红)/#16786a(teal)/#2c5aa0(蓝)/#faf8f5(暖底)/#1a1a1a(墨)。

【继续贴合这篇论文本身】结构由论文内容决定，不要套固定模板：有证明写证明、有算法给伪代码、
需要图画图、需要对比列表格；论文没有的不硬造。

【页面骨架】只输出 <div class="container"> … </div> 之间的内容：
1) <header class="header">：<div class="header-meta">领域 · 精读讲义</div>、<h1>标题</h1>、
   <p class="header-subtitle">一句话钩子</p>、
   <div class="paper-ref"><span class="paper-title">论文标题</span><br>作者 · arXiv 链接</div>
2) <nav class="toc"> 目录（章节要多，反映"泡开"后的结构）
3) 正文各 <section>（尽量泡开、够长、够多图表和例子）
4) 结尾 <div class="box success"> 一句话总结。
（页脚由程序自动添加，你**不用**写 footer。）

【输出要求】只输出上述 HTML（从 <div class="container"> 到 </div>）。
不要 <html>/<head>/<body>，不要 markdown 代码围栏(```），不要任何额外说明文字。"""


def build_user_prompt(meta, full_text):
    authors = ", ".join(meta.get("authors") or [])
    return (
        f"论文标题：{meta.get('title')}\n"
        f"arXiv:{meta.get('arxiv_id')}\n"
        f"作者：{authors}\n"
        f"链接：https://arxiv.org/abs/{meta.get('arxiv_id')}\n\n"
        f"===== 论文完整正文（PDF 抽取）=====\n{full_text}"
    )


# ---------------------------------------------------------------------------
# 清洗模型输出 + 套骨架
# ---------------------------------------------------------------------------

def _clean_body(content):
    """去掉可能的 ``` 围栏 / <html><head><body> 外壳，保留 container 内容。"""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # 若模型给了完整文档，取 body 内部
    m = re.search(r"<body[^>]*>(.*)</body>", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    return s


def load_css():
    try:
        with open(CSS_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] 读取 skill CSS 失败 ({CSS_PATH}): {e}")
        return ""


def wrap_document(meta, body_html, css, model=DEFAULT_MODEL):
    """套 skill 的 <head>（字体 + KaTeX auto-render + 内联 CSS）+ 程序生成的免责页脚。"""
    title = meta.get("title") or "论文精读"
    arxiv_id = meta.get("arxiv_id", "")
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    # 轻量页脚：原文链接（权威来源）+ 一行 AI 说明
    footer = (
        '<footer class="footer">\n'
        f'    <p>原始论文（权威来源）：<a href="{abs_url}">arXiv:{arxiv_id}</a></p>\n'
        f'    <p style="margin-top:6px;">本文为 AI 辅助生成的通俗讲义（模型：{model}），'
        '旨在帮助快速入门；细节与严谨性以原论文为准。</p>\n'
        '</footer>'
    )
    stripped = body_html.rstrip()
    if 'class="container"' in body_html and stripped.endswith("</div>"):
        # 结构完整：footer 放进 container 末尾那个 </div> 之前
        idx = stripped.rfind("</div>")
        body_html = stripped[:idx] + footer + "\n" + stripped[idx:]
    elif 'class="container"' in body_html:
        # 有 container 开头但结尾不干净（很可能被截断）：footer 追加到整段之后，
        # 绝不插进中间的某个内层 </div>，以免像之前那样把免责声明塞到正文里。
        body_html = body_html + "\n" + footer
    else:
        # 完全没有 container：自己包一层
        body_html = f'<div class="container">\n{body_html}\n{footer}\n</div>'
    # A2 安全网：消灭 SVG <text> 里无法渲染的 $...$（转 unicode）
    body_html = strip_dollar_in_svg_text(body_html)
    # A1 lint：统计正文里残留的未加 $ 的裸 unicode 数学（只告警，不改写）
    n_bare = count_undelimited_math(body_html)
    if n_bare:
        print(f"[study][lint] 正文残留疑似未加 $ 的裸 unicode 数学 {n_bare} 处（仅告警，未改写）")

    katex_head = katex_inline.head_block()  # 自包含 KaTeX（css+base64字体+js+config），离线可渲染
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>精读 · {title}</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Libre+Baskerville:ital,wght@0,700;1,400&family=Noto+Sans+SC:wght@400;700&family=Noto+Serif+SC:wght@700&family=Source+Sans+3:wght@400;700&display=swap" rel="stylesheet">

{katex_head}

<style>
{css}
</style>
</head>
<body>
{body_html}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 统计 / 主流程
# ---------------------------------------------------------------------------

def _stats(body_html):
    display = re.findall(r"\$\$.+?\$\$", body_html, re.S)
    inline = re.findall(r"(?<!\$)\$(?!\$)[^$\n]+?\$(?!\$)", body_html)
    # 段落体量：<p> 内文本（去标签）字符数、段数、超长段（>300 字符）数
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>", body_html, re.S | re.I)
    para_texts = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
    para_texts = [p for p in para_texts if p]
    prose_chars = sum(len(p) for p in para_texts)
    long_paras = sum(1 for p in para_texts if len(p) > 300)
    return {
        "sections": len(re.findall(r"<h2", body_html)),
        "subsections": len(re.findall(r"<h3", body_html)),
        "formulas": len(display) + len(inline),
        "formulas_display": len(display),
        "formulas_inline": len(inline),
        "svg": len(re.findall(r"<svg", body_html)),
        "tables": len(re.findall(r"<table", body_html)),
        "theorem_blocks": len(re.findall(r"theorem-block", body_html)),
        "algo_blocks": len(re.findall(r'class="algo"', body_html)),
        "step_lists": len(re.findall(r'class="steps"', body_html)),
        "callouts": len(re.findall(r'class="box', body_html)),
        "prose_chars": prose_chars,
        "paragraphs": len(para_texts),
        "long_paragraphs": long_paras,
    }


def _llm(system, user, model, temperature=0.6, max_tokens=32_000):
    """relay 调用 + max_tokens 降档重试（对付个别模型拒绝过大 max_tokens 的 400）。"""
    ladder = sorted({mt for mt in [max_tokens, 16_384, 8_192] if mt <= max_tokens},
                    reverse=True) or [max_tokens]
    last_err = None
    for mt in ladder:
        try:
            return relay.relay_chat(system, user, temperature=temperature,
                                    timeout=1800, max_tokens=mt, model=model)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code == 400 and "max_token" in detail.lower():
                print(f"[study] max_tokens={mt} 被拒（400），降档重试")
                last_err = e
                continue
            raise
    raise last_err


# ---------------------------------------------------------------------------
# 按原文结构切块（toc → 字体/加粗 → 正则 → 失败则回退单趟）
# ---------------------------------------------------------------------------

MAX_CHUNK_CHARS = 9_000
MIN_BODY_CHARS = 200   # 正文短于此的块视为空壳/假节
_SKIP_TITLE_RE = re.compile(r"^(references|bibliography|acknowledg|致谢|参考文献)", re.I)
# 假节的乞讨话术（Pass-2 收到空壳时模型会这么回；用于丢弃 + 完整性判定）
_BEG_RE = re.compile(r"请粘贴|请提供|暂未提供|目前只有标题|请补充|paste the|provide the (?:content|text)", re.I)


def _looks_fake_title(title):
    """标题像假节：纯/小数编号(0.93/2.1/.94)、全大写常量(APP_ID)、尾连字符碎片(LLM-)。"""
    t = (title or "").strip()
    if not t:
        return True
    if re.fullmatch(r"\d+(\.\d+)*", t):        # 0.93 / 2.1 / 3
        return True
    if re.fullmatch(r"0?\.\d+", t):            # .94
        return True
    if re.fullmatch(r"[A-Z0-9_]{2,}", t):      # APP_ID / LLM
        return True
    if t.endswith("-"):                        # LLM-
        return True
    return False


def _is_fake_chunk(c):
    """空壳/假节：正文过短 或 标题像假节。"""
    return len((c.get("text") or "").strip()) < MIN_BODY_CHARS or _looks_fake_title(c.get("title"))


def _merge_fakes(chunks):
    """
    在切块出生处清洗：丢弃 References 等；把空壳/假节（表号 0.93、常量 APP_ID、碎片 LLM-、空正文）
    的少量文字【并入前一块】而非单独成节——既不丢内容，又不把假节送进 Pass-2 引发乞讨话术。
    """
    out = []
    for c in chunks:
        if _SKIP_TITLE_RE.match(c["title"]):
            continue                                   # References/致谢：整块丢弃
        if _is_fake_chunk(c):
            if out:                                    # 并入前一真块（保留其少量正文）
                extra = (c.get("text") or "").strip()
                if extra:
                    out[-1]["text"] = (out[-1]["text"].rstrip() + "\n" + extra).strip()
            elif (c.get("text") or "").strip():        # 首块即假但有正文：暂留作种子（无处可并）
                out.append(dict(c))
            # 首块假且空正文：直接丢弃
        else:
            out.append(dict(c))
    return out
_NAMED_HEAD_RE = re.compile(
    r"^(abstract|introduction|related works?|background|preliminaries|"
    r"methodology|methods?|experiments?|evaluation|results?|discussion|"
    r"conclusions?|future work|references|bibliography|acknowledg|"
    r"appendix|appendices)\b", re.I)
# 纯编号行（"3"、"2.1"，无尾点）；编号+标题同一行；以及"定理/图/表…"前缀（排除误判）
_PURE_NUM_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")
_SINGLELINE_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,2})[.\s]+([A-Za-z(].{0,110})$")
_THM_PREV_RE = re.compile(
    r"^(theorem|lemma|corollary|proposition|definition|remark|example|"
    r"algorithm|figure|fig\.?|table|eq\.?|equation)\b", re.I)


def _extract_lines(doc):
    """逐行提取文本 + 字号/加粗（用于按排版检测标题）。"""
    lines = []
    for page in doc:
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                txt = "".join(s.get("text", "") for s in spans).strip()
                if not txt:
                    continue
                size = max((s.get("size", 0) for s in spans), default=0)
                flags = 0
                for s in spans:
                    flags |= s.get("flags", 0)
                lines.append({"text": txt, "size": round(size, 1),
                              "bold": bool(flags & 16)})
    return lines


def _modal_body_size(lines):
    from collections import Counter
    sizes = Counter(l["size"] for l in lines if len(l["text"]) >= 40)
    if not sizes:
        sizes = Counter(l["size"] for l in lines)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _label_depth(label):
    return label.count(".") + 1 if re.match(r"^\d", label) else 1


def _next_nonempty(lines, j):
    n = len(lines)
    while j < n and not lines[j]["text"].strip():
        j += 1
    return j


def _detect_headings_font(lines, body_size):
    """
    字体/加粗检测标题，返回记录 [{line, pos, label, title}]（pos = 正文起始行索引）。
    关键：支持"编号与标题分两行"的常见排版（如 '3' 一行、'DATASET AGGREGATION' 下一行）。
    """
    heads, consumed, n = [], set(), len(lines)
    for i, l in enumerate(lines):
        if i in consumed:
            continue
        t = l["text"].strip()
        emph = l["bold"] or l["size"] >= body_size + 0.5
        if not emph:
            continue
        # (1) 两行式：纯编号行 + 下一行标题
        if _PURE_NUM_RE.match(t):
            prev = lines[i - 1]["text"].strip() if i > 0 else ""
            if _THM_PREV_RE.match(prev):
                continue  # "Theorem 2.1." 这类，不是章节
            j = _next_nonempty(lines, i + 1)
            if j < n:
                title = lines[j]["text"].strip()
                if len(title) <= 80 and re.match(r"^[A-Z(]", title):
                    heads.append({"line": i, "pos": j + 1, "label": t, "title": title})
                    consumed.add(j)
                    continue
        # (2) 单行式：编号+标题同一行
        m = _SINGLELINE_NUM_RE.match(t)
        if m:
            heads.append({"line": i, "pos": i + 1,
                          "label": m.group(1), "title": m.group(2).strip()})
            continue
        # (3) 命名标题 / 全大写短标题（如 THEORETICAL ANALYSIS、FUTURE WORK）
        if len(t) <= 70:
            allcaps = (t == t.upper()) and (sum(c.isalpha() for c in t) >= 3)
            if _NAMED_HEAD_RE.match(t) or allcaps:
                heads.append({"line": i, "pos": i + 1, "label": t, "title": t})
    heads = [h for h in heads if h["line"] not in consumed]
    heads.sort(key=lambda h: h["line"])
    return heads


def _detect_headings_regex(lines):
    heads = []
    for i, l in enumerate(lines):
        t = l["text"].strip()
        m = _SINGLELINE_NUM_RE.match(t)
        if len(t) <= 120 and m:
            heads.append({"line": i, "pos": i + 1,
                          "label": m.group(1), "title": m.group(2).strip()})
    return heads


def _detect_headings_toc(toc, lines):
    """用 PDF 内嵌书签标题去 lines 里定位标题行，返回记录。"""
    if not toc:
        return []
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip().lower()
    ln = [norm(l["text"]) for l in lines]
    heads, used = [], set()
    for _lvl, title, _page in toc:
        t = norm(title)
        if not t:
            continue
        for i, x in enumerate(ln):
            if i in used:
                continue
            if x == t or (len(x) > 6 and (x.startswith(t) or t.startswith(x))):
                first = title.strip().split()[0] if title.strip() else title
                label = first if re.match(r"^\d", first) else title.strip()
                heads.append({"line": i, "pos": i + 1, "label": label,
                              "title": title.strip()})
                used.add(i)
                break
    heads.sort(key=lambda h: h["line"])
    return heads


def _build_chunks(lines, heads):
    """按标题记录切块，仅在 depth<=2 处断开（更深的并入父块）。"""
    bnd = [h for h in heads if _label_depth(h["label"]) <= 2]
    if len(bnd) < 3:
        return []
    chunks = []
    for k, h in enumerate(bnd):
        start = h["pos"]
        end = bnd[k + 1]["line"] if k + 1 < len(bnd) else len(lines)
        text = "\n".join(l["text"] for l in lines[start:end]).strip()
        chunks.append({"label": h["label"], "title": h["title"], "text": text})
    return chunks


def _subdivide(chunk):
    text = chunk["text"]
    if len(text) <= MAX_CHUNK_CHARS:
        return [chunk]
    paras = text.split("\n\n") if "\n\n" in text else text.split("\n")
    pieces, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > MAX_CHUNK_CHARS:
            pieces.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        pieces.append(cur)
    if len(pieces) <= 1:
        return [chunk]
    out = []
    for i, pc in enumerate(pieces):
        out.append({"label": chunk["label"] + chr(ord("a") + i),
                    "title": chunk["title"] + (f"（续{i + 1}）" if i else ""),
                    "text": pc})
    return out


def analyze_pdf(pdf_bytes):
    """返回 (full_text, pages, chunks|None, method)。chunks 为 None 表示切分失败→单趟回退。"""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    lines = _extract_lines(doc)
    pages = doc.page_count
    try:
        toc = doc.get_toc() or []
    except Exception:
        toc = []
    doc.close()

    full_text = "\n".join(l["text"] for l in lines)
    body_size = _modal_body_size(lines)

    # 切分方法阶梯：字体/加粗（含两行式，最稳）→ 内嵌书签 → 正则；取首个能给出 ≥3 个
    # depth≤2 边界的方法。
    def _ok(heads):
        return len([h for h in heads if _label_depth(h["label"]) <= 2]) >= 3

    heads = _detect_headings_font(lines, body_size)
    method = "font"
    if not _ok(heads):
        toc_heads = _detect_headings_toc(toc, lines)
        if _ok(toc_heads):
            heads, method = toc_heads, "toc"
    if not _ok(heads):
        rx_heads = _detect_headings_regex(lines)
        if _ok(rx_heads):
            heads, method = rx_heads, "regex"

    chunks = _build_chunks(lines, heads) if _ok(heads) else []
    # 出生处清洗：丢 References、把空壳/假节并入前一块（防止假节进 Pass-2）；再对超长块细分
    kept = []
    for c in _merge_fakes(chunks):
        kept.extend(_subdivide(c))
    if len(kept) < 3:
        return full_text, pages, None, "none"
    return full_text, pages, kept, method


# ---------------------------------------------------------------------------
# Pass 1 / 2 / 3（指令保持极简）
# ---------------------------------------------------------------------------

def pass1_outline(meta, full_text, chunks, model, enforce_all=False):
    label_list = "\n".join(f"- {c['label']}: {c['title']}" for c in chunks)
    coverage_rule = (
        "可以跳过不值得教的部分（例如没有实质方法/证明的附录）。\n"
        if not enforce_all else
        "【务必】把上面列出的【每一个】原文小节 label 都分配到某个大章节，不要遗漏任何一个 "
        "label（这些 label 已过滤掉假节，都是有实质内容的真章节）。\n")
    system = (
        "我是刚读完大二的 STEM 学生，第一次接触这个方向，想学懂这篇论文。"
        "请规划你会怎么教我：把下面列出的「原文小节」组织成若干个大教学章节，"
        "并说明每个大章节覆盖哪些原文小节（用给定的 label）。"
        + coverage_rule +
        "title、hook、takeaway 全部用【中文】。\n"
        '只输出 JSON：{"hook":"一句话中文副标题","takeaway":"一句话中文总结",'
        '"big_sections":[{"title":"中文大章节标题","section_labels":["1","2.1"]}]}。'
        "不要输出多余文字。")
    user = (f"论文标题：{meta.get('title')}\n\n原文小节列表：\n{label_list}\n\n"
            f"===== 论文完整正文 =====\n{full_text}")
    content, usage = _llm(system, user, model, temperature=0.4, max_tokens=8_000)
    return relay.extract_json(content) or {}, usage


def pass2_explain(chunk, model):
    system = (
        "我是刚读完大二的学生，想在几分钟内看懂这部分。请优先用图、表、要点列表来讲，"
        "文字只用来串联——能画图就画图，能列表就列表，能做对比就做表格。"
        "每段文字不超过 3 句话。不要长篇大论。\n"
        "【硬性规则】\n"
        "- 文字很贵、图表很便宜：每一部分都【默认】用一张 SVG 图 / 一个表格 / 一个要点列表来讲，"
        "prose 只用于把它们串起来。\n"
        "- 任何一段 <p> 不得超过 3 句话；更长的内容必须拆开，或改写成表格 / 列表 / 图。\n"
        "- 只要涉及【对比、顺序、结构、流程】→ 必须用表格 <table> 或图 <svg>，不许用大段文字。\n"
        "输出 HTML 片段（不是完整网页）：\n"
        "- 数学公式【必须】用 $...$（行内）或 $$...$$（独立），直接写 LaTeX；"
        "【不要】用 \\(...\\) 或 \\[...\\] 这类定界符。\n"
        "- 【所有数学一律 $LaTeX$】正文里、表格单元格 <td> 里、列表 <li> 里，只要出现数学符号"
        "（希腊字母、上/下标、根号、积分、≤/≥、期望 E[·]、矩阵等）就必须包进 $...$，"
        "严禁裸写 unicode/ASCII 伪数学。例：写 $\\tilde v$、$\\Sigma_t\\succeq 0$、"
        "$\\Lambda^*=M^{*-1}$、$E[\\tilde v\\mid Y]$；不要写 ṽ、ΣT≥0、Λ*=M*⁻¹、E[ṽ|Y]。\n"
        "- 【SVG 例外】<svg> 里的 <text> 标注【不能】用 $...$/LaTeX（KaTeX 无法在 SVG 内渲染，"
        "会原样显示成 $）；图里只放简短纯文本 + unicode 数学符号（A ≤ B、√、Σ、xᵢ、M⁻¹）；"
        "复杂公式不要塞进图里，放到图旁的 <p>/<div> 说明里（那里用 $...$ 会被正常渲染）。\n"
        "- 所有正文、表格单元格、图（SVG）里的文字标注【一律用中文】"
        "（专有名词可保留英文原词），不要整块切换成英文。\n"
        "多用 <table>、<ul>/<ol>、内联 <svg>（配色 #c0392b/#16786a/#2c5aa0/#faf8f5/#1a1a1a）、"
        "<div class=\"box info|success|danger\">；有算法用 <div class=\"algo\">，"
        "有定理/证明用 <div class=\"theorem-block\">/<div class=\"theorem-block proof\">。\n"
        "不要输出 <h1>/<h2>、不要写任何章节编号、不要写目录、不要 markdown 代码围栏。")
    # 守卫①：正文实质为空 → 不调 LLM（避免空壳引发乞讨话术）
    if len((chunk.get("text") or "").strip()) < MIN_BODY_CHARS:
        print(f"    [pass2][skip] {chunk.get('label')} 正文过短/空，跳过讲解")
        return "", None
    user = f"这一部分的标题：{chunk['title']}\n\n内容：\n{chunk['text']}"
    content, usage = _llm(system, user, model, temperature=0.6, max_tokens=16_000)
    frag = _clean_fragment(content)
    # 守卫②：输出含乞讨话术（模型没拿到实质内容）→ 丢弃该块
    if _BEG_RE.search(frag):
        print(f"    [pass2][discard] {chunk.get('label')} 输出含乞讨话术，丢弃")
        return "", usage
    return frag, usage


def pass3_connective(big_title, chunk_expls, model):
    """Plan B：模型只产出 intro/outro/过渡句；正文由代码拼接，绝不经手模型输出。"""
    n = len(chunk_expls)
    system = (
        "以下是同一个主题下若干部分的讲解，它们【已经写好，请勿改写、勿压缩、勿复述】。"
        "你只做两件事：(1) 写一个简短的开头引入 intro 和一个总结提升的结尾 outro；"
        "(2) 为相邻两部分之间各写一句自然的过渡 transition。\n"
        f'只输出 JSON：{{"intro":"...","outro":"...","transitions":[...]}}，'
        f"其中 transitions 的数量应为 {max(0, n - 1)}（相邻部分之间各一句；只有一个部分则为空数组）。"
        "intro/outro/transition 用 HTML 片段（可含 <p>）。切记不要重写或概括各部分内容。")
    user = "主题：" + (big_title or "") + "\n\n（以下各部分只读，供你了解衔接，不要改写）：\n\n"
    for i, (t, h) in enumerate(chunk_expls, 1):
        user += f"===== 部分{i}：{t} =====\n{h}\n\n"
    content, usage = _llm(system, user, model, temperature=0.5, max_tokens=8_000)
    data = relay.extract_json(content) or {}
    trans = data.get("transitions")
    trans = trans if isinstance(trans, list) else []
    return (data.get("intro", "") or "", data.get("outro", "") or "", trans, usage)


def pass_visuals(full_text, resolved, model):
    """
    视觉 Pass：给每个大章节配【至少一个视觉件】——一张 SVG 图 或 一个对比表格，
    目标 5-8 个（大致每章一个）。凡涉及方法/思路对比，务必用表格。
    返回 [{section:int, kind:'svg'|'table', caption:str, html:str}]。
    """
    sec_list = "\n".join(f"{i}. {bs['title']}" for i, bs in enumerate(resolved, 1))
    n = len(resolved)
    system = (
        "我是刚读完大二的学生，想靠图表快速看懂这篇论文。请为下面【每一个大章节】各做至少一个"
        "「视觉件」——要么一张 SVG 示意图（讲概念/流程/几何直觉），要么一个对比表格"
        f"（讲方法/思路/取舍的对比）。总共大约 {n}-{n + 3} 个，尽量每个大章节都有一个。\n"
        "【务必】只要论文里有方法/方案的对比（例如不同做法的假设、误差阶、是否需要专家、稳定性等），"
        "就做成一个对比表格 <table>，不要用文字描述对比。\n"
        "SVG 配色用：#c0392b(红) / #16786a(teal) / #2c5aa0(蓝) / #faf8f5(暖底) / #1a1a1a(墨)；"
        "建议 viewBox=\"0 0 720 320\"，带清晰文字标注。表格用普通 <table><thead>…<tbody>…。\n"
        "【SVG 文字硬性规则】<svg> 的 <text> 里只用简短纯文本 + unicode 数学符号"
        "（A ≤ B、√、Σ、xᵢ、M⁻¹），【绝不要】用 $...$ 或 LaTeX（KaTeX 无法在 SVG 内渲染，会显示成字面 $）；"
        "复杂公式不要放进图里。表格 <td> 里则相反——数学要用 $...$ LaTeX。\n"
        '只输出 JSON：{"visuals":[{"section":<章节编号,整数>,"kind":"svg"或"table",'
        '"caption":"中文图/表注","html":"<svg …></svg> 或 <table>…</table>"}]}。'
        "html 必须是完整闭合的 <svg> 或 <table>。不要 markdown 围栏、不要多余文字。")
    user = f"大章节列表：\n{sec_list}\n\n===== 论文完整正文 =====\n{full_text}"
    content, usage = _llm(system, user, model, temperature=0.5, max_tokens=32_000)

    out = []
    data = relay.extract_json(content)
    if isinstance(data, dict) and isinstance(data.get("visuals"), list):
        for d in data["visuals"]:
            if not isinstance(d, dict):
                continue
            frag = (d.get("html") or d.get("svg") or "").strip()
            low = frag.lower()
            ok = ("<svg" in low and "</svg>" in low) or ("<table" in low and "</table>" in low)
            if not ok:
                continue
            kind = "table" if "<table" in low else "svg"
            try:
                sec = int(d.get("section"))
            except (TypeError, ValueError):
                sec = 1
            out.append({"section": max(1, min(n, sec)), "kind": kind,
                        "caption": (d.get("caption") or "").strip(), "html": frag})
    if not out:
        # 兜底：JSON 坏了也从原始输出抢救 <svg>/<table> 块，按顺序分配到各章节
        blocks = re.findall(r"<svg\b.*?</svg>|<table\b.*?</table>", content or "",
                            re.S | re.I)
        for k, m in enumerate(blocks):
            kind = "table" if m.lower().startswith("<table") else "svg"
            out.append({"section": min(n, k + 1), "kind": kind,
                        "caption": "", "html": m.strip()})
    return out, usage


def _visual_figure(caption, frag):
    cap = ""
    if caption:
        cap = (f'<p style="text-align:center;color:var(--ink-caption);'
               f'font-size:0.9rem;margin-top:8px;">{html.escape(caption)}</p>')
    return (f'<div style="margin:26px 0;text-align:center;">{frag}{cap}</div>')


# ---------------------------------------------------------------------------
# 清洗片段 + 组装（编号/目录全部由代码掌控）
# ---------------------------------------------------------------------------

# --- A2 安全网：SVG <text> 内无法用 KaTeX，把 $...$ 去定界并把常见 LaTeX 记号转成 unicode ---
_SVG_TOKEN_MAP = [
    (r"\\mathbb\{R\}", "ℝ"), (r"\\mathbb\{Q\}", "ℚ"), (r"\\mathbb\{E\}", "𝔼"),
    (r"\\mathbb\{N\}", "ℕ"), (r"\\mathbb\{Z\}", "ℤ"),
    (r"\\leq\b", "≤"), (r"\\le\b", "≤"), (r"\\geq\b", "≥"), (r"\\ge\b", "≥"),
    (r"\\preceq\b", "⪯"), (r"\\succeq\b", "⪰"), (r"\\neq\b", "≠"), (r"\\approx\b", "≈"),
    (r"\\times\b", "×"), (r"\\cdot\b", "·"), (r"\\in\b", "∈"), (r"\\notin\b", "∉"),
    (r"\\infty\b", "∞"), (r"\\to\b", "→"), (r"\\mapsto\b", "↦"), (r"\\partial\b", "∂"),
    (r"\\nabla\b", "∇"), (r"\\sqrt\b", "√"), (r"\\sum\b", "Σ"), (r"\\int\b", "∫"),
    (r"\\prod\b", "∏"), (r"\\Sigma\b", "Σ"), (r"\\sigma\b", "σ"), (r"\\Lambda\b", "Λ"),
    (r"\\lambda\b", "λ"), (r"\\alpha\b", "α"), (r"\\beta\b", "β"), (r"\\gamma\b", "γ"),
    (r"\\delta\b", "δ"), (r"\\Delta\b", "Δ"), (r"\\mu\b", "μ"), (r"\\nu\b", "ν"),
    (r"\\top\b", "⊤"), (r"\\tilde\b", "~"), (r"\\dot\b", "˙"),
]


def _demath_for_svg(expr):
    """把一小段 LaTeX 尽量转成可读 unicode（用于 SVG <text>，那里不能跑 KaTeX）。"""
    for pat, rep in _SVG_TOKEN_MAP:
        expr = re.sub(pat, rep, expr)
    expr = re.sub(r"\\[a-zA-Z]+", "", expr)      # 丢弃未映射的命令（保留其花括号内的参数文本）
    expr = expr.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"\s+", " ", expr).strip()


def strip_dollar_in_svg_text(html_text):
    """把 <svg> 的 <text>…</text> 里的 $...$ 去定界并转 unicode——消灭图里字面 "$A" 的乱码。"""
    def fix_text(m):
        return re.sub(r"\$([^$]{1,160}?)\$", lambda mm: _demath_for_svg(mm.group(1)), m.group(0))
    return re.sub(r"<text\b[^>]*>.*?</text>", fix_text, html_text, flags=re.S | re.I)


# --- A1 lint：统计正文里未加 $ 的裸 unicode 伪数学（只告警、不改写，避免误伤） ---
_UNI_MATH = re.compile(r"[α-ωΑ-Ωϵϕφ∫∑∏√≤≥≠≈∞∈∉⊤⊥∇∂×÷±→↦⟨⟩⌊⌋⌈⌉⁻⁰¹²³ⁿₜₖₛᵀ̇̂̃]")


def count_undelimited_math(html_text):
    """统计【非 SVG、非 $ 内】的裸 unicode 数学符号数量（回归监控用；不修改内容）。"""
    t = re.sub(r"<svg.*?</svg>", " ", html_text, flags=re.S | re.I)   # 去掉 SVG（图里本就用 unicode）
    t = re.sub(r"<[^>]+>", " ", t)                                     # 去标签
    t = re.sub(r"\$\$.*?\$\$", " ", t, flags=re.S)                     # 去显示公式
    t = re.sub(r"\$[^$\n]{1,400}?\$", " ", t)                          # 去行内公式
    return len(_UNI_MATH.findall(t))


_MATH_SPAN = re.compile(r"\$\$.*?\$\$|\$[^$\n]*?\$", re.S)


def _escape_lt_gt_in_math(s):
    """
    数学定界符（$...$ / $$...$$）内的裸 < > 会被 HTML 解析器误当成标签起始（如 $A<B$
    里的 <B 被吃成标签），破坏 DOM、导致公式漏渲染成 "$A" 之类的乱码。
    在数学内把 < > 转义为 &lt; &gt;：浏览器把文本节点解码回 < >，KaTeX 照常渲染小于/大于号。
    """
    def esc(m):
        return m.group(0).replace("<", "&lt;").replace(">", "&gt;")
    return _MATH_SPAN.sub(esc, s)


def _balance_list_tags(s, tag):
    """丢弃无匹配的 </ul>/</ol>（模型偶尔多闭合，尤其列表项里嵌了 $$ 显示公式时），
    并为未闭合的 <ul>/<ol> 补齐闭合，保证片段内列表标签配平。"""
    depth = 0
    out = []
    last = 0
    for m in re.finditer(rf"<{tag}(?:\s[^>]*)?>|</{tag}>", s, re.I):
        out.append(s[last:m.start()])
        last = m.end()
        tok = m.group(0)
        if tok.lower().startswith(f"</{tag}"):
            if depth > 0:
                depth -= 1
                out.append(tok)
            # depth==0：无匹配的多余闭合 → 丢弃
        else:
            depth += 1
            out.append(tok)
    out.append(s[last:])
    if depth > 0:
        out.append(f"</{tag}>" * depth)  # 补齐未闭合
    return "".join(out)


def _clean_fragment(content):
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    m = re.search(r"<body[^>]*>(.*)</body>", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    # 剥掉模型擅自加的外层 section / h2 / 目录，以及 h3 里的手工编号
    s = re.sub(r"</?section[^>]*>", "", s)
    s = re.sub(r"<h2\b[^>]*>.*?</h2>", "", s, flags=re.S | re.I)
    s = re.sub(r'<nav class="toc".*?</nav>', "", s, flags=re.S | re.I)
    s = re.sub(r"(<h3\b[^>]*>)\s*\d+(?:\.\d+)*\.?\s*", r"\1", s, flags=re.I)
    # 中和片段里擅自加的 class="container"（会造成嵌套 container、正文被二次收窄）；
    # 只去掉这个 class，保留 <div> 本身以维持标签配平
    s = re.sub(r'\s*class="container"', "", s, flags=re.I)
    # 归一化数学定界符：\[...\] → $$...$$，\(...\) → $...$（防止某个块漂移成 \(\) 风格，
    # 与全文 $ 风格统一；即使 KaTeX 配置已容错，这里也做一层内容层归一化）
    s = re.sub(r"\\\[", "$$", s)
    s = re.sub(r"\\\]", "$$", s)
    s = re.sub(r"\\\(", "$", s)
    s = re.sub(r"\\\)", "$", s)
    # 数学内裸 < > 转义（防止 $A<B$ 被 HTML 解析器破坏成 "$A" 乱码）
    s = _escape_lt_gt_in_math(s)
    # 列表标签配平（丢弃多余 </ul>/</ol>，补齐未闭合）
    s = _balance_list_tags(s, "ul")
    s = _balance_list_tags(s, "ol")
    return s.strip()


def _assemble_big_section(idx, big_title, chunk_expls, intro, outro, trans, figures=None):
    esc = html.escape
    parts = [f'<section id="s{idx}">',
             f'<h2><span class="section-num">{idx}</span>{esc(big_title)}</h2>']
    if intro:
        parts.append(intro)
    # 视觉 Pass 分配到本章节的图/表：放在引入之后、正文之前，给读者一个总览
    for fig in (figures or []):
        parts.append(_visual_figure(fig.get("caption", ""), fig["html"]))
    for i, (t, h) in enumerate(chunk_expls):
        parts.append(f"<h3>{esc(t)}</h3>")
        parts.append(h)
        if i < len(chunk_expls) - 1 and i < len(trans) and trans[i]:
            parts.append(trans[i])
    if outro:
        parts.append(outro)
    parts.append("</section>")
    return "\n".join(parts)


def _build_body(meta, hook, takeaway, big_titles, sections_html):
    esc = html.escape
    title = meta.get("title") or ""
    arxiv_id = meta.get("arxiv_id", "")
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    authors = ", ".join((meta.get("authors") or [])[:12])
    header = (
        '<header class="header">\n'
        '<div class="header-meta">论文精读 · 教学讲义</div>\n'
        f"<h1>{esc(title)}</h1>\n"
        f'<p class="header-subtitle">{esc(hook)}</p>\n'
        f'<div class="paper-ref"><span class="paper-title">{esc(title)}</span><br>\n'
        f'{esc(authors)} · <a href="{abs_url}">arXiv:{esc(arxiv_id)}</a></div>\n'
        '</header>')
    toc = ('<nav class="toc">\n<div class="toc-label">目录</div>\n<ol>\n'
           + "\n".join(f'<li><a href="#s{i}">{esc(t)}</a></li>'
                       for i, t in enumerate(big_titles, 1))
           + '\n</ol>\n</nav>')
    takeaway_box = ""
    if takeaway:
        takeaway_box = ('<div class="box success">\n<div class="box-label">一句话总结</div>\n'
                        f"<p><strong>{esc(takeaway)}</strong></p>\n</div>")
    return ('<div class="container">\n' + header + "\n" + toc + "\n"
            + "\n".join(sections_html) + "\n" + takeaway_box + "\n</div>")


def _sum_usage(usages):
    tot = {"total_tokens": 0, "cost_usd": 0.0}
    for u in usages:
        if not u:
            continue
        tot["total_tokens"] += u.get("total_tokens", 0) or 0
        tot["cost_usd"] += u.get("cost_usd", 0.0) or 0.0
    return tot


# ---------------------------------------------------------------------------
# 多趟主流程 + 单趟回退 + 路由
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 断点续跑（checkpoint / resume）——中途 relay 失败不必从头重跑整篇
#   位置: output/.checkpoint/{safe_id}_{model_tag}.json（每 论文×模型 一个，跨重试稳定、不混淆 sol/terra）
#   指纹: 有序 (idx,label) 列表 + 全文长度 + 切分法 + 模型 → PDF 版本/切分/模型任一变化即弃档重来
#   粒度: Pass-1 大纲、视觉件、每个小节 Pass-2 讲解(按 ordinal idx 键)、每个大章 Pass-3 衔接，逐步落盘
#   清档: 仅在整篇成功 assemble+写 HTML 后删除；任何 pass 抛错 → 保留档，供下次 resume
# ---------------------------------------------------------------------------
def _cp_path(arxiv_id, model):
    safe = (arxiv_id or "").replace("/", "_")
    tag = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    return os.path.join(CHECKPOINT_DIR, f"{safe}_{tag}.json")


def _pdf_version(pdf_url):
    m = re.search(r"v(\d+)(?:\.pdf)?$", pdf_url or "")
    return "v" + m.group(1) if m else None


def _fingerprint(chunks, text_chars, split_method, model):
    """PDF/切分/模型 指纹：按 ordinal 键，任一变化即失配 → 弃旧档全新生成。"""
    basis = (json.dumps([[c.get("_idx"), c.get("label")] for c in chunks], ensure_ascii=False)
             + f"|{text_chars}|{split_method}|{model}")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _cp_load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None       # 缺失/损坏 → 视作无档，全新生成


def _cp_save(path, cp):
    """原子写（tmp+replace）。失败仅告警、不影响生成——最多失去续跑能力。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cp, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[study][WARN] checkpoint 保存失败（{str(e)[:60]}）——不影响本篇生成，仅失去断点续跑")


def _cp_delete(path):
    try:
        os.remove(path)
    except Exception:
        pass


def generate_multipass(meta, full_text, pages, chunks, css, model,
                       split_method, outline_only=False, pdf_version=None):
    arxiv_id = meta.get("arxiv_id", "")
    usages = []

    # 每个 chunk 标注全局序号（稳定唯一键，规避 label 可能重名）
    for i, c in enumerate(chunks):
        c["_idx"] = i
    fp = _fingerprint(chunks, len(full_text), split_method, model)
    cp_path = _cp_path(arxiv_id, model)
    _cp = _cp_load(cp_path)
    resume = bool(_cp and _cp.get("fingerprint") == fp)
    if _cp and not resume:
        print("[study] checkpoint 与当前 PDF/切分/模型不匹配 → 弃旧档，全新生成")
        _cp_delete(cp_path)
        _cp = None
    if not resume:
        _cp = {"cp_version": 1, "arxiv_id": arxiv_id, "pdf_version": pdf_version,
               "model": model, "split_method": split_method, "n_chunks": len(chunks),
               "fingerprint": fp, "pass1": None, "visuals": None,
               "explanations": {}, "connective": {}}
    by_idx = {c["_idx"]: c for c in chunks}

    by_label = {c["label"]: c for c in chunks}
    total = len(chunks)

    def _resolve(bigs):
        res, us = [], set()
        for bs in bigs:
            title = (bs.get("title") or "（无标题）").strip()
            cs = []
            for l in [str(x).strip() for x in (bs.get("section_labels") or [])]:
                if l in by_label and l not in us:
                    cs.append(by_label[l])
                    us.add(l)
            if cs:
                res.append({"title": title, "chunks": cs})
        return res, us

    # ---- Pass 1（大纲）：有匹配 checkpoint 则复用，否则跑覆盖率守卫循环并落盘 ----
    if resume and _cp.get("pass1"):
        p1 = _cp["pass1"]
        hook, takeaway = p1.get("hook", ""), p1.get("takeaway", "")
        resolved = [{"title": bs["title"],
                     "chunks": [by_idx[i] for i in bs["section_idx"] if i in by_idx]}
                    for bs in p1.get("big_sections", [])]
        resolved = [bs for bs in resolved if bs["chunks"]]
        used_idx = {c["_idx"] for bs in resolved for c in bs["chunks"]}
        excluded = [c["label"] for c in chunks if c["_idx"] not in used_idx]
        print(f"[study] resume：复用 Pass-1 大纲（{len(resolved)} 大章），跳过 Pass-1")
    else:
        print(f"[study] Pass 1（大纲）… 结构切分方法={split_method}，原文小节 {len(chunks)} 块")
        # Pass-1 覆盖率守卫：假节已过滤，剩下多为真节 → 要求覆盖 ≥60%；不足则重试并强制“每个 label 都分配”，取最佳一次
        hook = takeaway = ""
        resolved, used, best = [], set(), None
        for attempt in range(3):
            data, u = pass1_outline(meta, full_text, chunks, model, enforce_all=(attempt > 0))
            usages.append(u)
            hook = data.get("hook", "") or hook
            takeaway = data.get("takeaway", "") or takeaway
            resolved, used = _resolve(data.get("big_sections") or [])
            frac = (len(used) / total) if total else 0.0
            print(f"[study] Pass-1 覆盖：映射 {len(used)}/{total} 小节（{frac:.0%}）"
                  + ("" if attempt == 0 else f"，第{attempt + 1}次已强制全覆盖"))
            if best is None or len(used) > len(best[1]):
                best = (resolved, used, hook, takeaway)
            if resolved and frac >= 0.6:
                break
            if attempt < 2:
                print("[study] Pass-1 覆盖不足 60%，重试（要求每个真节 label 都分配到某章）…")
        resolved, used, hook, takeaway = best   # 取覆盖最好的一次
        excluded = [c["label"] for c in chunks if c["label"] not in used]

        # ---- 检查点：先打印 Pass-1 大纲，供人工核对 ----
        print("\n================= Pass-1 教学大纲（请核对是否合理）=================")
        print(f"hook    : {hook}")
        print(f"takeaway: {takeaway}")
        for i, bs in enumerate(resolved, 1):
            labs = ", ".join(c["label"] for c in bs["chunks"])
            print(f"  [大章节 {i}] {bs['title']}")
            print(f"             覆盖原文小节: {labs}")
        if excluded:
            print(f"  (Pass-1 主动略过的小节: {', '.join(excluded)})")
        print("==================================================================\n")

        _cp["pass1"] = {"hook": hook, "takeaway": takeaway,
                        "big_sections": [{"title": bs["title"],
                                          "section_idx": [c["_idx"] for c in bs["chunks"]]}
                                         for bs in resolved],
                        "excluded": excluded}
        _cp_save(cp_path, _cp)

    if outline_only:
        return {"arxiv_id": arxiv_id, "title": meta.get("title"), "model": model,
                "mode": "outline-only", "split_method": split_method,
                "outline": resolved, "excluded": excluded, "hook": hook,
                "takeaway": takeaway, "usage": _sum_usage(usages)}

    if not resolved:
        raise RuntimeError("Pass-1 未产出任何可用大章节，无法继续")

    # ---- 视觉 Pass：有 checkpoint 则复用，否则跑并落盘 ----
    if resume and _cp.get("visuals") is not None:
        visuals = _cp["visuals"]
        print(f"[study] resume：复用视觉件 {len(visuals)} 个，跳过视觉 Pass")
    else:
        print("[study] 视觉 Pass（每章配图/表，对比一律做表格）…")
        visuals, ud = pass_visuals(full_text, resolved, model)
        usages.append(ud)
        _cp["visuals"] = visuals
        _cp_save(cp_path, _cp)
    figs_by_sec = {}
    for d in visuals:
        figs_by_sec.setdefault(d["section"], []).append(d)
    n_svg_v = sum(1 for d in visuals if d["kind"] == "svg")
    n_tbl_v = sum(1 for d in visuals if d["kind"] == "table")
    print(f"    -> 生成 {len(visuals)} 个视觉件（图 {n_svg_v} + 表 {n_tbl_v}）"
          + (f"，覆盖章节 {sorted(figs_by_sec)}" if visuals else "（无）"))

    # ---- Pass 2（逐节讲解）：按 ordinal idx 断点续跑，已讲解的跳过；每讲完一节落盘 ----
    saved_expl = _cp["explanations"]
    total_chunks = sum(len(b["chunks"]) for b in resolved)
    if resume and saved_expl:
        print(f"[study] resume：Pass-2 已完成 {len(saved_expl)}/{total_chunks} 小节，续跑剩余")
    print("[study] Pass 2（逐节讲解，顺序执行）…")
    for bs in resolved:
        for c in bs["chunks"]:
            k = str(c["_idx"])
            if k in saved_expl:
                c["explanation"] = saved_expl[k]     # 复用，跳过 LLM
                continue
            print(f"    - 讲解 {c['label']} · {c['title'][:40]}")
            c["explanation"], u = pass2_explain(c, model)
            usages.append(u)
            saved_expl[k] = c["explanation"]
            _cp_save(cp_path, _cp)

    print("[study] Pass 3（衔接：模型只写 intro/outro/过渡，正文由代码拼接）…")
    saved_conn = _cp["connective"]
    big_titles, sections_html = [], []
    out_idx = 0
    for orig_i, bs in enumerate(resolved, 1):
        # 只保留有实质讲解的小节（被 Pass-2 守卫跳过/丢弃的空壳不进正文）；整章为空则不产出该 section
        chunk_expls = [(c["title"], c["explanation"]) for c in bs["chunks"]
                       if (c.get("explanation") or "").strip()]
        if not chunk_expls:
            print(f"    - [章] {bs['title'][:36]} 无有效讲解，跳过")
            continue
        out_idx += 1
        ck = str(orig_i)
        if ck in saved_conn:                          # 复用衔接，跳过 LLM
            cd = saved_conn[ck]
            intro, outro, trans = cd["intro"], cd["outro"], cd["trans"]
        else:
            intro, outro, trans, u = pass3_connective(bs["title"], chunk_expls, model)
            usages.append(u)
            saved_conn[ck] = {"intro": intro, "outro": outro, "trans": trans}
            _cp_save(cp_path, _cp)
        sections_html.append(
            _assemble_big_section(out_idx, bs["title"], chunk_expls, intro, outro, trans,
                                  figures=figs_by_sec.get(orig_i, [])))  # 图按原章序取，编号连续
        big_titles.append(bs["title"])

    body = _build_body(meta, hook, takeaway, big_titles, sections_html)
    doc = wrap_document(meta, body, css, model=model)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()
    safe_id = arxiv_id.replace("/", "_")
    model_tag = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    path = os.path.join(OUTPUT_DIR, f"{today}_{safe_id}_study_mp_{model_tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

    _cp_delete(cp_path)   # 仅整篇成功 assemble+写 HTML 后清档；任何 pass 抛错都会保留档以便续跑

    return {
        "arxiv_id": arxiv_id, "title": meta.get("title"), "model": model,
        "mode": "multi-pass", "split_method": split_method, "pages": pages,
        "text_chars": len(full_text), "abstract_chars": len(meta.get("abstract") or ""),
        "hook": hook, "takeaway": takeaway, "outline": resolved, "excluded": excluded,
        "n_big_sections": len(resolved),
        "n_chunks_explained": sum(len(b["chunks"]) for b in resolved),
        "n_visuals": len(visuals),
        "finished_clean": body.rstrip().endswith("</div>"),
        "usage": _sum_usage(usages), "path": path,
        "doc_bytes": len(doc.encode("utf-8")), "doc_lines": doc.count("\n") + 1,
        "body_chars": len(body), "stats": _stats(body),
    }


def generate_single_pass(meta, text, pages, css, model):
    """回退路径：结构切分失败时的单趟生成（原实现）。"""
    print(f"[study][WARN] 结构切分失败，回退到单趟生成（模型：{model}）。")
    content, usage = _llm(SYSTEM_PROMPT, build_user_prompt(meta, text), model,
                          temperature=0.6, max_tokens=OUTPUT_MAX_TOKENS)
    body = _clean_body(content)
    finished_clean = body.rstrip().endswith("</div>")
    if not finished_clean:
        print("[study][WARN] 输出疑似被截断：body 未以 </div> 干净收尾。footer 已安全追加到末尾，"
              "但内容可能不完整——请勿直接采用。")
    doc = wrap_document(meta, body, css, model=model)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()
    safe_id = meta.get("arxiv_id", "").replace("/", "_")
    model_tag = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    path = os.path.join(OUTPUT_DIR, f"{today}_{safe_id}_study_{model_tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return {
        "arxiv_id": meta.get("arxiv_id"), "title": meta.get("title"), "model": model,
        "mode": "single-pass", "split_method": "none", "pages": pages,
        "text_chars": len(text), "abstract_chars": len(meta.get("abstract") or ""),
        "finished_clean": finished_clean, "usage": usage, "path": path,
        "doc_bytes": len(doc.encode("utf-8")), "doc_lines": doc.count("\n") + 1,
        "body_chars": len(body), "stats": _stats(body),
        "n_big_sections": None, "n_chunks_explained": None,
        "outline": None, "excluded": None,
    }


def generate_study(arxiv_id, model=DEFAULT_MODEL, outline_only=False):
    print(f"[study] 取元数据 {arxiv_id} …（模型：{model}）")
    meta = fetch_metadata(arxiv_id)
    if not meta:
        raise RuntimeError(f"arXiv 查不到 {arxiv_id}")
    pdf_url = meta.get("pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}"
    print(f"[study] 下载 PDF {pdf_url} …")
    pdf_bytes = download_pdf(pdf_url)
    full_text, pages, chunks, method = analyze_pdf(pdf_bytes)
    full_text = full_text.strip()
    print(f"[study] PDF {pages} 页，全文 {len(full_text)} 字符"
          f"（摘要仅 {len(meta.get('abstract') or '')} 字符）；结构切分={method}")

    if len(full_text) > MAX_TEXT_CHARS:
        full_text = full_text[:MAX_TEXT_CHARS]
        print(f"[study] 全文超过 {MAX_TEXT_CHARS} 字符，已截断（仅极长论文才会触发）")

    css = load_css()
    if chunks:
        return generate_multipass(meta, full_text, pages, chunks, css, model,
                                  method, outline_only=outline_only,
                                  pdf_version=_pdf_version(pdf_url))
    if outline_only:
        print("[study] 结构切分失败，无法只出大纲。")
        return {"arxiv_id": arxiv_id, "title": meta.get("title"), "model": model,
                "mode": "outline-only-failed", "split_method": "none"}
    return generate_single_pass(meta, full_text, pages, css, model)


def main():
    args = [a for a in sys.argv[1:]]
    outline_only = "--outline-only" in args
    args = [a for a in args if a != "--outline-only"]
    if not args:
        print("用法: python daily_bot/deep_study.py <arxiv_id> [model] [--outline-only]")
        sys.exit(1)
    arxiv_id = args[0].strip()
    model = args[1].strip() if len(args) > 1 else \
        os.environ.get("DEEP_STUDY_MODEL", DEFAULT_MODEL)

    r = generate_study(arxiv_id, model=model, outline_only=outline_only)

    if r.get("mode", "").startswith("outline-only"):
        print("\n== 仅大纲模式完成 ==")
        print(f"model: {r.get('model')}  split={r.get('split_method')}  "
              f"usage={r.get('usage')}")
        return

    st = r["stats"]
    print("\n== 精读生成完成 ==")
    print(f"arxiv_id     : {r['arxiv_id']}")
    print(f"model        : {r['model']}   mode: {r['mode']}   split: {r['split_method']}")
    print(f"title        : {r['title']}")
    print(f"size         : {r['doc_bytes']//1024} KB / {r['doc_lines']} lines")
    if r["finished_clean"]:
        print("finished OK  : True（body 以 </div> 干净收尾 → 未被截断）")
    else:
        print("finished OK  : False  ⚠️⚠️  警告：输出疑似被截断，请勿直接采用！")
    if r["mode"] == "multi-pass":
        print(f"big sections : {r['n_big_sections']}   原文小节讲解数: {r['n_chunks_explained']}"
              + (f"   略过: {', '.join(r['excluded'])}" if r.get("excluded") else ""))
    print(f"PDF pages    : {r['pages']}")
    print(f"full text    : {r['text_chars']} 字符（摘要仅 {r['abstract_chars']} 字符 → 确证读了全文）")
    print(f"sections(h2) : {st['sections']}  subsections(h3): {st['subsections']}")
    print(f"PROSE        : {st['prose_chars']} 字符 / {st['paragraphs']} 段（其中 >300 字符的长段 {st['long_paragraphs']} 个）")
    print(f"VISUALS      : SVG {st['svg']} + 表格 {st['tables']} = {st['svg'] + st['tables']} 个视觉件")
    print(f"formulas     : {st['formulas']}（display {st['formulas_display']} + inline {st['formulas_inline']}）")
    print(f"theorem-blocks: {st['theorem_blocks']}", end="   ")
    print(f"algo blocks  : {st['algo_blocks']}   step-lists: {st['step_lists']}   callouts: {st['callouts']}")
    if r["usage"]:
        print(f"usage        : {r['usage']}")
    print(f"output       : {r['path']}")


if __name__ == "__main__":
    main()
