#!/usr/bin/env python3
"""
run_boards —— 一次性【主题榜单】编排（两个板块，两个群），与每日流水线【完全隔离】：
  只写 board_paper / board_score / board_push，绝不碰 daily_* 表。复用 scorer/deep_study/
  exp_theme_summary/assemble/cos_upload/wecom 的计算函数（仅在评分处按区切换 freshness 曲线、
  cross-check 走 sol）。

板块（硬分类：方法里用到 LLM → B，否则 → A）：
  A 传统因子挖掘（GP/符号回归/RL，无 LLM）  B LLM 因子挖掘
每板块两区：⭐经典必读(seeds，relaxed freshness，按 composite 质量排) + 🆕最新Top30(其余，steep freshness)。

用法：
  python daily_bot/run_boards.py --board A --dry-run        # 广召回→预筛→sol判定(过滤+分类)；池规模/两区/深读目标（无评分/深读/推送）
  python daily_bot/run_boards.py --board A                  # 真跑：评分→综评→深读6→概览→(可选)推送
  flags: --board A|B  --dry-run  --latest-top 30  --study-per-section 3  --window-years 2  --webhook URL
"""

import datetime
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run          # noqa: E402  加载 .env + parse_arxiv_xml + 复用 _classify_study_error 等
import db           # noqa: E402
import scorer       # noqa: E402
import deep_study   # noqa: E402

ARXIV_API = "http://export.arxiv.org/api/query"
STUDY_MODEL = "gpt-5.6-sol"      # deep-study + theme-summary（luna 宕机）
CROSS_MODEL = "gpt-5.6-sol"      # 交叉复核走 sol（luna 宕机；daily 默认不变）
CLF_MODEL = "claude-fable-5"     # LLM-or-not 分类（fable-5 在线）
# 经典区 relaxed freshness（center=365, scale=600）——老经典不被压到 1（AlphaGen 1125d≈2.3）；待学长最终确认
RELAXED = {"fresh_center": 365, "fresh_scale": 600}

SEEDS = {
    "A": {"2306.12964": "AlphaGen", "2406.18394": "AlphaForge",
          "2409.05144": "QuantFactor REINFORCE", "2406.16505": "Alpha^2",
          "2002.08245": "AutoAlpha", "2507.20263": "Trajectory-level Reward",
          "2601.22119": "Grammar-Guided"},
    "B": {"2508.06312": "Chain-of-Alpha", "2502.16789": "AlphaAgent",
          "2505.11122": "Alpha Jungle (LLM-MCTS)", "2511.18850": "Cognitive Alpha",
          "2603.16365": "FactorEngine", "2607.08332": "XALPHA",
          "2602.07085": "QuantaAlpha", "2602.14670": "FactorMiner"},
}
KW = {
    "A": ['AlphaGen formulaic alpha', 'AlphaForge', 'AlphaQCM', 'QuantFactor REINFORCE',
          'AutoAlpha factor mining', 'RiskMiner alpha', 'GFlowNet alpha factor',
          'genetic programming formulaic alpha', 'symbolic regression alpha'],
    "B": ['Chain-of-Alpha', 'AlphaAgent', 'LLM MCTS formulaic factor',
          'LLM code evolution alpha', 'large language model alpha factor mining',
          'LLM agent factor discovery'],
}
BROAD = ['formulaic alpha factor mining', 'alpha factor discovery quantitative',
         'alpha mining', 'automated factor construction stock', 'symbolic regression trading factor']


# ---------------------------------------------------------------------------
# arXiv 取数
# ---------------------------------------------------------------------------
def _get(url, tries=3):
    """arXiv API 偶发慢/超时 → 重试；仍失败返回 None（调用方跳过该查询，不崩溃整轮）。"""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PaperRAG-boards/0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as e:
            print(f"    [arxiv] 取数失败({str(e)[:35]})，重试 {k + 1}/{tries}", flush=True)
            time.sleep(5 * (k + 1))
    return None


def fetch_ids(ids):
    if not ids:
        return {}
    data = _get(ARXIV_API + "?" + urllib.parse.urlencode(
        {"id_list": ",".join(ids), "max_results": len(ids)}))
    return {p["arxiv_id"]: p for p in run.parse_arxiv_xml(data)} if data else {}


def fetch_search(query, n=50):
    # 广召回：相关度排序、较大 n；精度交给「便宜预筛 + sol LLM 判定」，不再用脆弱的正则主题闸。
    data = _get(ARXIV_API + "?" + urllib.parse.urlencode(
        {"search_query": f"all:{query}", "start": 0, "max_results": n,
         "sortBy": "relevance", "sortOrder": "descending"}))
    time.sleep(1.0)  # 尊重 arXiv 限速
    return run.parse_arxiv_xml(data) if data else []


# 便宜预筛：先扔掉明显非金融（省 LLM 调用）——q-fin 分类，或标题/摘要含金融词。
# 物理的 'alpha 粒子/α′ 修正' 不含这些金融词 → 被扔掉，不进 LLM 判定。
_FIN_RE = re.compile(
    r"(financ|stock|trading|\btrade\b|investment|portfolio|\bquant|market|asset|equit|"
    r"return predict|因子|股票|alpha factor|formulaic alpha|factor mining|alpha mining)", re.I)


def _prefilter(meta):
    if any((c or "").startswith("q-fin") for c in (meta.get("categories") or [])):
        return True
    return bool(_FIN_RE.search(f"{meta.get('title','')} {meta.get('abstract','')}"))


# ---------------------------------------------------------------------------
# 分类：LLM-or-not
# ---------------------------------------------------------------------------
_JUDGE_SYS = (
    "判断这篇论文是否为【股票/量化 alpha 因子挖掘】论文——即挖掘/发现用于预测收益的公式化因子/alpha。"
    "若是，再判断其【方法】是否用到大语言模型(LLM)：用到（LLM 生成/进化公式、LLM agent、LLM+MCTS、"
    "代码进化等）→ board B（LLM 因子挖掘）；仅用 GP/符号回归/强化学习/进化算法等、方法无 LLM → board A（传统）。"
    "注意：物理里的 'alpha 粒子/α′ 修正'、纯股价预测/情感分析而非因子挖掘、与选股因子无关的论文，"
    "is_factor_mining 一律 false。"
    '只输出 JSON：{"is_factor_mining":true或false,"board":"A"或"B"或null,"reason":"一句中文理由"}')


def judge_llm(meta, model=STUDY_MODEL):
    """sol 一次调用同时【过滤+分类】：{is_factor_mining, board A/B/null, reason}。503 退避重试。"""
    import relay
    user = f"标题：{meta.get('title')}\n摘要：{(meta.get('abstract') or '')[:1400]}"
    for k in range(3):
        try:
            content, _ = relay.relay_chat(_JUDGE_SYS, user, temperature=0, model=model, max_tokens=220)
            obj = relay.extract_json(content)
            if obj is not None and "is_factor_mining" in obj:
                fm = bool(obj.get("is_factor_mining"))
                b = str(obj.get("board") or "").strip().upper()
                b = b if b in ("A", "B") else None
                return fm, b, (obj.get("reason") or "").strip()
        except Exception as e:
            print(f"    [judge] {meta.get('arxiv_id')} 异常({str(e)[:35]})，退避 {k+1}/3", flush=True)
            time.sleep(20 * (k + 1))
    return False, None, "（判定失败）"


# ---------------------------------------------------------------------------
# 组池：seeds(classics) + 非种子候选(latest)，去重 + 时间过滤
# ---------------------------------------------------------------------------
def retrieve_all(window_years=2, max_per_query=50):
    """广召回（seed 名 + 关键词 + broad，相关度排序），返回 (seed_meta_both, 非种子候选)。
    检索源不决定 board；精度交给下游 预筛 + sol 判定。"""
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=int(window_years * 365))
    seed_ids = set(SEEDS["A"]) | set(SEEDS["B"])
    seed_meta = fetch_ids(list(seed_ids))
    queries = list(SEEDS["A"].values()) + list(SEEDS["B"].values()) + KW["A"] + KW["B"] + BROAD
    pool = {}
    for q in queries:
        for m in fetch_search(q, max_per_query):
            aid = m["arxiv_id"]
            if aid in seed_ids or aid in pool:
                continue
            try:
                if datetime.date.fromisoformat((m.get("published") or "")[:10]) < cutoff:
                    continue
            except Exception:
                continue
            pool[aid] = m
    return seed_meta, list(pool.values())


# ---------------------------------------------------------------------------
# DRY-RUN：池 / 分类预览 / 两区 / 深读目标（无 LLM/评分/深读/推送）
# ---------------------------------------------------------------------------
def dry_run(board, study_per_section, latest_top, window_years, judge_cap=140):
    print(f"\n===== DRY-RUN 主题榜单（焦点 Board {board}；本轮做 sol 判定，无评分/深读/推送）=====")
    print("检索广召回 → 便宜金融预筛 → 每篇 sol 一次调用同时【过滤+分类】{is_factor_mining, board, reason}。")

    seed_meta, cand = retrieve_all(window_years)
    raw = len(cand)
    pre = [m for m in cand if _prefilter(m)]
    capped = False
    if len(pre) > judge_cap:                       # 兜底：判定数上限，避免失控（会打印被裁量）
        pre.sort(key=lambda m: m.get("published") or "", reverse=True)
        pre, capped = pre[:judge_cap], True

    print(f"\n(a) 候选池规模：")
    print(f"    检索非种子候选（去重+近{window_years}年）           {raw:>4} 篇")
    print(f"    → 金融预筛后（进入 sol 判定）                 {len(pre):>4} 篇"
          + (f"  [已裁至上限 {judge_cap}，丢弃最旧 {len([m for m in cand if _prefilter(m)]) - judge_cap} 篇]" if capped else ""))

    keptA, keptB, dropped = [], [], 0
    for i, m in enumerate(pre, 1):
        fm, b, reason = judge_llm(m)
        if fm and b:
            m["_reason"] = reason
            (keptA if b == "A" else keptB).append(m)
        else:
            dropped += 1
        if i % 20 == 0:
            print(f"    ...sol 判定进度 {i}/{len(pre)}（保留 A={len(keptA)} B={len(keptB)} 丢弃={dropped}）", flush=True)

    print(f"    → sol 判定后：is_factor_mining=true 保留 {len(keptA)+len(keptB)} 篇"
          f"（A={len(keptA)}，B={len(keptB)}），丢弃 {dropped} 篇（非因子挖掘）")

    # 种子分类 sanity（report-and-keep：不改人工归属）
    disagree = []
    for bd in ("A", "B"):
        for aid, name in SEEDS[bd].items():
            m = seed_meta.get(aid)
            if not m:
                disagree.append((aid, name, bd, "取数失败"))
                continue
            fm, jb, _ = judge_llm(m)
            if jb and jb != bd:
                disagree.append((aid, name, bd, jb + ("（且判为非因子挖掘）" if not fm else "")))
            elif not fm:
                disagree.append((aid, name, bd, "判为非因子挖掘"))

    for lst in (keptA, keptB):
        lst.sort(key=lambda m: m.get("published") or "", reverse=True)  # dry：无 composite，按新→旧预览

    for bd, latest in (("A", keptA), ("B", keptB)):
        star = "  ◀ 焦点" if bd == board else ""
        print(f"\n===== Board {bd}{star} =====")
        print(f"  ⭐ 经典必读(seeds) {len(SEEDS[bd])} 篇（真跑 relaxed freshness 365/600，按 composite 质量排）：")
        for aid, name in SEEDS[bd].items():
            m = seed_meta.get(aid) or {}
            print(f"    {aid:11} {str(m.get('published') or '?'):11} {name}｜{(m.get('title') or '')[:40]}")
        print(f"  🆕 最新（sol 判定={bd}）{len(latest)} 篇，真跑 steep freshness + composite 排序取前 {latest_top}：")
        if not latest:
            print("    （无）")
        for m in latest[:latest_top]:
            print(f"    {m['arxiv_id']:11} {str(m.get('published') or '?'):11} {(m.get('title') or '')[:50]}")
            print(f"        ← {(m.get('_reason') or '')[:76]}")

    print(f"\n[种子分类 sanity —— report-and-keep（不改动人工归属）]")
    if disagree:
        for aid, name, manual, jb in disagree:
            print(f"  ⚠ {aid} {name}: 人工={manual} 但 sol 判定={jb}")
    else:
        print("  ✓ 全部种子与人工归属一致，且均判为因子挖掘。")

    print(f"\n深读目标（真跑）：每板块 经典前 {study_per_section} + 最新前 {study_per_section} = {2*study_per_section} 篇"
          f"（{STUDY_MODEL}，theme-summary 风格）。经典 freshness={RELAXED}，最新 steep；"
          f"判定/交叉复核走 {CROSS_MODEL}。本轮无写库、无推送。")
    return {"board": board, "raw": raw, "prefiltered": len(pre),
            "keptA": len(keptA), "keptB": len(keptB), "dropped": dropped, "dry_run": True}


# ---------------------------------------------------------------------------
# 隔离持久化：只读写 board_*，绝不碰 daily_* 表
# ---------------------------------------------------------------------------
def _persist_paper(conn, aid, board, section, is_seed, seed_name, source,
                   published, clf_reason, clf_board):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO board_paper
              (arxiv_id, board, section, is_seed, seed_name, source, published, clf_reason, clf_board)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (arxiv_id, board) DO UPDATE SET
              section=EXCLUDED.section, is_seed=EXCLUDED.is_seed, seed_name=EXCLUDED.seed_name,
              source=EXCLUDED.source, published=EXCLUDED.published,
              clf_reason=EXCLUDED.clf_reason, clf_board=EXCLUDED.clf_board
        """, (aid, board, section, is_seed, seed_name, source,
              (published or None), clf_reason, clf_board))
    conn.commit()


def _board_pool_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM board_paper")
        return cur.fetchone()[0]


def ensure_pool(conn, window_years=2, refresh=False):
    """判定一次、两板块共用：广召回→预筛→sol 判定，把种子(classics)+命中候选(latest) 落 board_paper。
    board_paper 非空且未 --refresh-pool 则跳过（增量/断点续跑，省去重复 sol 判定）。"""
    if _board_pool_count(conn) > 0 and not refresh:
        print(f"[boards] board_paper 已有 {_board_pool_count(conn)} 行，跳过判定（--refresh-pool 可强制重判）。")
        return
    print("[boards] 首次判定候选池（广召回→预筛→sol 过滤+分类）…")
    seed_meta, cand = retrieve_all(window_years)
    pre = [m for m in cand if _prefilter(m)]
    print(f"[boards] 候选 {len(cand)} → 预筛 {len(pre)} → sol 判定中…")

    # 种子：人工归属为准，section=classics；judge 仅做 sanity（report-and-keep）
    for bd in ("A", "B"):
        for aid, name in SEEDS[bd].items():
            m = seed_meta.get(aid) or {}
            fm, jb, reason = judge_llm(m) if m else (True, bd, "（种子取数失败，跳过 sanity）")
            note = reason
            if jb and jb != bd:
                note = f"[sanity 分歧:sol判={jb}] {reason}"
                print(f"  ⚠ 种子 {aid} {name}: 人工={bd} 但 sol={jb}（report-and-keep）")
            elif not fm:
                note = f"[sanity:sol判为非因子挖掘] {reason}"
                print(f"  ⚠ 种子 {aid} {name}: sol 判为非因子挖掘（report-and-keep）")
            _persist_paper(conn, aid, bd, "classics", True, name, "seed",
                           (m.get("published") or "")[:10] or None, note, jb)

    # 候选：sol 判定，is_factor_mining 且有 board → latest
    kept = {"A": 0, "B": 0}
    for m in pre:
        fm, b, reason = judge_llm(m)
        if fm and b:
            _persist_paper(conn, m["arxiv_id"], b, "latest", False, None, "keyword/broad",
                           (m.get("published") or "")[:10] or None, reason, b)
            kept[b] += 1
    print(f"[boards] 判定完成：latest 命中 A={kept['A']} B={kept['B']}；board_paper 共 {_board_pool_count(conn)} 行。")


def load_board(conn, board):
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, section, is_seed, seed_name, published, clf_reason, clf_board
                       FROM board_paper WHERE board=%s""", (board,))
        rows = cur.fetchall()
    classics, latest = [], []
    for aid, section, is_seed, seed_name, published, clf_reason, clf_board in rows:
        d = {"arxiv_id": aid, "section": section, "is_seed": is_seed, "seed_name": seed_name,
             "published": str(published) if published else None,
             "clf_reason": clf_reason, "clf_board": clf_board}
        (classics if section == "classics" else latest).append(d)
    return classics, latest


def _get_board_score(conn, aid, board):
    with conn.cursor() as cur:
        cur.execute("""SELECT composite_score, freshness_score, freshness_mode, repro_score,
                              novelty_total, paper_type, domain_relevance_score, authority_score,
                              authority_na, composite_reason, study_complete, study_path, themed_path,
                              score_path
                       FROM board_score WHERE arxiv_id=%s AND board=%s""", (aid, board))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["composite_score", "freshness_score", "freshness_mode", "repro_score", "novelty_total",
            "paper_type", "domain_relevance_score", "authority_score", "authority_na",
            "composite_reason", "study_complete", "study_path", "themed_path", "score_path"]
    return dict(zip(keys, r))


def _persist_score(conn, aid, board, section, res, composite, fresh_mode):
    norm = res["norm"]
    auth = res.get("authority") or {}
    dom = res.get("domain_relevance") or {}
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO board_score
              (arxiv_id, board, freshness_score, freshness_mode, repro_score, novelty_total,
               paper_type, domain_relevance_score, authority_score, authority_na, authority_venue,
               composite_score, composite_reason, score_path, scored_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (arxiv_id, board) DO UPDATE SET
              freshness_score=EXCLUDED.freshness_score, freshness_mode=EXCLUDED.freshness_mode,
              repro_score=EXCLUDED.repro_score, novelty_total=EXCLUDED.novelty_total,
              paper_type=EXCLUDED.paper_type, domain_relevance_score=EXCLUDED.domain_relevance_score,
              authority_score=EXCLUDED.authority_score, authority_na=EXCLUDED.authority_na,
              authority_venue=EXCLUDED.authority_venue, composite_score=EXCLUDED.composite_score,
              composite_reason=EXCLUDED.composite_reason, score_path=EXCLUDED.score_path,
              scored_at=NOW()
        """, (aid, board, res["fresh"]["score"], fresh_mode, res["repro_total"],
              res["novelty_total"], norm["paper_type"],
              dom.get("score"), (None if auth.get("na") else auth.get("score")),
              bool(auth.get("na", True)), auth.get("venue"),
              (None if composite["na"] else composite["score"]),
              composite["reason"] or None, res["path"]))
    conn.commit()


# ---------------------------------------------------------------------------
# 阶段①：评分（+综评）→ 两区排名（深读前给人过目）
# ---------------------------------------------------------------------------
def score_one(conn, aid, board, section):
    """对单篇：generate_score(按区切 freshness，交叉复核走 sol) → generate_composite → 落 board_score。
    空签名/异常 → 抛出交由上游跳过（绝不伪造 0）。"""
    mode = "relaxed" if section == "classics" else "steep"
    fc, fs = (RELAXED["fresh_center"], RELAXED["fresh_scale"]) if section == "classics" else (45, 15)
    res = scorer.generate_score(aid, cross_check_on=True, model=scorer.MAIN_MODEL,
                                fresh_center=fc, fresh_scale=fs, cross_model=CROSS_MODEL)
    meta = dict(res["meta"])
    meta["area"] = meta.get("area") or "、".join(meta.get("categories") or [])
    norm = res["norm"]
    auth = res.get("authority") or {}
    sub = {
        "freshness": float(res["fresh"]["score"]),
        "reproducibility": float(res["repro_total"]),
        "novelty": float(res["novelty_total"]),
        "paper_type": norm["paper_type"],
        "domain_relevance": (res.get("domain_relevance") or {}).get("score"),
        "authority": ("N/A" if auth.get("na") else auth.get("score")),
    }
    composite = scorer.generate_composite(meta, sub)
    conn = db.ensure(conn)
    _persist_score(conn, aid, board, section, res, composite, mode)
    return composite, res


def score_stage(conn, board, latest_top, study_per_section, window_years, refresh_pool):
    ensure_pool(conn, window_years, refresh_pool)
    classics, latest = load_board(conn, board)
    print(f"\n[boards] Board {board}：classics(seeds) {len(classics)} 篇，latest {len(latest)} 篇 → 评分（增量）")

    failed = []
    for section, items in (("classics", classics), ("latest", latest)):
        for d in items:
            aid = d["arxiv_id"]
            prev = _get_board_score(conn, aid, board)
            if prev and prev.get("composite_score") is not None:
                print(f"  [skip] {aid}（{section}）已评分，跳过")
                continue
            print(f"  [score] {aid}（{section}，freshness={'relaxed' if section=='classics' else 'steep'}）…", flush=True)
            for attempt in range(2):
                try:
                    comp, _ = score_one(conn, aid, board, section)
                    print(f"    → composite={'N/A' if comp['na'] else comp['score']}  {(comp['reason'] or '')[:44]}")
                    break
                except Exception as e:
                    kind = run._classify_study_error(e) if hasattr(run, "_classify_study_error") else "content"
                    if kind == "relay" and attempt == 0:
                        print(f"    [relay] {aid} 503/超时，退避 60s 重试一次…（{str(e)[:50]}）", flush=True)
                        time.sleep(60)
                        conn = db.ensure(conn)
                        continue
                    print(f"    [FAIL] {aid} 评分失败，跳过（{str(e)[:60]}）")
                    failed.append((aid, section, str(e)[:60]))
                    break

    _print_rankings(conn, board, latest_top, study_per_section)
    if failed:
        print(f"\n[boards] 评分失败 {len(failed)} 篇（未落库，可重跑本阶段增量补齐）：")
        for aid, section, e in failed:
            print(f"  ✗ {aid}（{section}）：{e}")
    print(f"\n[boards] ↑ 审阅两区排名 + 深读目标（每区前 {study_per_section}）。确认后运行："
          f"\n         python daily_bot/run_boards.py --board {board} --study")


def _ranked_section(conn, board, section):
    with conn.cursor() as cur:
        cur.execute("""SELECT bp.arxiv_id, bp.seed_name, bp.published, bs.composite_score,
                              bs.freshness_score, bs.freshness_mode, bs.repro_score, bs.novelty_total,
                              bs.domain_relevance_score, bs.authority_score, bs.authority_na,
                              bs.study_complete
                       FROM board_paper bp LEFT JOIN board_score bs
                         ON bp.arxiv_id=bs.arxiv_id AND bp.board=bs.board
                       WHERE bp.board=%s AND bp.section=%s""", (board, section))
        rows = cur.fetchall()
    # composite desc；None（未评/na）置底
    rows.sort(key=lambda r: (r[3] is not None, r[3] if r[3] is not None else -1), reverse=True)
    return rows


def _print_rankings(conn, board, latest_top, study_per_section):
    label = {"A": "传统因子挖掘（GP/符号回归/RL，无 LLM）", "B": "LLM 因子挖掘"}[board]
    print(f"\n{'='*74}\n  Board {board} — {label}\n{'='*74}")
    for section, title, cap in (("classics", "⭐ 经典必读（relaxed freshness 365/600，按 composite 质量排）", None),
                                ("latest", "🆕 最新进展（steep freshness，按 composite 排）", latest_top)):
        rows = _ranked_section(conn, board, section)
        if cap:
            rows = rows[:cap]
        print(f"\n{title}  [{len(rows)} 篇]")
        print(f"  {'#':>2} {'comp':>4} {'fresh':>5} {'repr':>4} {'novl':>4} {'domR':>4} {'auth':>4}  {'id':11} title")
        for i, r in enumerate(rows, 1):
            (aid, seed_name, pub, comp, fresh, fmode, repro, novl, domr, auth, auth_na, done) = r
            star = " ★深读" if i <= study_per_section else ""
            comp_s = f"{comp:.1f}" if comp is not None else " na"
            auth_s = " na" if auth_na else (f"{auth:.1f}" if auth is not None else " -")
            name = (seed_name + "｜") if seed_name else ""
            print(f"  {i:>2} {comp_s:>4} {str(fresh) if fresh is not None else '-':>5} "
                  f"{str(repro) if repro is not None else '-':>4} {str(novl) if novl is not None else '-':>4} "
                  f"{str(domr) if domr is not None else '-':>4} {auth_s:>4}  {aid:11} {name}{'★' if done else ''}")
            print(f"       {star or '':6}({str(pub)}) {(_title_of(conn, aid))[:60]}")


_TITLE_CACHE = {}


def _title_of(conn, aid):
    if aid not in _TITLE_CACHE:
        m = fetch_ids([aid]).get(aid) or {}
        _TITLE_CACHE[aid] = m.get("title") or ""
    return _TITLE_CACHE[aid]


# ---------------------------------------------------------------------------
# 阶段②：深读（sol + theme-summary）→ 概览 → COS 双链（webhook 空=只生成）
# ---------------------------------------------------------------------------
def _study_targets(conn, board, study_per_section):
    targets = []
    for section in ("classics", "latest"):
        rows = _ranked_section(conn, board, section)
        rows = [r for r in rows if r[3] is not None]  # 只在已评分里挑
        for r in rows[:study_per_section]:
            targets.append((r[0], section))
    return targets


def study_stage(conn, board, study_per_section, latest_top, webhook):
    import exp_theme_summary
    import assemble
    import cos_upload
    targets = _study_targets(conn, board, study_per_section)
    if not targets:
        print("[boards] 无已评分目标，请先运行 --score。")
        return
    print(f"[boards] Board {board} 深读目标 {len(targets)} 篇：{[t[0] for t in targets]}")

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board_study_retry.log")
    BACKOFF = [10, 15, 20, 25, 30]
    start = time.time()
    CAP = 5.0 * 3600
    pending = list(targets)
    consec_relay, content_fails = 0, {t[0]: 0 for t in targets}
    completed = []

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # 增量：已完整的直接跳过
    for aid, section in list(pending):
        sc = _get_board_score(conn, aid, board) or {}
        sp = sc.get("study_path")
        if sc.get("study_complete") and sp and os.path.exists(sp) and run._count_big_sections(sp) >= 4:
            completed.append((aid, section))
            pending.remove((aid, section))
            logln(f"paper={aid} outcome=already-complete")

    rnd = 0
    while pending and (CAP - (time.time() - start)) > 0:
        rnd += 1
        for aid, section in list(pending):
            if (CAP - (time.time() - start)) <= 0:
                break
            try:
                res = deep_study.generate_study(aid, model=STUDY_MODEL)
                path = res.get("path") if isinstance(res, dict) else None
                nbig = run._count_big_sections(path) if path else 0
                begging = run._has_begging(path) if path else False
                if nbig >= 4 and not begging:
                    # theme-summary 概览层（sol）+ 评分卡前置
                    themed = None
                    try:
                        tr = exp_theme_summary.generate_theme_study(aid, model=STUDY_MODEL)
                        themed = tr.get("path") if isinstance(tr, dict) else None
                    except Exception as e:
                        logln(f"paper={aid} theme-summary 失败（{str(e)[:50]}），仅用 per-section 深读")
                    sc = _get_board_score(conn, aid, board) or {}
                    try:
                        assemble.prepend_score_card_to_study(themed or path, sc.get("score_path"))
                    except Exception as e:
                        logln(f"paper={aid} 评分卡前置失败（{str(e)[:40]}）")
                    conn = db.ensure(conn)
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE board_score SET study_path=%s, themed_path=%s,
                                       study_complete=TRUE WHERE arxiv_id=%s AND board=%s""",
                                    (path, themed, aid, board))
                    conn.commit()
                    completed.append((aid, section))
                    pending.remove((aid, section))
                    consec_relay = 0
                    logln(f"round={rnd} paper={aid} outcome=success nbig={nbig}")
                else:
                    content_fails[aid] += 1
                    why = "begging" if begging else f"nbig={nbig}<4"
                    if content_fails[aid] >= 3:
                        pending.remove((aid, section))
                    logln(f"round={rnd} paper={aid} outcome=incomplete({why}) attempt={content_fails[aid]}")
            except Exception as e:
                if run._classify_study_error(e) == "relay":
                    consec_relay += 1
                    rest = min(BACKOFF[min(consec_relay - 1, len(BACKOFF) - 1)] * 60,
                               max(0, CAP - (time.time() - start)))
                    logln(f"round={rnd} paper={aid} outcome=503/timeout rest={int(rest/60)}m ({str(e)[:50]})")
                    if rest > 0:
                        time.sleep(rest)
                else:
                    content_fails[aid] += 1
                    if content_fails[aid] >= 3:
                        pending.remove((aid, section))
                    logln(f"round={rnd} paper={aid} outcome=content-error attempt={content_fails[aid]} ({str(e)[:50]})")

    logln(f"FINISHED board={board} completed={[a for a,_ in completed]} pending={[a for a,_ in pending]}")

    # 概览 + COS 双链（webhook 空 → 只生成本地文件与链接，不推送）
    overview = _build_board_overview(conn, board, latest_top)
    date_str = datetime.date.today().isoformat()
    links = []
    try:
        ov_pv, ov_dl = cos_upload.upload_and_links(overview, f"{date_str}_Board{board}_overview.html")
        links.append(("概览", ov_pv, ov_dl))
    except Exception as e:
        print(f"[boards] 概览 COS 上传失败：{e}")
    for aid, section in completed:
        sc = _get_board_score(conn, aid, board) or {}
        sp = sc.get("themed_path") or sc.get("study_path")
        if sp and os.path.exists(sp):
            try:
                pv, dl = cos_upload.upload_and_links(sp, f"{date_str}_Board{board}_{aid.replace('/','_')}_study.html")
                links.append((aid, pv, dl))
            except Exception as e:
                print(f"[boards] {aid} COS 上传失败：{e}")

    print(f"\n[boards] Board {board} 生成完成。概览：{overview}")
    print(f"[boards] COS 链接（{len(links)} 项，预览 inline / 下载 attachment）：")
    for name, pv, dl in links:
        print(f"  {name}: 预览 {pv}\n         下载 {dl}")
    if webhook:
        print(f"[boards] （webhook 已提供，但本次为首轮，仍只生成不自动推送——请人工确认后推送。）")
    else:
        print(f"[boards] webhook 为空 → 只生成、不推送。请人工审阅两个板块后手动推送到两个群。")
    return {"board": board, "completed": [a for a, _ in completed], "overview": overview, "links": links}


def _build_board_overview(conn, board, latest_top):
    """自包含板块概览（隔离，不用 daily 的 assemble_overview）：标题 + 两区 composite 排名 + ★深读标记。"""
    label = {"A": "传统因子挖掘（GP / 符号回归 / RL，方法无 LLM）",
             "B": "LLM 因子挖掘（方法用到大语言模型）"}[board]
    date_str = datetime.date.today().isoformat()
    parts = [f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>主题榜单 Board {board} · {label}</title>",
             "<style>body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
             "max-width:920px;margin:24px auto;padding:0 16px;line-height:1.6;color:#1a1a1a}"
             "h1{font-size:22px}h2{font-size:18px;margin-top:28px;border-bottom:2px solid #eee;padding-bottom:6px}"
             "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid #eee;"
             "padding:6px 8px;text-align:left;vertical-align:top}th{background:#fafafa}"
             ".c{font-weight:700;color:#b8860b}.st{color:#c0392b;font-weight:700}</style></head><body>"]
    parts.append(f"<h1>📊 主题榜单 · Board {board}</h1>")
    parts.append(f"<p><b>{label}</b> · 生成日 {date_str} · 与每日流水线完全隔离（board_* 表）。</p>")
    for section, title in (("classics", "⭐ 经典必读（按 composite 质量排；relaxed 新鲜度不压老经典）"),
                           ("latest", "🆕 最新进展（按 composite 排；steep 新鲜度）")):
        rows = _ranked_section(conn, board, section)
        if section == "latest":
            rows = rows[:latest_top]
        parts.append(f"<h2>{title}</h2><table><tr><th>#</th><th>综评</th><th>论文</th>"
                     "<th>新鲜/复现/新颖/领域/权威</th></tr>")
        for i, r in enumerate(rows, 1):
            (aid, seed_name, pub, comp, fresh, fmode, repro, novl, domr, auth, auth_na, done) = r
            comp_s = f"<span class='c'>{comp:.1f}</span>" if comp is not None else "na"
            st = " <span class='st'>★深读</span>" if done else ""
            name = (f"<b>{seed_name}</b> · " if seed_name else "")
            sub = (f"{fresh if fresh is not None else '-'} / {repro if repro is not None else '-'} / "
                   f"{novl if novl is not None else '-'} / {domr if domr is not None else '-'} / "
                   f"{'na' if auth_na else (auth if auth is not None else '-')}")
            parts.append(f"<tr><td>{i}</td><td>{comp_s}</td>"
                         f"<td>{name}<a href='https://arxiv.org/abs/{aid}'>{aid}</a>{st}<br>"
                         f"<small>{_title_of(conn, aid)}</small></td><td>{sub}</td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    out = os.path.join(scorer.OUTPUT_DIR, f"{date_str}_Board{board}_overview.html")
    os.makedirs(scorer.OUTPUT_DIR, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return out


def main():
    args = sys.argv[1:]
    if "--board" not in args:
        print("用法: python daily_bot/run_boards.py --board A|B "
              "[--dry-run | --score | --study] [--latest-top 30] [--study-per-section 3] "
              "[--window-years 2] [--refresh-pool] [--webhook URL]")
        sys.exit(1)
    board = args[args.index("--board") + 1].strip().upper()
    if board not in ("A", "B"):
        print("--board 必须是 A 或 B"); sys.exit(1)

    def _argi(name, d):
        return int(args[args.index(name) + 1]) if name in args else d
    def _args(name, d):
        return args[args.index(name) + 1] if name in args else d
    latest_top = _argi("--latest-top", 30)
    study_ps = _argi("--study-per-section", 3)
    window_years = _argi("--window-years", 2)
    refresh_pool = "--refresh-pool" in args
    webhook = _args("--webhook", "").strip()

    if "--dry-run" in args:
        dry_run(board, study_ps, latest_top, window_years)
        return

    conn = db.get_connection()
    if "--score" in args:
        score_stage(conn, board, latest_top, study_ps, window_years, refresh_pool)
    elif "--study" in args:
        study_stage(conn, board, study_ps, latest_top, webhook)
    else:
        print("请指定阶段：--dry-run（预览）| --score（评分+排名，深读前过目）| --study（深读+概览+COS）")


if __name__ == "__main__":
    main()
