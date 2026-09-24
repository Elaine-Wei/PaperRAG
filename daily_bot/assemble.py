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
import wecom         # noqa: E402  (复用 format_scores 群消息格式)


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
        # 若深读已在顶部注入过评分卡（top-30 模式），这里剥掉，避免完整报告里评分卡重复
        study_inner = re.sub(r"<!--SCORECARD-INJECTED-->.*?<!--/SCORECARD-INJECTED-->",
                             "", study_inner, flags=re.S)

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


_OV_CSS = """
.ov-wrap{max-width:820px;margin:0 auto;padding:24px 20px 60px}
.ov-title{font-family:'Libre Baskerville','Noto Serif SC',serif;font-size:1.5rem;margin:0 0 4px}
.ov-sub{color:#777;font-size:.9rem;margin-bottom:20px}
.ov-entry{border:1px solid #e0ddd8;border-radius:10px;padding:14px 18px;margin:14px 0;background:#fff}
.ov-rank{display:inline-block;min-width:1.8em;color:#c0392b;font-weight:700;font-family:'IBM Plex Mono',monospace}
.ov-head{font-size:1.05rem;font-weight:700;line-height:1.5}
.ov-badge{display:inline-block;background:#c0392b;color:#fff;border-radius:10px;padding:1px 9px;
font-size:.72rem;margin-left:8px;vertical-align:middle}
.ov-meta{color:#4a4a4a;font-size:.92rem;margin:4px 0}
.ov-comp{font-family:'IBM Plex Mono',monospace;color:#16786a;font-weight:700}
.ov-hook{color:#333;font-size:.95rem;margin:6px 0}
.ov-scores{font-family:'IBM Plex Mono',monospace;font-size:.82rem;color:#555;background:#faf8f5;
border-radius:6px;padding:8px 10px;margin:6px 0;line-height:1.6}
.ov-reason{font-size:.92rem;color:#1a1a1a;margin-top:6px}
.ov-reason b{color:#c0392b}
.ov-board{margin-top:30px}
.ov-board-title{font-family:'Noto Serif SC','Libre Baskerville',serif;font-size:1.25rem;
border-bottom:2px solid #e0ddd8;padding-bottom:8px;margin:26px 0 12px}
.ov-board-sub{color:#777;font-size:.86rem;margin:-4px 0 12px}
"""


def prepend_score_card_to_study(study_path, score_path):
    """
    把评分卡插到深读 HTML 顶部（第0部分），让每篇精读开门见山先看本篇 5 维雷达+综评。
    复用已渲染好的评分卡（score_path 的 <body> 内容）+ 注入 scorer.CSS。幂等（marker 包裹，
    可被 assemble_full 剥离以免完整报告重复）。评分数字不变，只是前置呈现。返回 True 成功/已注入。
    """
    if not (study_path and os.path.exists(study_path)):
        return False
    h = _read(study_path)
    if "SCORECARD-INJECTED" in h:
        return True  # 幂等：已注入
    if not (score_path and os.path.exists(score_path)):
        return False
    score_body = deep_study.strip_dollar_in_svg_text(_body_inner(_read(score_path)))
    block = ('<!--SCORECARD-INJECTED-->' + _sep("本篇评分卡 · Score")
             + f'<div class="container">{score_body}</div>'
             + '<!--/SCORECARD-INJECTED-->')
    # 注入评分卡样式 + 中和 scorer 的 body 宽度规则（沿用组合页做法，避免整页被收窄到 660px）
    if "</style>" in h:
        h = h.replace("</style>", scorer.CSS +
                      "\nbody{max-width:none;margin:0;padding:0;background:#fff}\n</style>", 1)
    m = re.search(r'(<div class="container"[^>]*>)', h)
    if m:
        h = h[:m.end()] + "\n" + block + "\n" + h[m.end():]
    else:
        h = re.sub(r"(<body[^>]*>)", r"\1\n" + block, h, count=1)
    with open(study_path, "w", encoding="utf-8") as f:
        f.write(h)
    return True


def _hook_for(row, meta):
    """概览一行 hook：优先用已有 digest 的中文 hook，否则退回摘要首句（截断）。"""
    if row and row.get("digest_path") and os.path.exists(row["digest_path"]):
        hk = extract_hook(row["digest_path"])
        if hk:
            return hk
    abs_ = (meta.get("abstract") or "").strip().replace("\n", " ")
    return (abs_[:90] + "…") if len(abs_) > 90 else abs_


def assemble_overview(conn, ids_sorted, studied_ids, date_str=None, prev_studied=None,
                      monthly_rows=None, classic_rows=None):
    """
    组装【滚动窗口榜单】概览单文件：每篇一行 = 群消息格式(format_scores) + 综评分 + 综评理由，
    按 ids_sorted（综评降序）呈现。今日新精读标 ★今日精读；此前已精读标 ✓已精读。
    """
    date_str = date_str or datetime.date.today().isoformat()
    studied = set(studied_ids or [])
    prev = set(prev_studied or []) - studied   # 之前已精读（与今日新精读不重叠）
    metas = {m["arxiv_id"]: m for m in db.get_papers(conn, list(ids_sorted))}

    # 名为 top30：完整排名留在 DB，但【交付的概览只展示前 30】。
    # 例外：已精读的论文（今日★/此前✓）即使排名在 30 名外也保留其条目（不丢深读链接/徽标），排名号仍为真实名次。
    DISPLAY_CAP = 30
    total = len(ids_sorted)
    shown = min(DISPLAY_CAP, total)
    extra_studied = [a for i, a in enumerate(ids_sorted, 1)
                     if i > DISPLAY_CAP and (a in studied or a in prev)]

    entries = []
    for i, aid in enumerate(ids_sorted, 1):
        if i > DISPLAY_CAP and not (aid in studied or aid in prev):
            continue   # 30 名外且非精读 → 不进概览（但仍在 DB 完整排名里）
        meta = metas.get(aid, {"arxiv_id": aid, "title": aid})
        row = db.get_daily_row(conn, aid) or {}
        sc = db.get_score(conn, aid)
        title = html.escape(meta.get("title") or aid)
        area = html.escape(row.get("area") or "?")
        hook = html.escape(_hook_for(row, meta))
        scores_html = html.escape(wecom.format_scores(sc)).replace("\n", "<br>") if sc else "（暂无评分）"
        comp = sc.get("composite_score") if sc else None
        creason = html.escape((sc.get("composite_reason") or "") if sc else "")
        comp_txt = f"{float(comp):.1f}/10" if comp is not None else "N/A"
        if aid in studied:
            badge = '<span class="ov-badge">★今日精读</span>'
        elif aid in prev:
            badge = '<span class="ov-badge" style="background:#16786a">✓已精读</span>'
        else:
            badge = ""
        entries.append(
            f'<div class="ov-entry">'
            f'<div class="ov-head"><span class="ov-rank">#{i}</span>📄 {title}{badge}</div>'
            f'<div class="ov-meta">方向 <b>{area}</b> · 综评 <span class="ov-comp">{comp_txt}</span> · '
            f'<a href="https://arxiv.org/abs/{html.escape(aid)}">arXiv:{html.escape(aid)}</a></div>'
            f'<div class="ov-hook">{hook}</div>'
            f'<div class="ov-scores">{scores_html}</div>'
            + (f'<div class="ov-reason"><b>入选理由：</b>{creason}</div>' if creason else "")
            + '</div>')

    full_css = _read(CSS_PATH)
    katex_head = katex_inline.head_block()
    weekly_body = deep_study.strip_dollar_in_svg_text("\n".join(entries))

    def board_card(rank, aid, title, published, score, meta_text, reason=""):
        score_txt = f"{float(score):.1f}/10" if score is not None else "N/A"
        return (f'<div class="ov-entry">'
                f'<div class="ov-head"><span class="ov-rank">#{rank}</span>📄 '
                f'{html.escape(title or aid)}</div>'
                f'<div class="ov-meta">{html.escape(meta_text)} · 综评 '
                f'<span class="ov-comp">{score_txt}</span> · '
                f'<a href="https://arxiv.org/abs/{html.escape(aid)}">arXiv:{html.escape(aid)}</a></div>'
                + (f'<div class="ov-reason">{html.escape(reason)}</div>' if reason else "")
                + '</div>')

    monthly_rows = list(monthly_rows or [])
    classic_rows = list(classic_rows or [])
    monthly_cards = []
    for rank, item in enumerate(monthly_rows[:10], 1):
        aid = item.get("arxiv_id")
        meta = next((m for m in db.get_papers(conn, [aid]) if m.get("arxiv_id") == aid), {})
        monthly_cards.append(board_card(
            rank, aid, meta.get("title") or aid, item.get("published"), item.get("composite"),
            f"发表 {item.get('published')} · 方向 {item.get('area') or '?'}"))

    classic_cards = []
    for rank, item in enumerate(classic_rows[:10], 1):
        aid, title, published, score, citations, active, percentile, fwci = item
        classic_cards.append(board_card(
            rank, aid, title, published, score,
            f"发表 {published} · citations {citations if citations is not None else 'N/A'} · "
            f"活跃引用年 {active if active is not None else 'N/A'}",
            f"Classic score v2.1 · OpenAlex percentile {percentile if percentile is not None else 'N/A'}"))

    monthly_body = "".join(monthly_cards) or '<div class="ov-entry">暂无数据</div>'
    classic_body = "".join(classic_cards) or '<div class="ov-entry">暂无数据</div>'
    body = (f'<section class="ov-board"><h2 class="ov-board-title">🔥 本周新品榜</h2>'
            f'<div class="ov-board-sub">最近 7 天相关论文，按综评分排序</div>{weekly_body}</section>')
    body += (f'<section class="ov-board"><h2 class="ov-board-title">📈 本月潜力榜</h2>'
             f'<div class="ov-board-sub">发表 8–30 天、已有综评的论文</div>'
             f'{monthly_body}</section>')
    body += (f'<section class="ov-board"><h2 class="ov-board-title">📚 经典沉淀榜</h2>'
             f'<div class="ov-board-sub">OpenAlex citation impact · Classic score v2.1</div>'
             f'{classic_body}</section>')
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>今日精选 · Top {shown} · {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Libre+Baskerville:ital,wght@0,700;1,400&family=Noto+Sans+SC:wght@400;700&family=Noto+Serif+SC:wght@700&family=Source+Sans+3:wght@400;700&display=swap" rel="stylesheet">
{katex_head}
<style>
{full_css}
{scorer.CSS}
{_OV_CSS}
body{{max-width:none;margin:0;padding:0;background:#fff}}
</style></head><body class="show-zh">
<div class="ov-wrap">
<h1 class="ov-title">今日精选 · Top {shown}</h1>
<div class="ov-sub">{date_str} · 最近 7 天窗口（共 {total} 篇入窗，展示前 {shown}{('，另含 ' + str(len(extra_studied)) + ' 篇 30 名外已精读') if extra_studied else ''}）· 按综评分（0-10）降序 · ★今日精读 / ✓已精读</div>
{body}
</div>
</body></html>
"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"{date_str}_top30_overview.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
