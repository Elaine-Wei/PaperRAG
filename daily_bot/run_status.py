#!/usr/bin/env python3
"""
run_status —— 一次性【主题榜单】：市场状态与风格识别 / Market Regime & Style Detection，
3 个【加权、有序】分区（复用 run_sttf 的分区机制），按对量化的相关度排序：深度学习最前、风格轮动其后，权重/条数递减。
与每日流水线(daily_*)、其它主题(selfevo/sttf)【完全隔离】：只写 topic_* 表，topic='status' + section 列。

分区（权重=条数上限 cap + 展示顺序，非分数混合；每区独立 sol-judge keep/drop、独立 composite 排序）：
  1 深度学习 regime 识别 ★core  steep  cap12 study2 win2yr —— 4 seeds + 关键词网
  2 统计/经典方法              relaxed cap8 study1 win6yr —— 无 seed（HMM/Markov-switching 多为期刊/书；关键词网），宽 freshness+宽窗口取经典
  3 风格轮动/因子择时          steep  cap4 study1 win2yr —— 无 seed，关键词网
研究分配：共 4 篇 = 深度学习 2 + 统计 1 + 风格 1。逐-paper sol↔terra 轮换（开启）。
取数按 6 个类别 cat-scoped 检索（cs.LG/q-fin.ST/q-fin.CP/q-fin.TR/econ.EM/stat.ML），滤掉量子HMM/天体GMM等跨域噪声。

用法（分阶段，与 sttf 一致）：
  python daily_bot/run_status.py --dry-run     # 逐区 seeds+关键词 → 预筛 → sol keep/drop（写池，无评分/深读/推送）
  python daily_bot/run_status.py --score       # 逐区 freshness + composite 排序 + cap（写分数）
  python daily_bot/run_status.py --study        # 深读 2+1+1（sol↔terra）→ 3 区有序概览 → COS 双链（generate-only）
  flags: --refresh-pool  --no-model-rotation  --webhook URL
"""

import datetime
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run            # noqa: E402  加载 .env + parse_arxiv_xml + _classify_study_error/_count_big_sections/_has_begging
import db             # noqa: E402
import scorer         # noqa: E402
import deep_study     # noqa: E402
import run_boards     # noqa: E402  复用 fetch_ids / fetch_search / _title_of

TOPIC = "status"
TOPIC_LABEL = "市场状态与风格识别 / Market Regime & Style Detection"
STUDY_MODEL = "gpt-5.6-sol"
CROSS_MODEL = "gpt-5.6-sol"
JUDGE_MODEL = "gpt-5.6-sol"
SCORE_MAIN = "gpt-5.6-sol"
MODEL_ROTATION = ["gpt-5.6-sol", "gpt-5.6-terra"]
STUDY_MODEL_ROTATION = True       # 本主题【开启】逐-paper sol↔terra 轮换（活测；--no-model-rotation 可关）
STEEP = {"fresh_center": 45, "fresh_scale": 15}
RELAXED = {"fresh_center": 365, "fresh_scale": 600}

# --------- 逐区 sol-judge 系统提示（keep/drop；S1 额外注明架构 + asset-agnostic） ---------
# S1 核心原则（advisor 定调）：regime/state/style 识别是【底层工具】，优先跨市场通用（asset-agnostic）方法。
_J_DL = (
    "判断这篇论文是否属于【识别市场状态 / regime / 风格 的方法（深度学习方向）】——"
    "即用神经网络/表示学习/状态嵌入等识别或切换市场状态（regime-aware Transformer、autoencoder-gated、"
    "状态空间、regime-switching 神经网络等）。\n"
    "属于→keep=true；只做普通收益预测而不显式识别状态、与市场状态无关→false。\n"
    "keep=true 时：(a) arch 注明架构（Transformer / autoencoder / 状态空间 / RL / 聚类 / 混合 等）；"
    "(b) reason 里【务必注明该方法是否 asset-agnostic 跨市场通用】——能用于股/期/期权/加密等多市场为通用(强)，"
    "绑定单一资产为专用(仍可 keep 但注明)；这是底层工具的关键。\n"
    '只输出 JSON：{"keep":true或false,"arch":"架构或空串","reason":"一句中文，注明架构+是否跨市场通用"}')
_J_STAT = (
    "判断这篇是否属于【统计/经典 市场状态识别方法】——HMM、Markov-switching、GMM 高斯混合、聚类、"
    "变点检测(change-point) 等用于金融市场 regime 识别/切换。\n"
    "属于→keep=true；与市场状态识别无关、纯理论无金融应用、非上述方法族→false。\n"
    "keep=true 时 arch 注明方法（HMM / Markov-switching / GMM / 聚类 / 变点检测 等），"
    "reason 里注明是否 asset-agnostic 跨市场通用。\n"
    '只输出 JSON：{"keep":true或false,"arch":"方法或空串","reason":"一句中文，注明方法+是否跨市场通用"}')
_J_STYLE = (
    "判断这篇是否属于【风格轮动 / 因子择时 / 波动率状态】——style rotation、factor timing、sector rotation、"
    "volatility regime 等择时/轮动/状态驱动的配置方法。\n"
    "属于→keep=true；普通因子选股而无择时/轮动/状态、与之无关→false。\n"
    "keep=true 时 arch 注明方法，reason 里注明是否 asset-agnostic 跨市场通用。\n"
    '只输出 JSON：{"keep":true或false,"arch":"方法或空串","reason":"一句中文，注明方法+是否跨市场通用"}')

SECTIONS = [
    {"key": "dl", "title": "深度学习 regime 识别 ★core", "order": 1, "fresh": "steep",
     "cap": 12, "study_n": 2, "window_years": 2, "judge": _J_DL,
     "seeds": {"2606.29347": "Regime-Gated Attention Transformer",
               "2603.19136": "Regime-Aware Autoencoder Dual-Node",
               "2410.22346": "Representation Learning Regime (U-SPDNet)",
               "2512.03298": "Deep Switching State-Space + Conformal"},
     "keywords": ["market regime detection deep learning", "regime-aware transformer",
                  "regime switching neural", "market state embedding"]},
    {"key": "stat", "title": "统计/经典方法", "order": 2, "fresh": "relaxed",
     "cap": 8, "study_n": 1, "window_years": 6, "judge": _J_STAT,
     "seeds": {},
     "keywords": ["hidden markov model market regime", "markov switching financial",
                  "change point detection market", "gaussian mixture regime"]},
    {"key": "style", "title": "风格轮动/因子择时", "order": 3, "fresh": "steep",
     "cap": 4, "study_n": 1, "window_years": 2, "judge": _J_STYLE,
     "seeds": {},
     "keywords": ["style rotation factor timing", "factor timing regime",
                  "sector rotation prediction", "volatility regime"]},
]
SEC_BY_KEY = {s["key"]: s for s in SECTIONS}

# 种子标题校验关键词（不含则告警，keyword-fallback；仍保留）
_EXPECT_KW = {
    "2606.29347": "regime-gated", "2603.19136": "regime-aware",
    "2410.22346": "regime detection", "2512.03298": "regime-switching",
}

# 便宜预筛：省 LLM 调用。命中目标学科分类（含 econ）或 regime/市场状态词即放行。
_PRE_RE = re.compile(
    r"(regime|market state|market condition|regime[- ]?switch|change[- ]?point|"
    r"hidden markov|markov[- ]?switch|gaussian mixture|style rotation|factor timing|"
    r"sector rotation|volatility regime|市场状态|市场风格|状态切换|风格轮动|波动率状态|因子择时)", re.I)
_PRE_CATS = ("q-fin", "cs.", "econ", "stat.", "eess")   # +econ（Markov-switching 多在 econ.EM）


def _prefilter(meta):
    if any((c or "").startswith(_PRE_CATS) for c in (meta.get("categories") or [])):
        return True
    return bool(_PRE_RE.search(f"{meta.get('title','')} {meta.get('abstract','')}"))


# ---------------------------------------------------------------------------
# sol 判官（逐区 keep/drop；金融区解析 arch）
# ---------------------------------------------------------------------------
def judge(meta, judge_sys, model=JUDGE_MODEL):
    """一次 sol 调用 keep/drop（+金融 arch）。返回 (keep:bool, arch:str, reason:str)。503 退避重试。"""
    import relay
    user = f"标题：{meta.get('title')}\n摘要：{(meta.get('abstract') or '')[:1400]}"
    for k in range(3):
        try:
            content, _ = relay.relay_chat(judge_sys, user, temperature=0, model=model, max_tokens=220)
            obj = relay.extract_json(content)
            if obj is not None and "keep" in obj:
                return (bool(obj.get("keep")), (obj.get("arch") or "").strip(),
                        (obj.get("reason") or "").strip())
        except Exception as e:
            print(f"    [judge] {meta.get('arxiv_id')} 异常({str(e)[:35]})，退避 {k+1}/3", flush=True)
            time.sleep(20 * (k + 1))
    return False, "", "（判定失败）"


# ---------------------------------------------------------------------------
# 取数（逐区 seeds + 关键词网；跨区按 order 优先去重：高优先区先占坑，如 dl 赢 style）
# ---------------------------------------------------------------------------
_TITLE = {}   # aid -> title（取数时缓存，dry-run 免二次 arXiv）


def _cache_title(m):
    if m and m.get("arxiv_id"):
        _TITLE[m["arxiv_id"]] = m.get("title") or ""


def _title_of(conn, aid):
    if aid in _TITLE:
        return _TITLE[aid]
    return run_boards._title_of(conn, aid)


# cat-scoped 检索：把召回限定到 regime 工作真正所在的 6 个类别，滤掉量子HMM/天体GMM等跨域噪声。
FETCH_CATS = ["cs.LG", "q-fin.ST", "q-fin.CP", "q-fin.TR", "econ.EM", "stat.ML"]
_CAT_CLAUSE = "(" + " OR ".join(f"cat:{c}" for c in FETCH_CATS) + ")"


def _fetch_search(query, n=50):
    """相关度检索，但 AND 限定到 FETCH_CATS 6 类（regime 工作真正所在）——比 all-fields 更干净。"""
    sq = f"(all:{query}) AND {_CAT_CLAUSE}"
    data = run_boards._get(run_boards.ARXIV_API + "?" + urllib.parse.urlencode(
        {"search_query": sq, "start": 0, "max_results": n,
         "sortBy": "relevance", "sortOrder": "descending"}))
    time.sleep(1.0)   # 尊重 arXiv 限速
    return run.parse_arxiv_xml(data) if data else []


def retrieve_section(sec, window_years, seen):
    """返回 (seed_meta:{id:meta}, cand:[meta])。seeds 按 ID 取+校验；候选走 cat-scoped 关键词网。
    窗口按【本区 window_years】（S2 统计经典=6yr，S1/S3=2yr）；seen 跨区按 order 优先去重。"""
    today = datetime.date.today()
    wy = sec.get("window_years", window_years)
    cutoff = today - datetime.timedelta(days=int(wy * 365))
    seed_meta = run_boards.fetch_ids(list(sec["seeds"])) if sec["seeds"] else {}
    for aid, name in sec["seeds"].items():
        m = seed_meta.get(aid)
        if not m:
            print(f"  ⚠ [{sec['key']}] 种子 {aid}（{name}）取数失败——建议 keyword-fallback")
            continue
        _cache_title(m)
        kw = _EXPECT_KW.get(aid, "")
        if kw and kw.lower() not in (m.get("title") or "").lower():
            print(f"  ⚠ [{sec['key']}] 种子 {aid}（{name}）标题校验不符：{m.get('title')!r}（保留，待核）")
    pool = {}
    for q in sec["keywords"]:
        for m in _fetch_search(q, 50):
            aid = m["arxiv_id"]
            if aid in sec["seeds"] or aid in seen or aid in pool:
                continue
            try:
                if datetime.date.fromisoformat((m.get("published") or "")[:10]) < cutoff:
                    continue
            except Exception:
                continue
            _cache_title(m)
            pool[aid] = m
    return seed_meta, list(pool.values())


# ---------------------------------------------------------------------------
# 隔离持久化：topic_paper（topic='status' + section）
# ---------------------------------------------------------------------------
def _persist_paper(conn, aid, section, is_seed, seed_name, source, published, keep, reason):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO topic_paper
              (topic, arxiv_id, section, is_seed, seed_name, source, published, judge_keep, judge_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (topic, arxiv_id) DO UPDATE SET
              section=EXCLUDED.section, is_seed=EXCLUDED.is_seed, seed_name=EXCLUDED.seed_name,
              source=EXCLUDED.source, published=EXCLUDED.published,
              judge_keep=EXCLUDED.judge_keep, judge_reason=EXCLUDED.judge_reason
        """, (TOPIC, aid, section, is_seed, seed_name, source, (published or None), keep, reason))
    conn.commit()


def _pool_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM topic_paper WHERE topic=%s", (TOPIC,))
        return cur.fetchone()[0]


def ensure_pool(conn, window_years=2, refresh=False):
    """逐区判定并落 topic_paper：seeds(keep=true,含 sanity 备注) + 候选(sol keep/drop)。
    已有 status 行且未 --refresh-pool → 跳过（复用已审阅池）。跨区按 order 优先去重。"""
    if _pool_count(conn) > 0 and not refresh:
        print(f"[status] topic_paper 已有 {_pool_count(conn)} 行，跳过判定（--refresh-pool 强制重判）。")
        return
    if refresh:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM topic_paper WHERE topic=%s", (TOPIC,))
        conn.commit()
    seen = set()
    for sec in SECTIONS:   # 按 order 处理，高优先区先占坑
        for aid in sec["seeds"]:
            seen.add(aid)
    for sec in sorted(SECTIONS, key=lambda s: s["order"]):
        print(f"\n[status] === 分区 {sec['order']} {sec['title']} 判定 ===")
        seed_meta, cand = retrieve_section(sec, window_years, seen)
        # 种子：report-and-keep（judge 仅 sanity）
        for aid, name in sec["seeds"].items():
            m = seed_meta.get(aid) or {}
            if m:
                keep, arch, reason = judge(m, sec["judge"])
                note = (f"[arch:{arch}] " if arch else "") + reason
                if not keep:
                    note = "[sanity:sol判否] " + note
                    print(f"  ⚠ 种子 {aid} {name}: sol 判否（report-and-keep）")
            else:
                note = "（种子取数失败，保留占位）"
            _persist_paper(conn, aid, sec["key"], True, name, "seed",
                           (m.get("published") or "")[:10] or None, True, note)
        # 候选：预筛 → sol keep/drop
        pre = [m for m in cand if _prefilter(m)]
        print(f"  候选 {len(cand)} → 预筛 {len(pre)} → sol 判定…")
        kept = 0
        for i, m in enumerate(pre, 1):
            aid = m["arxiv_id"]
            if aid in seen:               # 已被更高优先区占用
                continue
            keep, arch, reason = judge(m, sec["judge"])
            note = (f"[arch:{arch}] " if arch else "") + reason
            _persist_paper(conn, aid, sec["key"], False, None, "keyword",
                           (m.get("published") or "")[:10] or None, keep, note)
            if keep:
                seen.add(aid)             # keep 才占坑（drop 的可留给后续区再试）
                kept += 1
        print(f"  → {sec['title']}: keep {kept}/{len(pre)}")
    print(f"\n[status] 判定完成：topic_paper 共 {_pool_count(conn)} 行。")


# ---------------------------------------------------------------------------
# DRY-RUN：逐区 keep/drop 池（无评分/深读/推送）
# ---------------------------------------------------------------------------
def _rows_for_section(conn, key):
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name, published, judge_keep, judge_reason
                       FROM topic_paper WHERE topic=%s AND section=%s""", (TOPIC, key))
        return cur.fetchall()


def dry_run(window_years, refresh):
    print(f"\n===== DRY-RUN 主题榜单 [status] {TOPIC_LABEL} =====")
    print("逐区（seeds 按 ID 校验 + 关键词网）→ 便宜预筛 → sol keep/drop。写隔离表 topic_paper（无评分/深读/推送）。")
    ensure_pool(conn, window_years, refresh)

    for sec in sorted(SECTIONS, key=lambda s: s["order"]):
        rows = _rows_for_section(conn, sec["key"])
        seeds = [r for r in rows if r[1]]
        kept = [r for r in rows if (not r[1]) and r[4]]
        drop = [r for r in rows if (not r[1]) and (not r[4])]
        fresh_txt = "relaxed 365/600" if sec["fresh"] == "relaxed" else "steep 45/15"
        print(f"\n{'='*74}")
        print(f"  分区 {sec['order']} · {sec['title']}  "
              f"[freshness={fresh_txt} · cap={sec['cap']} · 深读={sec['study_n']}]")
        print(f"{'='*74}")
        print(f"  🌱 种子 {len(seeds)}（report-and-keep）：")
        for aid, is_seed, name, pub, keep, reason in sorted(seeds, key=lambda r: r[3] or "", reverse=True):
            print(f"    {aid:12} {str(pub or '?'):11} {name}｜{_title_of(conn, aid)[:44]}")
            if reason:
                print(f"        ← {reason[:82]}")
        print(f"  ✅ keep 候选 {len(kept)}（真跑按 composite 排序，取前 {sec['cap']}）：")
        for aid, is_seed, name, pub, keep, reason in sorted(kept, key=lambda r: r[3] or "", reverse=True):
            print(f"    {aid:12} {str(pub or '?'):11} {_title_of(conn, aid)[:50]}")
            if reason:
                print(f"        ← {reason[:82]}")
        if not kept:
            print("    （无 keep 候选——该区可能依赖 seeds 或 arXiv 供给稀缺）")
        print(f"  ❌ drop 候选 {len(drop)}（附理由供复核误杀）：")
        for aid, is_seed, name, pub, keep, reason in sorted(drop, key=lambda r: r[3] or "", reverse=True)[:12]:
            print(f"    {aid:12} {str(pub or '?'):11} {_title_of(conn, aid)[:46]}")
            if reason:
                print(f"        ✗ {reason[:78]}")
        if len(drop) > 12:
            print(f"    …另有 {len(drop)-12} 篇 drop（略）")

    total_keep = sum(len([r for r in _rows_for_section(conn, s['key']) if r[4]]) for s in SECTIONS)
    print(f"\n[汇总] 3 区 keep 合计（含种子）= {total_keep} 篇。"
          f"\n下一步（等你 green-light）：python daily_bot/run_status.py --score")


# ---------------------------------------------------------------------------
# 阶段①：评分（逐区 freshness）+ 综评 → 逐区 composite 排名 + cap
# ---------------------------------------------------------------------------
def _get_score(conn, aid):
    with conn.cursor() as cur:
        cur.execute("""SELECT composite_score, freshness_score, freshness_mode, repro_score,
                              novelty_total, domain_relevance_score, authority_score, authority_na,
                              composite_reason, study_complete, study_path, themed_path, score_path
                       FROM topic_score WHERE topic=%s AND arxiv_id=%s""", (TOPIC, aid))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["composite_score", "freshness_score", "freshness_mode", "repro_score", "novelty_total",
            "domain_relevance_score", "authority_score", "authority_na", "composite_reason",
            "study_complete", "study_path", "themed_path", "score_path"]
    return dict(zip(keys, r))


def _persist_score(conn, aid, res, composite, mode, model=None):
    norm = res["norm"]
    auth = res.get("authority") or {}
    dom = res.get("domain_relevance") or {}
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO topic_score
              (topic, arxiv_id, freshness_score, freshness_mode, repro_score, novelty_total,
               paper_type, domain_relevance_score, authority_score, authority_na, authority_venue,
               composite_score, composite_reason, score_path, model, scored_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (topic, arxiv_id) DO UPDATE SET
              freshness_score=EXCLUDED.freshness_score, freshness_mode=EXCLUDED.freshness_mode,
              repro_score=EXCLUDED.repro_score, novelty_total=EXCLUDED.novelty_total,
              paper_type=EXCLUDED.paper_type, domain_relevance_score=EXCLUDED.domain_relevance_score,
              authority_score=EXCLUDED.authority_score, authority_na=EXCLUDED.authority_na,
              authority_venue=EXCLUDED.authority_venue, composite_score=EXCLUDED.composite_score,
              composite_reason=EXCLUDED.composite_reason, score_path=EXCLUDED.score_path,
              model=EXCLUDED.model, scored_at=NOW()
        """, (TOPIC, aid, res["fresh"]["score"], mode, res["repro_total"], res["novelty_total"],
              norm["paper_type"], dom.get("score"),
              (None if auth.get("na") else auth.get("score")), bool(auth.get("na", True)),
              auth.get("venue"), (None if composite["na"] else composite["score"]),
              composite["reason"] or None, res["path"], model))
    conn.commit()


def score_one(conn, aid, sec):
    """按区 freshness（steep/relaxed）+ 交叉复核 sol → generate_score → generate_composite → 落 topic_score。
    空签名/异常 → 抛出交由上游跳过（绝不伪造 0）。"""
    fc = STEEP if sec["fresh"] == "steep" else RELAXED
    try:
        res = scorer.generate_score(aid, cross_check_on=True, model=SCORE_MAIN,
                                    fresh_center=fc["fresh_center"], fresh_scale=fc["fresh_scale"],
                                    cross_model=CROSS_MODEL)
    except Exception as e:
        # relay 走不通 → DS 直连兜底一次；DS 也不行则抛出，交上游按原逻辑跳过（绝不伪造 0）
        print(f"  [score] {aid} relay 失败（{str(e)[:60]}）")
        res, _ds = run.ds_score_fallback(aid, fresh_center=fc["fresh_center"],
                                         fresh_scale=fc["fresh_scale"])
        if not res:
            raise
    model_used = res.get("model_used") or SCORE_MAIN
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
    # 综评必须跟随 score 用同一个模型：否则 DS 兜底时这最后一步仍会打到已经死掉的 relay
    composite = scorer.generate_composite(meta, sub, model=model_used)
    conn2 = db.ensure(conn)
    _persist_score(conn2, aid, res, composite, sec["fresh"], model_used)
    return composite


def _kept_in_section(conn, key):
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name FROM topic_paper
                       WHERE topic=%s AND section=%s AND judge_keep=TRUE""", (TOPIC, key))
        return cur.fetchall()


def score_stage():
    global conn
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_score.log")

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    if _pool_count(conn) == 0:
        print("[status] 池为空，请先 --dry-run。"); return
    logln("=== score_stage status（逐区 freshness，generate-only）===")
    failed = []
    for sec in sorted(SECTIONS, key=lambda s: s["order"]):
        conn = db.ensure(conn)
        kept = _kept_in_section(conn, sec["key"])
        logln(f"--- 分区 {sec['order']} {sec['title']}：{len(kept)} 篇 keep → 评分（{sec['fresh']}，增量）")
        for aid, is_seed, name in kept:
            conn = db.ensure(conn)   # 单篇评分可跑十几分钟，pooler 会关掉空闲连接；读库前先重连
            prev = _get_score(conn, aid)
            if prev and prev.get("composite_score") is not None:
                logln(f"  [skip] {aid} 已评分")
                continue
            tag = name or "kw"
            logln(f"  [score] {aid}（{tag}）…")
            for attempt in range(2):
                try:
                    comp = score_one(conn, aid, sec)
                    logln(f"    → {aid} composite={'N/A' if comp['na'] else comp['score']}  {(comp['reason'] or '')[:40]}")
                    break
                except Exception as e:
                    kind = run._classify_study_error(e)
                    if kind == "relay" and attempt == 0:
                        logln(f"    [relay] {aid} 退避 60s 重试…（{str(e)[:50]}）")
                        time.sleep(60); conn = db.ensure(conn)
                        continue
                    logln(f"    [FAIL] {aid} 跳过（{str(e)[:60]}）")
                    failed.append((sec["key"], aid, str(e)[:60]))
                    break
    _print_rankings(conn)
    if failed:
        print(f"\n[status] 评分失败 {len(failed)} 篇（可重跑 --score 增量补齐）：")
        for k, aid, e in failed:
            print(f"  ✗ [{k}] {aid}：{e}")
    print(f"\n[status] ↑ 审阅 3 区排名 + 深读目标。确认后：python daily_bot/run_status.py --study")


def _ranked_section(conn, key):
    """按 composite 降序返回该区 kept 论文行；None（未评/na）置底。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT tp.arxiv_id, tp.is_seed, tp.seed_name, tp.published, tp.judge_reason,
                              ts.composite_score, ts.freshness_score, ts.freshness_mode, ts.repro_score,
                              ts.novelty_total, ts.domain_relevance_score, ts.authority_score,
                              ts.authority_na, ts.study_complete
                       FROM topic_paper tp LEFT JOIN topic_score ts
                         ON tp.topic=ts.topic AND tp.arxiv_id=ts.arxiv_id
                       WHERE tp.topic=%s AND tp.section=%s AND tp.judge_keep=TRUE""", (TOPIC, key))
        rows = cur.fetchall()
    rows.sort(key=lambda r: (r[5] is not None, r[5] if r[5] is not None else -1), reverse=True)
    return rows


def _study_targets(conn):
    """深读目标 = 各区 composite 前 study_n：dl 前 2 + stat 前 1 + style 前 1 = 4 篇。
    通用：按 SECTIONS 的 study_n 逐区取 composite 最高的已评分论文。"""
    targets = []
    for sec in sorted(SECTIONS, key=lambda s: s["order"]):
        n = sec.get("study_n", 0)
        if n <= 0:
            continue
        top = [r for r in _ranked_section(conn, sec["key"]) if r[5] is not None][:n]
        targets += [(r[0], sec["key"]) for r in top]
    return targets


def _print_rankings(conn):
    print(f"\n{'#'*74}\n  STATUS 3 区排名（各区独立 composite；权重=cap+顺序，非分数混合）\n{'#'*74}")
    tgt = {a for a, _ in _study_targets(conn)}
    for sec in sorted(SECTIONS, key=lambda s: s["order"]):
        rows = _ranked_section(conn, sec["key"])
        capped = rows[:sec["cap"]]
        fresh_txt = "relaxed" if sec["fresh"] == "relaxed" else "steep"
        print(f"\n{'='*74}")
        print(f"  分区 {sec['order']} · {sec['title']}  [fresh={fresh_txt} · cap={sec['cap']} · 深读={sec['study_n']}]"
              f"  —— 展示 {len(capped)}/{len(rows)}")
        print(f"{'='*74}")
        print(f"  {'#':>2} {'comp':>4} {'fr':>4} {'rp':>4} {'nv':>4} {'dR':>4} {'au':>4}  {'id':12} paper")
        for i, r in enumerate(capped, 1):
            (aid, is_seed, name, pub, jr, comp, fr, fm, rp, nv, dr, au, ana, done) = r
            star = " ★深读" if aid in tgt else ""
            seed = "🌱" if is_seed else "  "
            comp_s = f"{comp:.1f}" if comp is not None else " na"
            au_s = "na" if ana else (f"{au}" if au is not None else "-")
            nm = (name + "｜") if name else ""
            print(f"  {i:>2} {comp_s:>4} {str(fr) if fr is not None else '-':>4} "
                  f"{str(rp) if rp is not None else '-':>4} {str(nv) if nv is not None else '-':>4} "
                  f"{str(dr) if dr is not None else '-':>4} {au_s:>4}  {seed}{aid:12} {nm}{_title_of(conn, aid)[:40]}{star}")
    print(f"\n深读分配（真跑 --study）：深度学习 2 + 统计 1 + 风格 1 = 4 篇（各区 composite 最高）；"
          f"逐-paper sol↔terra 轮换（本主题开启）。")


# ---------------------------------------------------------------------------
# 阶段②：深读（2+1+1，sol↔terra 逐-paper 轮换）→ 3 区有序概览 → COS 双链（generate-only）
# ---------------------------------------------------------------------------
def _local_title(conn, aid):
    """标题优先本地评分 HTML(<h1>)（离线、免 arXiv 限流）；缺失才回退 arXiv。"""
    if aid in _TITLE:
        return _TITLE[aid]
    sc = _get_score(conn, aid) or {}
    sp = sc.get("score_path")
    if sp and os.path.exists(sp):
        try:
            h = open(sp, encoding="utf-8").read()
            m = re.search(r"<h1[^>]*>(.*?)</h1>", h, re.S)
            if m:
                _TITLE[aid] = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                return _TITLE[aid]
        except Exception:
            pass
    return run_boards._title_of(conn, aid)


def _resolve_targets(conn, override_ids=None):
    if override_ids:
        out = []
        for aid in override_ids:
            with conn.cursor() as cur:
                cur.execute("SELECT section FROM topic_paper WHERE topic=%s AND arxiv_id=%s", (TOPIC, aid))
                r = cur.fetchone()
            out.append((aid, r[0] if r else None))
        return out
    return _study_targets(conn)


def study_stage(override_ids=None, model_rotation=None, webhook=""):
    import exp_theme_summary
    import assemble
    import cos_upload
    global conn
    targets = _resolve_targets(conn, override_ids)
    if not targets:
        print("[status] 无深读目标（请先 --score）。"); return
    rotate_on = STUDY_MODEL_ROTATION if model_rotation is None else model_rotation

    def _pm(i):
        return MODEL_ROTATION[i % len(MODEL_ROTATION)] if rotate_on else STUDY_MODEL
    paper_model = {aid: _pm(i) for i, (aid, _s) in enumerate(targets)}
    print(f"[status] 深读目标 {len(targets)} 篇（rotation={'ON' if rotate_on else 'OFF'}）："
          + "，".join(f"{a}({s})->{paper_model[a]}" for a, s in targets))

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_study.log")
    BACKOFF = [10, 15, 20, 25, 30]
    start = time.time()
    CAP = 5.0 * 3600
    pending = list(targets)
    consec_relay, content_fails = 0, {a: 0 for a, _ in targets}
    relay_fails = {a: 0 for a, _ in targets}   # 逐篇 relay 类失败数 → 达阈值转 DS
    ds_tried = set()                           # 每篇最多兜底一次
    completed = []

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _finalize(aid, sec, path, model, nbig, rnd, outcome="success"):
        """深读产出合格后的收尾：theme 概览 → 评分卡前置 → 落库 → 记完成。relay/DS 两条路共用。"""
        global conn
        themed = None
        try:
            tr = exp_theme_summary.generate_theme_study(aid, model=model)
            themed = tr.get("path") if isinstance(tr, dict) else None
        except Exception as e:
            logln(f"paper={aid} theme-summary 失败（{str(e)[:50]}），仅用 per-section")
        conn = db.ensure(conn)   # 深读+theme 长跑数十分钟，DB 读写前先重连（pooler 会关空闲连接）
        sc = _get_score(conn, aid) or {}
        try:
            assemble.prepend_score_card_to_study(themed or path, sc.get("score_path"))
        except Exception as e:
            logln(f"paper={aid} 评分卡前置失败（{str(e)[:40]}）")
        conn = db.ensure(conn)
        with conn.cursor() as cur:
            cur.execute("""UPDATE topic_score SET study_path=%s, themed_path=%s, model=%s,
                           study_complete=TRUE WHERE topic=%s AND arxiv_id=%s""",
                        (path, themed, model, TOPIC, aid))
        conn.commit()
        completed.append((aid, sec))
        if (aid, sec) in pending:
            pending.remove((aid, sec))
        logln(f"round={rnd} paper={aid} model={model} outcome={outcome} nbig={nbig}")

    def _try_ds(aid, sec, rnd):
        """relay 对本篇已走不通 → DS 直连兜底一次；成功则按成功路径收尾。"""
        import relay
        if aid in ds_tried:
            return False
        ds_tried.add(aid)
        fb = run.ds_study_fallback(aid, logln, 4)
        if not fb:
            return False
        path, nbig = fb
        _finalize(aid, sec, path, relay.DS_MODEL_TAG, nbig, rnd, outcome="success-ds")
        return True

    for aid, sec in list(pending):
        sc = _get_score(conn, aid) or {}
        sp = sc.get("study_path")
        if sc.get("study_complete") and sp and os.path.exists(sp) and run._count_big_sections(sp) >= 4:
            completed.append((aid, sec)); pending.remove((aid, sec))
            logln(f"paper={aid} outcome=already-complete")

    rnd = 0
    while pending and (CAP - (time.time() - start)) > 0:
        rnd += 1
        for aid, sec in list(pending):
            if (CAP - (time.time() - start)) <= 0:
                break
            model = paper_model.get(aid, STUDY_MODEL)
            try:
                res = deep_study.generate_study(aid, model=model)
                path = res.get("path") if isinstance(res, dict) else None
                nbig = run._count_big_sections(path) if path else 0
                begging = run._has_begging(path) if path else False
                if nbig >= 4 and not begging:
                    _finalize(aid, sec, path, model, nbig, rnd)
                    consec_relay = 0
                else:
                    content_fails[aid] += 1
                    why = "begging" if begging else f"nbig={nbig}<4"
                    exhausted = content_fails[aid] >= 3
                    if exhausted and _try_ds(aid, sec, rnd):
                        continue
                    if exhausted:
                        pending.remove((aid, sec))
                    logln(f"round={rnd} paper={aid} outcome=incomplete({why}) attempt={content_fails[aid]}")
            except Exception as e:
                if run._classify_study_error(e) == "relay":
                    consec_relay += 1
                    relay_fails[aid] = relay_fails.get(aid, 0) + 1
                    logln(f"round={rnd} paper={aid} outcome=503/timeout ({str(e)[:50]})")
                    # relay 对这一篇连续不通 → 转 DS，别把 5h 全耗在退避上
                    if relay_fails[aid] >= run.DS_AFTER_RELAY_FAILS and _try_ds(aid, sec, rnd):
                        continue
                    rest = min(BACKOFF[min(consec_relay - 1, len(BACKOFF) - 1)] * 60,
                               max(0, CAP - (time.time() - start)))
                    logln(f"round={rnd} paper={aid} rest={int(rest/60)}m")
                    if rest > 0:
                        time.sleep(rest)
                else:
                    content_fails[aid] += 1
                    exhausted = content_fails[aid] >= 3
                    if exhausted and _try_ds(aid, sec, rnd):
                        continue
                    if exhausted:
                        pending.remove((aid, sec))
                    logln(f"round={rnd} paper={aid} outcome=content-error attempt={content_fails[aid]} ({str(e)[:50]})")

    logln(f"FINISHED completed={[a for a,_ in completed]} pending={[a for a,_ in pending]}")

    conn = db.ensure(conn)
    overview = _build_overview(conn)
    date_str = datetime.date.today().isoformat()
    links = []
    try:
        pv, dl = cos_upload.upload_and_links(overview, f"{date_str}_status_overview.html")
        links.append(("概览", pv, dl))
    except Exception as e:
        print(f"[status] 概览 COS 上传失败：{e}")
    for aid, sec in completed:
        sc = _get_score(conn, aid) or {}
        sp = sc.get("themed_path") or sc.get("study_path")
        if sp and os.path.exists(sp):
            try:
                pv, dl = cos_upload.upload_and_links(sp, f"{date_str}_status_{aid.replace('/','_')}_study.html")
                links.append((aid, pv, dl))
            except Exception as e:
                print(f"[status] {aid} COS 上传失败：{e}")

    print(f"\n[status] 生成完成。概览：{overview}")
    print(f"[status] COS 链接（{len(links)} 项，预览 inline / 下载 attachment）：")
    for name, pv, dl in links:
        print(f"  {name}: 预览 {pv}\n         下载 {dl}")
    print("[status] webhook 为空 → 只生成、不推送。" if not webhook
          else "[status] （首轮仍只生成不自动推送——人工确认后推送。）")
    return {"completed": [a for a, _ in completed], "overview": overview, "links": links}


def _build_overview(conn):
    """自包含 3 区有序概览（隔离）：每区 composite 排名 + cap + ★深读徽标；架构注记。"""
    date_str = datetime.date.today().isoformat()
    tgt = {a for a, _ in _study_targets(conn)}
    with conn.cursor() as cur:  # 已实际深读完成的（study_complete）
        cur.execute("SELECT arxiv_id FROM topic_score WHERE topic=%s AND study_complete=TRUE", (TOPIC,))
        studied = {r[0] for r in cur.fetchall()}
    parts = ["<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>主题榜单 · {TOPIC_LABEL}</title>",
             "<style>body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
             "max-width:960px;margin:24px auto;padding:0 16px;line-height:1.6;color:#1a1a1a}"
             "h1{font-size:22px}h2{font-size:18px;margin-top:26px;border-bottom:2px solid #eee;padding-bottom:6px}"
             "table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #eee;"
             "padding:5px 7px;text-align:left;vertical-align:top}th{background:#fafafa}"
             ".c{font-weight:700;color:#2c5aa0}.st{color:#c0392b;font-weight:700}"
             ".arch{color:#666;font-size:12px}</style></head><body>"]
    parts.append(f"<h1>🌐 主题榜单 · {TOPIC_LABEL}</h1>")
    parts.append(f"<p>生成日 {date_str} · 3 区加权（金融→跨领域，权重/条数递减）· "
                 f"各区独立 composite 排序 · 与每日/其它主题完全隔离（topic=status）。</p>")
    for sec in sorted(SECTIONS, key=lambda s: s["order"]):
        rows = _ranked_section(conn, sec["key"])[:sec["cap"]]
        fresh_txt = "relaxed 新鲜度" if sec["fresh"] == "relaxed" else "steep 新鲜度"
        parts.append(f"<h2>分区 {sec['order']} · {sec['title']}"
                     f" <span class='arch'>（{fresh_txt} · 展示前 {sec['cap']} · 深读 {sec['study_n']}）</span></h2>")
        parts.append("<table><tr><th>#</th><th>综评</th><th>论文</th><th>架构</th>"
                     "<th>新/复/颖/域/权</th></tr>")
        for i, r in enumerate(rows, 1):
            (aid, is_seed, name, pub, jr, comp, fr, fm, rp, nv, dr, au, ana, done) = r
            comp_s = f"<span class='c'>{comp:.1f}</span>" if comp is not None else "na"
            badge = " <span class='st'>★深读</span>" if aid in studied else (
                " <span class='st'>★目标</span>" if aid in tgt else "")
            seedmark = "🌱" if is_seed else ""
            nm = (f"<b>{name}</b> · " if name else "")
            arch = ""
            if jr and jr.startswith("[arch:"):
                arch = jr[6:jr.index("]")]
            sub = (f"{fr if fr is not None else '-'}/{rp if rp is not None else '-'}/"
                   f"{nv if nv is not None else '-'}/{dr if dr is not None else '-'}/"
                   f"{'na' if ana else (au if au is not None else '-')}")
            parts.append(f"<tr><td>{i}</td><td>{comp_s}</td>"
                         f"<td>{seedmark}{nm}<a href='https://arxiv.org/abs/{aid}'>{aid}</a>{badge}<br>"
                         f"<small>{_local_title(conn, aid)}</small></td>"
                         f"<td class='arch'>{arch}</td><td>{sub}</td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    out = os.path.join(scorer.OUTPUT_DIR, f"{date_str}_status_overview.html")
    os.makedirs(scorer.OUTPUT_DIR, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return out


conn = None


def main():
    global conn
    args = sys.argv[1:]
    window_years = 2
    refresh = "--refresh-pool" in args

    def _args(name, d=""):
        return args[args.index(name) + 1] if name in args else d
    override_ids = [x.strip() for x in _args("--study-ids").split(",") if x.strip()] or None
    model_rotation = False if "--no-model-rotation" in args else (True if "--model-rotation" in args else None)
    webhook = _args("--webhook").strip()

    conn = db.get_connection()
    if "--dry-run" in args:
        dry_run(window_years, refresh)
    elif "--score" in args:
        score_stage()
    elif "--study" in args:
        study_stage(override_ids=override_ids, model_rotation=model_rotation, webhook=webhook)
    else:
        print("请指定阶段：--dry-run | --score | --study")


if __name__ == "__main__":
    main()
