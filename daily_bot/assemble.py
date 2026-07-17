#!/usr/bin/env python3
"""
assemble —— 把一篇论文的三件产物组装成一个自包含的完整报告 HTML：
  1) 简报框（digest：可视化头 + 问题/方法/意义，中英双语切换）
  2) 评分卡（score：雷达 + 子项 + 领域 + 论文类型徽章）
  3) 深度精读（deep-study，若已生成则附上，否则省略）

做法：直接从已生成的三份产物 HTML 里抽出 <body> 内容拼接（不重跑 LLM），
统一套 academic-html-skill 的 CSS + scorer 的 CSS + KaTeX。输出 {date}_{arxiv_id}_full.html。
"""

import datetime
import html
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
CSS_PATH = os.path.join(_PROJECT, "academic-html-skill", "unpacked",
                        "academic-html-article", "references", "full-css.css")
OUTPUT_DIR = os.path.join(_HERE, "output")

sys.path.insert(0, _HERE)
import db            # noqa: E402
import scorer        # noqa: E402  (复用其 CSS)
import katex_inline  # noqa: E402  (自包含 KaTeX head，离线可渲染)
import deep_study    # noqa: E402  (复用 SVG $ 清洗安全网)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _body_inner(txt):
    m = re.search(r"<body[^>]*>(.*)</body>", txt, re.S | re.I)
    return (m.group(1).strip() if m else txt.strip())


def extract_hook(digest_path):
    """从 digest HTML 里抽中文 hook（header-subtitle l-zh）作为推送摘要。"""
    txt = _read(digest_path)
    m = re.search(r'<p class="header-subtitle l-zh">(.*?)</p>', txt, re.S)
    if not m:
        m = re.search(r'<p class="header-subtitle[^"]*">(.*?)</p>', txt, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def _sep(label):
    return (f'<hr style="max-width:780px;margin:44px auto 0;border:none;'
            f'border-top:2px solid #c8c4be">'
            f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:0.8rem;'
            f'letter-spacing:0.12em;text-transform:uppercase;color:#c0392b;'
            f'text-align:center;margin:10px 0 4px">{html.escape(label)}</div>')


def assemble_full(conn, arxiv_id):
    """组装完整报告 HTML；至少需要 digest。返回文件路径，或 None（无 digest）。"""
    row = db.get_daily_row(conn, arxiv_id)
    if not row or not row.get("digest_path") or not os.path.exists(row["digest_path"]):
        return None
    metas = db.get_papers(conn, [arxiv_id])
    meta = metas[0] if metas else {"arxiv_id": arxiv_id, "title": arxiv_id}
    title = meta.get("title") or arxiv_id

    digest_inner = _body_inner(_read(row["digest_path"]))
    score_inner = ""
    if row.get("score_path") and os.path.exists(row["score_path"]):
        # 评分卡本身无 .container，包一层让它与其它部分同宽居中
        score_inner = f'<div class="container">{_body_inner(_read(row["score_path"]))}</div>'
    study_inner = ""
    if row.get("deep_study_path") and os.path.exists(row["deep_study_path"]):
        study_inner = _body_inner(_read(row["deep_study_path"]))

    parts = [_sep("简报 · Digest"), digest_inner,
             _sep("评分卡 · Score"),
             score_inner or '<div class="container"><p>（本篇暂无评分）</p></div>']
    if study_inner:
        parts.append(_sep("深度精读 · Deep Study"))
        parts.append(study_inner)
    body = "\n".join(parts)
    body = deep_study.strip_dollar_in_svg_text(body)  # 兜底清洗组合体里所有 SVG <text> 内的 $

    full_css = _read(CSS_PATH)
    katex_head = katex_inline.head_block()  # 自包含 KaTeX（css+base64字体+js+config），离线可渲染
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>完整报告 · {html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Libre+Baskerville:ital,wght@0,700;1,400&family=Noto+Sans+SC:wght@400;700&family=Noto+Serif+SC:wght@700&family=Source+Sans+3:wght@400;700&display=swap" rel="stylesheet">
{katex_head}
<style>
{full_css}
{scorer.CSS}
/* 组合页：scorer 的 body 规则只作用于 <body>，这里放开，让各 .container 统一居中/限宽 */
body{{max-width:none;margin:0;padding:0;background:#fff}}
</style></head><body class="show-zh">
{body}
</body></html>
"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()
    safe = arxiv_id.replace("/", "_")
    path = os.path.join(OUTPUT_DIR, f"{today}_{safe}_full.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
