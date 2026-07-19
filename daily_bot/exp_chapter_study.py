#!/usr/bin/env python3
"""
exp_chapter_study —— 【实验，独立文件，绝不改动生产代码】
A/B 对比深读的「讲解单元」：
  生产：每个原文小节一次 LLM 调用（~45 次/篇）。
  实验：每个 Pass-1 大章节一次 LLM 调用（把该章节的小节文本合并后送入，~5-6 次/篇）。

复用 deep_study 的现成部件（analyze_pdf 切分 / pass1_outline / pass_visuals / _llm /
_clean_fragment / _visual_figure / _build_body / wrap_document / CSS / SVG-$ 清洗）——
只在本文件里实现「按大章节合并输入 + 单次讲解 + 完整性守卫 + 组装」。

输出：daily_bot/output/{date}_{arxiv_id}_study_CHAPTER_{model}.html（永不与生产文件冲突）。
用法：python daily_bot/exp_chapter_study.py <arxiv_id> [model]
"""

import datetime
import html as _html
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deep_study as ds  # noqa: E402  （只读复用，不修改）

DEFAULT_MODEL = ds.DEFAULT_MODEL
MIN_BODY_CHARS = 200          # 小节正文短于此→视为空壳/假节，跳过
CHAPTER_MAX_TOKENS = 32_000   # 一章比一节大，放开输出
_BEG = re.compile(r"请粘贴|请提供|请补充|暂未提供|paste the|provide the (?:content|text)", re.I)


def _looks_fake(title):
    """标题像假节：纯/小数编号(0.93/2.1/.94)、全大写常量(APP_ID)、尾连字符碎片(LLM-)。"""
    t = (title or "").strip()
    if not t:
        return True
    if re.fullmatch(r"\d+(\.\d+)*", t):        # 0.93 / 2.1 / 3
        return True
    if re.fullmatch(r"0?\.\d+", t):            # .94
        return True
    if re.fullmatch(r"[A-Z0-9_]{2,}", t):      # APP_ID / LLM  (纯大写常量)
        return True
    if t.endswith("-"):                        # LLM-
        return True
    return False


def _keep(c):
    text = (c.get("text") or "").strip()
    return len(text) >= MIN_BODY_CHARS and not _looks_fake(c.get("title"))


def _merged_input(chapter):
    """text-first + cleaned titles；返回 (merged_text, kept_labels, skipped_labels)。"""
    parts, kept, skipped = [], [], []
    for c in chapter["chunks"]:
        if _keep(c):
            parts.append(f"◆ {(c.get('title') or '').strip()}\n{c['text'].strip()}")
            kept.append(c["label"])
        else:
            skipped.append(c["label"])
    return "\n\n".join(parts), kept, skipped


# 章节讲解 prompt：沿用 pass2_explain 的人设/图表优先/数学/SVG 规则，但允许 <h3> 分小节、面向整章。
CHAPTER_SYS = (
    "我是刚读完大二的学生，想在几分钟内看懂这【一整章】。下面是这一章包含的若干原文小节内容"
    "（已合并，用 ◆ 分隔）。请把整章讲成一段【连贯】的教学讲义，可用 <h3> 分小节来组织，"
    "但【不要】输出 <h1>/<h2>、不要章节编号、不要目录。\n"
    "请优先用图、表、要点列表来讲，文字只用来串联——能画图就画图，能列表就列表，能做对比就做表格。\n"
    "【硬性规则】\n"
    "- 文字很贵、图表很便宜：每个小节都【默认】用一张 SVG 图 / 一个表格 / 一个要点列表来讲。\n"
    "- 任何一段 <p> 不得超过 3 句话；更长的内容必须拆开，或改写成表格 / 列表 / 图。\n"
    "- 只要涉及【对比、顺序、结构、流程】→ 必须用表格 <table> 或图 <svg>，不许用大段文字。\n"
    "输出 HTML 片段（不是完整网页）：\n"
    "- 数学公式【必须】用 $...$（行内）或 $$...$$（独立），直接写 LaTeX；不要用 \\(...\\) 或 \\[...\\]。\n"
    "- 【所有数学一律 $LaTeX$】正文、表格单元格 <td>、列表 <li> 里出现数学符号就必须包进 $...$，"
    "严禁裸写 unicode/ASCII 伪数学（如 ṽ、ΣT≥0、Λ*=M*⁻¹）。\n"
    "- 【SVG 例外】<svg> 的 <text> 里不能用 $...$/LaTeX（KaTeX 无法在 SVG 内渲染），"
    "只放简短纯文本 + unicode 数学符号（A ≤ B、√、Σ、xᵢ、M⁻¹）；复杂公式放到图旁 <p> 里。\n"
    "- 所有文字标注一律用中文（专有名词可保留英文原词）。\n"
    "多用 <table>、<ul>/<ol>、内联 <svg>（配色 #c0392b/#16786a/#2c5aa0/#faf8f5/#1a1a1a）、"
    "<div class=\"box info|success|danger\">；算法用 <div class=\"algo\">，"
    "定理/证明用 <div class=\"theorem-block\">/<div class=\"theorem-block proof\">。"
    "不要 markdown 代码围栏。")


def _bad_output(h):
    """完整性守卫：太短 / 乞讨话术 / 疑似截断（未以 > 收尾）→ 返回原因，否则 None。"""
    s = (h or "").strip()
    if len(s) < 300:
        return "too-short"
    if _BEG.search(s):
        return "begging"
    if not s.endswith(">"):
        return "truncated"
    return None


def _explain_chapter(title, merged, model, max_attempts=4):
    """
    单次讲解一整章；扛住两类失败：
      - relay 503/超时 等异常 → 退避重试（30/60/90/120s）；
      - 坏输出（太短/乞讨/截断）→ 重试。
    最多 max_attempts 次；始终返回（不抛出、不崩溃）。
    返回 (html, calls, retried, out_chars, reason_final)。
    """
    user = f"这一章的标题：{title}\n\n（以下为本章合并的原文小节内容）\n{merged}"
    calls, last, reason = 0, "", "no-output"
    for attempt in range(1, max_attempts + 1):
        try:
            content, _ = ds._llm(CHAPTER_SYS, user, model, temperature=0.6,
                                 max_tokens=CHAPTER_MAX_TOKENS)
            calls += 1
            frag = ds.strip_dollar_in_svg_text(ds._clean_fragment(content))
            reason = _bad_output(frag)
            last = frag
            if not reason:
                return frag, calls, (attempt > 1), len(frag), None
            print(f"      [guard] 输出异常({reason})，attempt {attempt}/{max_attempts}")
            time.sleep(5)
        except Exception as e:
            calls += 1
            reason = f"exc:{str(e)[:40]}"
            rest = 30 * attempt
            print(f"      [guard] 调用异常({str(e)[:50]})，退避 {rest}s 后重试 "
                  f"attempt {attempt}/{max_attempts}")
            time.sleep(rest)
    return last, calls, True, len(last), reason


def _assemble_chapter(idx, title, body_html, figures):
    esc = _html.escape
    parts = [f'<section id="s{idx}">',
             f'<h2><span class="section-num">{idx}</span>{esc(title)}</h2>']
    for fig in (figures or []):
        parts.append(ds._visual_figure(fig.get("caption", ""), fig["html"]))
    parts.append(body_html)
    parts.append("</section>")
    return "\n".join(parts)


def generate_chapter_study(arxiv_id, model=DEFAULT_MODEL):
    t0 = time.time()
    meta = ds.fetch_metadata(arxiv_id)
    if not meta:
        raise RuntimeError(f"arXiv 查不到 {arxiv_id}")
    pdf_url = meta.get("pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}"
    print(f"[exp] 下载 PDF {pdf_url} …")
    full_text, pages, chunks, method = ds.analyze_pdf(ds.download_pdf(pdf_url))
    full_text = (full_text or "").strip()
    if not chunks:
        raise RuntimeError("结构切分失败（无 chunks）——本实验只测切分成功的情况")
    if len(full_text) > ds.MAX_TEXT_CHARS:
        full_text = full_text[:ds.MAX_TEXT_CHARS]
    print(f"[exp] {pages} 页，全文 {len(full_text)} 字符，原文小节 {len(chunks)} 块，切分={method}")

    total_calls = 0
    # Pass-1 大纲（relay 偶发失败/解析空 → 最多重试 3 次）
    by_label = {c["label"]: c for c in chunks}
    resolved, hook, takeaway = [], "", ""
    for attempt in range(3):
        try:
            data, _ = ds.pass1_outline(meta, full_text, chunks, model)
        except Exception as e:
            print(f"[exp] Pass-1 第{attempt+1}次调用异常：{e}")
            data = {}
        total_calls += 1
        hook = data.get("hook", "") or hook
        takeaway = data.get("takeaway", "") or takeaway
        bigs = data.get("big_sections") or []
        resolved, used = [], set()
        for bs in bigs:
            title = (bs.get("title") or "（无标题）").strip()
            cs = [by_label[str(l).strip()] for l in (bs.get("section_labels") or [])
                  if str(l).strip() in by_label and str(l).strip() not in used]
            for c in cs:
                used.add(c["label"])
            if cs:
                resolved.append({"title": title, "chunks": cs})
        if len(resolved) >= 3:   # 45 小节的论文应切出 ≥3 大章节；1 章多为 relay 抖动下的残缺输出
            break
        print(f"[exp] Pass-1 第{attempt+1}次仅 {len(resolved)} 个大章节（期望≥3），"
              f"{'重试' if attempt<2 else '放弃'}…")
        time.sleep(20 * (attempt + 1))
    if not resolved:
        raise RuntimeError("Pass-1 三次仍未产出可用大章节")
    if len(resolved) < 3:
        print(f"[exp][WARN] 最终仅 {len(resolved)} 个大章节，Pass-1 质量可能不佳，仍继续。")
    print(f"[exp] Pass-1 大章节 {len(resolved)} 个")

    # 视觉 Pass（复用，按章配图）；relay 503 时退避重试，实在不行就无图继续（不影响讲解单元对比）
    visuals = []
    for attempt in range(3):
        try:
            visuals, _ = ds.pass_visuals(full_text, resolved, model)
            total_calls += 1
            break
        except Exception as e:
            total_calls += 1
            print(f"[exp] 视觉 Pass 第{attempt+1}次异常({str(e)[:45]})，"
                  f"{'退避重试' if attempt < 2 else '放弃，无图继续'}")
            if attempt < 2:
                time.sleep(30 * (attempt + 1))
    figs_by_sec = {}
    for d in visuals:
        figs_by_sec.setdefault(d["section"], []).append(d)
    print(f"[exp] 视觉件 {len(visuals)} 个")

    # 逐【大章节】一次讲解
    print("[exp] 逐大章节讲解（每章一次调用）…")
    sections_html, big_titles, log = [], [], []
    for i, ch in enumerate(resolved, 1):
        merged, kept, skipped = _merged_input(ch)
        if not merged.strip():
            print(f"    - [章 {i}] {ch['title'][:40]} → 全部小节被过滤，跳过")
            log.append({"ch": i, "title": ch["title"], "in": 0, "out": 0,
                        "calls": 0, "retried": False, "kept": 0, "skipped": len(skipped)})
            continue
        print(f"    - [章 {i}] {ch['title'][:40]} | 合并 {len(kept)} 节（跳过 {len(skipped)}），"
              f"输入 {len(merged)} 字符")
        body, calls, retried, out_chars, reason = _explain_chapter(ch["title"], merged, model)
        total_calls += calls
        sections_html.append(_assemble_chapter(i, ch["title"], body, figs_by_sec.get(i, [])))
        big_titles.append(ch["title"])
        log.append({"ch": i, "title": ch["title"], "in": len(merged), "out": out_chars,
                    "calls": calls, "retried": retried, "kept": len(kept),
                    "skipped": len(skipped), "reason": reason})

    body = ds._build_body(meta, hook, takeaway, big_titles, sections_html)
    doc = ds.wrap_document(meta, body, ds.load_css(), model=model)
    os.makedirs(ds.OUTPUT_DIR, exist_ok=True)
    date = datetime.date.today().isoformat()
    tag = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    path = os.path.join(ds.OUTPUT_DIR, f"{date}_{arxiv_id}_study_CHAPTER_{tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

    n_big = doc.count('<section id="s')
    return {"path": path, "elapsed_s": round(time.time() - t0, 1),
            "total_calls": total_calls, "n_big_sections": n_big,
            "chapters": log, "hook": hook, "pages": pages,
            "text_chars": len(full_text), "n_chunks": len(chunks)}


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python daily_bot/exp_chapter_study.py <arxiv_id> [model]")
        sys.exit(1)
    aid = args[0].strip()
    model = args[1].strip() if len(args) > 1 else DEFAULT_MODEL
    r = generate_chapter_study(aid, model=model)
    print("\n===== CHAPTER 实验结果 =====")
    print(f"  文件: {r['path']}")
    print(f"  总 LLM 调用: {r['total_calls']}（对比 per-section ~{r['n_chunks']} 次讲解 + 2）")
    print(f"  用时: {r['elapsed_s']}s | n_big_sections: {r['n_big_sections']} | "
          f"{r['pages']}页/{r['text_chars']}字符/{r['n_chunks']}小节")
    print(f"  {'章':>3} {'合并节':>5} {'跳过':>4} {'输入字符':>8} {'输出字符':>8} {'调用':>4} {'重试':>4}  标题")
    begging = 0
    for c in r["chapters"]:
        print(f"  {c['ch']:>3} {c['kept']:>5} {c['skipped']:>4} {c['in']:>8} {c['out']:>8} "
              f"{c['calls']:>4} {str(c['retried']):>4}  {c['title'][:36]}")
        if c.get("reason") == "begging":
            begging += 1
    print(f"  乞讨话术章节数: {begging}（应为 0）")


if __name__ == "__main__":
    main()
