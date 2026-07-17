#!/usr/bin/env python3
"""
deep_study_simple —— 精读生成的「简化回退版」，与多趟版 deep_study.py 并存，便于对比。

架构（比多趟版更简单，砍掉易出 bug 的层）：
  Pass 1（大纲）：读全文 → 教学大纲，把大章节映射到原文小节；中文标题/hook/takeaway。
  Pass 2（逐块讲解）：每个原文小节用「视觉优先、极简」指令自我讲解（内联图/表/短句）。
  组装：代码【直接拼接】Pass-2 讲解——
        · 无 Pass-3 衔接层（intro/过渡/outro）——它曾是 bug 与 prose 膨胀的来源；
        · 无单独的视觉 Pass——视觉自然从 Pass-2 里产生，不再叠加第二轮。
        代码统一编号 + 目录 + 页脚 + KaTeX。

复用 deep_study.py 的全部辅助函数（含 class="container" 泄漏修复）；不修改 deep_study.py。
用法： python daily_bot/deep_study_simple.py <arxiv_id> [model] [--outline-only]
"""

import datetime
import os
import re
import sys

import deep_study as ds  # 复用其辅助函数与配置；导入即加载 .env


def generate_simple(arxiv_id, model=ds.DEFAULT_MODEL, outline_only=False):
    print(f"[simple] 取元数据 {arxiv_id} …（模型：{model}）")
    meta = ds.fetch_metadata(arxiv_id)
    if not meta:
        raise RuntimeError(f"arXiv 查不到 {arxiv_id}")
    pdf_url = meta.get("pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}"
    print(f"[simple] 下载 PDF {pdf_url} …")
    pdf_bytes = ds.download_pdf(pdf_url)
    full_text, pages, chunks, method = ds.analyze_pdf(pdf_bytes)
    full_text = full_text.strip()
    print(f"[simple] PDF {pages} 页，全文 {len(full_text)} 字符；结构切分={method}")

    if len(full_text) > ds.MAX_TEXT_CHARS:
        full_text = full_text[:ds.MAX_TEXT_CHARS]

    css = ds.load_css()
    if not chunks:
        print("[simple][WARN] 结构切分失败，转用 deep_study 的单趟回退。")
        return ds.generate_single_pass(meta, full_text, pages, css, model)

    # ---- Pass 1：大纲 ----
    print(f"[simple] Pass 1（大纲）… 原文小节 {len(chunks)} 块")
    data, u1 = ds.pass1_outline(meta, full_text, chunks, model)
    usages = [u1]
    hook = data.get("hook", "") or ""
    takeaway = data.get("takeaway", "") or ""
    bigs = data.get("big_sections") or []

    by_label = {c["label"]: c for c in chunks}
    resolved, used = [], set()
    for bs in bigs:
        title = (bs.get("title") or "（无标题）").strip()
        cs = []
        for l in [str(x).strip() for x in (bs.get("section_labels") or [])]:
            if l in by_label and l not in used:
                cs.append(by_label[l])
                used.add(l)
        if cs:
            resolved.append({"title": title, "chunks": cs})
    excluded = [c["label"] for c in chunks if c["label"] not in used]

    print("\n========== Pass-1 大纲（simple）==========")
    print(f"hook    : {hook}")
    print(f"takeaway: {takeaway}")
    for i, bs in enumerate(resolved, 1):
        print(f"  [大章节 {i}] {bs['title']}  ← {', '.join(c['label'] for c in bs['chunks'])}")
    if excluded:
        print(f"  (略过: {', '.join(excluded)})")
    print("=========================================\n")

    if outline_only:
        return {"arxiv_id": arxiv_id, "title": meta.get("title"), "model": model,
                "mode": "simple-outline-only", "usage": ds._sum_usage(usages)}
    if not resolved:
        raise RuntimeError("Pass-1 未产出任何可用大章节")

    # ---- Pass 2：逐块视觉优先讲解 ----
    print("[simple] Pass 2（逐块视觉优先讲解，顺序执行）…")
    for bs in resolved:
        for c in bs["chunks"]:
            print(f"    - 讲解 {c['label']} · {c['title'][:40]}")
            c["explanation"], u = ds.pass2_explain(c, model)
            usages.append(u)

    # ---- 组装：直接拼接（无 Pass-3、无视觉 Pass）----
    big_titles, sections_html = [], []
    for i, bs in enumerate(resolved, 1):
        chunk_expls = [(c["title"], c["explanation"]) for c in bs["chunks"]]
        # intro/outro/transitions 全空、figures=None：纯拼接
        sections_html.append(
            ds._assemble_big_section(i, bs["title"], chunk_expls, "", "", [], None))
        big_titles.append(bs["title"])

    body = ds._build_body(meta, hook, takeaway, big_titles, sections_html)
    doc = ds.wrap_document(meta, body, css, model=model)

    os.makedirs(ds.OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()
    safe_id = arxiv_id.replace("/", "_")
    model_tag = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-")
    path = os.path.join(ds.OUTPUT_DIR, f"{today}_{safe_id}_study_simple_{model_tag}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

    st = ds._stats(body)
    return {
        "arxiv_id": arxiv_id, "title": meta.get("title"), "model": model, "mode": "simple",
        "split_method": method, "pages": pages, "text_chars": len(full_text),
        "abstract_chars": len(meta.get("abstract") or ""),
        "hook": hook, "takeaway": takeaway, "excluded": excluded,
        "n_big_sections": len(resolved),
        "n_chunks_explained": sum(len(b["chunks"]) for b in resolved),
        "finished_clean": body.rstrip().endswith("</div>"),
        "usage": ds._sum_usage(usages), "path": path,
        "doc_bytes": len(doc.encode("utf-8")), "doc_lines": doc.count("\n") + 1,
        "body_chars": len(body), "stats": st,
    }


def main():
    args = [a for a in sys.argv[1:]]
    outline_only = "--outline-only" in args
    args = [a for a in args if a != "--outline-only"]
    if not args:
        print("用法: python daily_bot/deep_study_simple.py <arxiv_id> [model] [--outline-only]")
        sys.exit(1)
    arxiv_id = args[0].strip()
    model = args[1].strip() if len(args) > 1 else \
        os.environ.get("DEEP_STUDY_MODEL", ds.DEFAULT_MODEL)

    r = generate_simple(arxiv_id, model=model, outline_only=outline_only)

    if r.get("mode", "").endswith("outline-only"):
        print("\n== simple 仅大纲完成 ==  usage:", r.get("usage"))
        return
    st = r["stats"]
    print("\n== simple 精读生成完成 ==")
    print(f"model        : {r['model']}   mode: {r['mode']}   split: {r.get('split_method')}")
    print(f"title        : {r['title']}")
    print(f"size         : {r['doc_bytes']//1024} KB / {r['doc_lines']} lines")
    print(f"finished OK  : {r['finished_clean']}")
    print(f"big sections : {r['n_big_sections']}   chunks: {r['n_chunks_explained']}"
          + (f"   略过: {', '.join(r['excluded'])}" if r.get("excluded") else ""))
    print(f"PROSE        : {st['prose_chars']} chars / {st['paragraphs']} paras（>300: {st['long_paragraphs']}）")
    print(f"VISUALS      : SVG {st['svg']} + tables {st['tables']} = {st['svg'] + st['tables']}")
    print(f"formulas     : {st['formulas']}（disp {st['formulas_display']}）  theorem: {st['theorem_blocks']}  callouts: {st['callouts']}")
    print(f"usage        : {r['usage']}")
    print(f"output       : {r['path']}")


if __name__ == "__main__":
    main()
