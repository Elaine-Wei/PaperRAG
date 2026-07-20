#!/usr/bin/env python3
"""
exp_theme_summary —— 【实验，独立文件，绝不改动生产代码】
在【保留】既有 per-section 详细讲解（覆盖由代码控制）之上，叠加一层宏观「主题块导读」：

1) 读一篇已有的 per-section 深读 HTML，抽出各原文小节的 <h3>标题 + 详细讲解。
2) 边界检测（1 次 LLM）：把讲解列表喂给模型，让它把【连续】小节归组为「主题块」（语义分段，
   比字体切分/论文自带编号更准），输出块名 + 每块包含的连续小节编号。
3) 块导读（每块 1 次 LLM）：拿该块【原文】（合并）→ 生成『本块导读』：3-8 条要点、300-600 中文字符，
   什么问题/怎么做/关键结论/怎么串起来。是 SUMMARY（压缩+取舍），不是逐节 explanation。
4) 组装实验 HTML：每个主题块 = 顶部「本块导读」面板（常显）+ 折叠 <details> 内放该块的详细讲解
   （默认只看导读，点开才看深讲——直击「太长/读着累」的痛点）。同一套 CSS/KaTeX。
   输出 {date}_{arxiv_id}_study_THEMED_{model}.html（永不与生产文件冲突）。

复用 deep_study 的部件（analyze_pdf 取原文、_llm、_clean_fragment、strip、_build_body、wrap_document、CSS）。
所有 LLM 调用对 relay 503 退避重试。用法：python daily_bot/exp_theme_summary.py <arxiv_id> [model]
"""

import datetime
import glob
import html as _html
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deep_study as ds  # noqa: E402  （只读复用）
import relay            # noqa: E402

DEFAULT_MODEL = "gpt-5.6-sol"   # 实验用：sol 今日在线且质量好（luna 全天 503-dead）
_BEG = re.compile(r"请粘贴|请提供|暂未提供|请补充", re.I)


# ---------------------------------------------------------------------------
# 503-tolerant LLM
# ---------------------------------------------------------------------------
def _llm_retry(system, user, model, max_tokens, tries=4, want_json=False, min_len=0):
    for attempt in range(1, tries + 1):
        try:
            content, _ = ds._llm(system, user, model, temperature=0.3, max_tokens=max_tokens)
        except Exception as e:
            rest = 30 * attempt
            print(f"      [llm] 异常({str(e)[:45]})，退避 {rest}s（{attempt}/{tries}）", flush=True)
            time.sleep(rest)
            continue
        if want_json:
            obj = relay.extract_json(content)
            if obj:
                return obj
        else:
            frag = ds.strip_dollar_in_svg_text(ds._clean_fragment(content))
            if len(frag) >= min_len and not _BEG.search(frag):
                return frag
            print(f"      [llm] 输出不合格（短/乞讨），重试（{attempt}/{tries}）", flush=True)
            time.sleep(3)
    return None


# ---------------------------------------------------------------------------
# 读既有 per-section 深读 HTML → 抽取各小节 (标题, 讲解 HTML)
# ---------------------------------------------------------------------------
def _find_study_html(arxiv_id):
    cands = sorted(glob.glob(os.path.join(ds.OUTPUT_DIR, f"*_{arxiv_id}_study_mp_*.html")),
                   reverse=True)
    cands = [c for c in cands if "_THEMED_" not in c and "CHAPTER" not in c]
    return cands[0] if cands else None


def _extract_sections(study_path):
    """从深读 HTML 抽出有序的 [{title, expl_html}]（每个 <h3> 小节及其正文）。"""
    with open(study_path, encoding="utf-8") as f:
        body = f.read()
    h3s = list(re.finditer(r"<h3[^>]*>(.*?)</h3>", body, re.S))
    out = []
    for i, m in enumerate(h3s):
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        start = m.end()
        end = h3s[i + 1].start() if i + 1 < len(h3s) else len(body)
        expl = body[start:end]
        # 截掉章节/页脚/总结框等尾部，只留本小节讲解
        expl = re.split(r"</section>|<footer|<div class=\"box success\"|</div>\s*</body>",
                        expl)[0].strip()
        if title and expl:
            out.append({"title": title, "expl_html": expl})
    return out


def _hook_from_study(study_path):
    try:
        with open(study_path, encoding="utf-8") as f:
            h = f.read()
        m = re.search(r'<p class="header-subtitle[^"]*">(.*?)</p>', h, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 匹配原文（用于块导读的 SUMMARY 输入）
# ---------------------------------------------------------------------------
def _toks(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _orig_text_map(sections, chunks):
    """把每个 <h3> 小节标题匹配到 analyze_pdf 的原文 chunk，取原文 text。匹配不到→用讲解纯文本兜底。"""
    by_title = {c["title"].strip(): c for c in chunks}
    out = []
    for s in sections:
        t = s["title"]
        if t in by_title:
            out.append(by_title[t]["text"])
            continue
        best, bo = None, 0.0
        for c in chunks:
            a, b = _toks(t), _toks(c["title"])
            ov = (len(a & b) / len(a)) if a else 0.0
            if ov > bo:
                bo, best = ov, c
        out.append(best["text"] if best and bo >= 0.5
                   else re.sub(r"<[^>]+>", " ", s["expl_html"]))  # 兜底：讲解去标签
    return out


# ---------------------------------------------------------------------------
# 边界检测 + 块导读
# ---------------------------------------------------------------------------
BOUNDARY_SYS = (
    "下面是一篇论文按【原文小节】顺序排列的详细讲解列表（已编号）。请把【连续】的小节归组为若干"
    "『主题块』——每块是讨论同一件事的连续小节区间（如『问题设定与误差分析』）。"
    "要求：块内小节编号必须连续；所有小节都要被覆盖；块之间不重叠；每块起一个简洁中文名。\n"
    '只输出 JSON：{"blocks":[{"name":"中文块名","sections":[1,2,3]}]}，不要多余文字。')


def detect_blocks(sections, model):
    listing = "\n".join(
        f"{i + 1}. {s['title']}｜{re.sub(r'<[^>]+>', '', s['expl_html'])[:200]}"
        for i, s in enumerate(sections))
    obj = _llm_retry(BOUNDARY_SYS, listing, model, max_tokens=2000, want_json=True)
    n = len(sections)
    blocks = []
    if obj and isinstance(obj.get("blocks"), list):
        for b in obj["blocks"]:
            idxs = sorted(int(x) for x in (b.get("sections") or [])
                          if str(x).strip().isdigit() and 1 <= int(x) <= n)
            if idxs:
                blocks.append({"name": (b.get("name") or "（未命名）").strip(), "idxs": idxs})
    # HARD-FAIL：模型未产出有效主题块 → 直接报错，绝不退化成「每 3 节一块」的假分组
    if not blocks:
        raise RuntimeError("边界检测失败：模型未返回有效主题块（不做假分组，需重跑）")
    # 覆盖补齐：把未被分配的小节并入最近的前一个块（对基本正确结果的连续性修补，非假分组）
    blocks = sorted(blocks, key=lambda b: b["idxs"][0])
    block_of = {i: bi for bi, b in enumerate(blocks) for i in b["idxs"]}
    last_bi = 0
    for i in range(1, n + 1):
        if i in block_of:
            last_bi = block_of[i]
        else:
            blocks[last_bi]["idxs"].append(i)
    for b in blocks:
        b["idxs"] = sorted(set(b["idxs"]))
    return sorted(blocks, key=lambda b: b["idxs"][0])


BLOCK_SUMMARY_SYS = (
    "你在为一个『主题块』写【本块导读】，读者只想 2-3 分钟看懂全文骨架，别再堆长文。"
    "根据下面该块的【原文】，用 3-8 条要点（<ul><li>）条理化：这块在讲什么问题、怎么做的、"
    "关键结论、各部分怎么串起来。总长控制在 300-600 中文字符，简洁、不照抄、不写大段散文。\n"
    "只输出 HTML 片段（<ul><li>…</li></ul>，可前置一句 <p> 提纲）。数学用 $...$。不要 <h1>/<h2>/<h3>。")


def summarize_block(name, merged_orig, model):
    user = f"主题块：{name}\n\n（本块合并的原文）\n{merged_orig[:12000]}"
    frag = _llm_retry(BLOCK_SUMMARY_SYS, user, model, max_tokens=1500, min_len=120)
    return frag or '<p>（本块导读生成失败）</p>'


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def _assemble_block(idx, name, summary_html, sec_units):
    esc = _html.escape
    parts = [f'<section id="s{idx}">',
             f'<h2><span class="section-num">{idx}</span>{esc(name)}</h2>',
             '<div class="box info"><div class="box-label">本块导读（先读这个）</div>',
             summary_html, '</div>',
             f'<details style="margin-top:10px">'
             f'<summary style="cursor:pointer;font-weight:700;color:#2c5aa0;padding:6px 0">'
             f'▸ 展开详细讲解（{len(sec_units)} 节）</summary>']
    for s in sec_units:
        parts.append(f"<h3>{esc(s['title'])}</h3>")
        parts.append(s["expl_html"])
    parts.append("</details></section>")
    return "\n".join(parts)


def generate_theme_study(arxiv_id, model=DEFAULT_MODEL):
    t0 = time.time()
    study_path = _find_study_html(arxiv_id)
    if not study_path:
        raise RuntimeError(f"找不到 {arxiv_id} 的既有 per-section 深读 HTML（先生成一篇）")
    print(f"[theme] 读既有深读：{os.path.basename(study_path)}")
    sections = _extract_sections(study_path)
    if len(sections) < 2:
        raise RuntimeError(f"抽到的小节太少（{len(sections)}）")
    print(f"[theme] 抽到 {len(sections)} 个原文小节")

    meta = ds.fetch_metadata(arxiv_id) or {"arxiv_id": arxiv_id, "title": arxiv_id}
    hook = _hook_from_study(study_path)
    # 原文（用于 SUMMARY 输入）
    try:
        _, _, chunks, _ = ds.analyze_pdf(ds.download_pdf(meta.get("pdf_url") or
                                         f"https://arxiv.org/pdf/{arxiv_id}"))
    except Exception as e:
        print(f"[theme][WARN] 取原文失败({str(e)[:40]})，用讲解文本兜底")
        chunks = []
    orig = _orig_text_map(sections, chunks) if chunks else \
        [re.sub(r"<[^>]+>", " ", s["expl_html"]) for s in sections]

    calls = 0
    print("[theme] 边界检测（1 次 LLM）…")
    blocks = detect_blocks(sections, model)
    calls += 1
    print(f"[theme] 主题块 {len(blocks)} 个：")
    for b in blocks:
        titles = " / ".join(sections[i - 1]["title"][:18] for i in b["idxs"])
        print(f"   [{b['name']}] 小节 {b['idxs']} = {titles}")

    print("[theme] 各块导读（每块 1 次 LLM）…")
    block_names, sections_html, sizes = [], [], []
    for i, b in enumerate(blocks, 1):
        merged = "\n\n".join(f"◆ {sections[j-1]['title']}\n{orig[j-1]}" for j in b["idxs"])
        summ = summarize_block(b["name"], merged, model)
        calls += 1
        sizes.append(len(re.sub(r"<[^>]+>", "", summ)))
        units = [sections[j - 1] for j in b["idxs"]]
        sections_html.append(_assemble_block(i, b["name"], summ, units))
        block_names.append(b["name"])
        print(f"   [{i}] {b['name']} 导读 {sizes[-1]} 字符，含 {len(units)} 节")

    body = ds._build_body(meta, hook, "", block_names, sections_html)
    doc = ds.wrap_document(meta, body, ds.load_css(), model=model)
    os.makedirs(ds.OUTPUT_DIR, exist_ok=True)
    date = datetime.date.today().isoformat()
    tag = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    path = os.path.join(ds.OUTPUT_DIR, f"{date}_{arxiv_id}_study_THEMED_{tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return {"path": path, "elapsed_s": round(time.time() - t0, 1), "total_calls": calls,
            "n_sections": len(sections), "n_blocks": len(blocks),
            "blocks": [{"name": b["name"], "idxs": b["idxs"]} for b in blocks],
            "summary_sizes": sizes, "source": os.path.basename(study_path)}


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python daily_bot/exp_theme_summary.py <arxiv_id> [model]")
        sys.exit(1)
    aid = args[0].strip()
    model = args[1].strip() if len(args) > 1 else DEFAULT_MODEL
    r = generate_theme_study(aid, model=model)
    print("\n===== THEME 实验结果 =====")
    print(f"  源: {r['source']}")
    print(f"  文件: {r['path']}")
    print(f"  {r['n_sections']} 小节 → {r['n_blocks']} 主题块 | 总 LLM 调用 {r['total_calls']} | 用时 {r['elapsed_s']}s")
    print(f"  各块导读字符数: {r['summary_sizes']}（目标 300-600）")
    for b in r["blocks"]:
        print(f"    · {b['name']}  小节 {b['idxs']}")


if __name__ == "__main__":
    main()
