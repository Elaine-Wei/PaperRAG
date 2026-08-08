#!/usr/bin/env python3
"""
run_sttf —— 一次性【主题榜单】：Spatio-Temporal Transformer Feature Fusion（时空 Transformer 特征融合），
4 个【加权、有序】分区（像因子板 A/B，但 4 区），按对量化的相关度排序：金融最前、跨领域最后，权重/条数递减。
与每日流水线(daily_*)、其它主题(selfevo)【完全隔离】：只写 topic_* 表，topic='sttf' + section 列。

分区（权重=条数上限 cap + 展示顺序，非分数混合；每区独立 sol-judge keep/drop、独立 composite 排序）：
  1 金融/量化 ★core  steep  cap12  study2   —— CRISP/MaGNet/ACT + 5 关键词网
  2 通用时序骨干      relaxed cap8  study1   —— TFT/Autoformer/iTransformer/PatchTST/TimesNet/TimeMixer（经典，宽 freshness）
  3 综述             steep  cap3   study1
  4 跨领域借鉴        steep  cap3   study0（只列不深读，门槛最高、条数最少）
研究分配：共 4 篇 = 金融 2 + 骨干 1 + 综述 1；跨领域不深读。逐-paper sol↔terra 轮换（本次开启，做活测）。

用法（分阶段，与因子板/selfevo 一致）：
  python daily_bot/run_sttf.py --dry-run     # 逐区 seeds+关键词 → 预筛 → sol keep/drop（写池，无评分/深读/推送）
  python daily_bot/run_sttf.py --score       # 逐区 freshness + composite 排序 + cap（写分数）
  python daily_bot/run_sttf.py --study        # 深读 2+1+1（sol↔terra）→ 4 区有序概览 → COS 双链（generate-only）
  flags: --refresh-pool  --no-model-rotation  --webhook URL
"""

import datetime
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run            # noqa: E402  加载 .env + parse_arxiv_xml + _classify_study_error/_count_big_sections/_has_begging
import db             # noqa: E402
import scorer         # noqa: E402
import deep_study     # noqa: E402
import run_boards     # noqa: E402  复用 fetch_ids / fetch_search / _title_of

TOPIC = "sttf"
TOPIC_LABEL = "Spatio-Temporal Transformer Feature Fusion（时空 Transformer 特征融合）"
STUDY_MODEL = "gpt-5.6-sol"
CROSS_MODEL = "gpt-5.6-sol"
JUDGE_MODEL = "gpt-5.6-sol"
SCORE_MAIN = "gpt-5.6-sol"
MODEL_ROTATION = ["gpt-5.6-sol", "gpt-5.6-terra"]
STUDY_MODEL_ROTATION = True       # 本主题【开启】逐-paper sol↔terra 轮换（活测；--no-model-rotation 可关）
STEEP = {"fresh_center": 45, "fresh_scale": 15}
RELAXED = {"fresh_center": 365, "fresh_scale": 600}

# --------- 逐区 sol-judge 系统提示（keep/drop；金融区额外注明架构） ---------
_J_FIN = (
    "判断这篇论文是否属于【金融/量化：多资产/股票的时空特征融合预测】——即用时空建模对多支股票/资产做"
    "趋势/收益/协方差/组合预测，融合是关键（主干 Transformer / GNN 图 / Mamba / 混合 均可接受）。\n"
    "属于→keep=true；单资产纯时序（无跨资产/时空融合）、与金融无关、纯理论无预测→false。\n"
    "keep=true 时在 arch 注明主干架构（如 Transformer / GNN图 / Mamba / 混合），供人工区分真-Transformer vs 图/Mamba。\n"
    '只输出 JSON：{"keep":true或false,"arch":"架构或空串","reason":"一句中文，点明融合方式与架构"}')
_J_BACKBONE = (
    "判断这篇是否属于【通用时序预测骨干模型】——面向通用长时序预测的 Transformer/骨干方法（不绑定特定落地领域）。\n"
    "属于→true；绑定具体领域的落地应用（金融/交通/气象等）、与时序预测无关→false。\n"
    '只输出 JSON：{"keep":true或false,"reason":"一句中文"}')
_J_SURVEY = (
    "判断这篇是否为【时空预测 / 时序预测 的综述或框架性梳理】（Transformer / 基础模型 方向）。\n"
    "是 survey/review/综述/taxonomy/系统性梳理→true；具体方法论文、基准、非综述→false。\n"
    '只输出 JSON：{"keep":true或false,"reason":"一句中文"}')
_J_CROSS = (
    "判断这篇是否为【跨领域（交通 / 空气质量气象 / 遥感 / 视频动作识别）的时空特征融合 代表作】，"
    "用于给量化研究做跨界借鉴。门槛要高：须是该领域时空融合中有代表性/方法启发性的工作→true；"
    "普通增量、与时空融合无关→false。\n"
    '只输出 JSON：{"keep":true或false,"reason":"一句中文，点明领域与可借鉴点"}')

SECTIONS = [
    {"key": "finance", "title": "金融/量化 ★core", "order": 1, "fresh": "steep",
     "cap": 12, "study_n": 2, "judge": _J_FIN,
     "seeds": {"2510.20868": "CRISP", "2511.00085": "MaGNet", "2604.20204": "ACT"},
     "keywords": ["spatio-temporal transformer stock", "multi-asset forecasting transformer",
                  "hypergraph stock prediction", "portfolio spatio-temporal learning",
                  "stock relation graph transformer"]},
    {"key": "backbone", "title": "通用时序骨干（经典）", "order": 2, "fresh": "relaxed",
     "cap": 8, "study_n": 1, "judge": _J_BACKBONE,
     "seeds": {"1912.09363": "TFT", "2106.13008": "Autoformer", "2310.06625": "iTransformer",
               "2211.14730": "PatchTST", "2210.02186": "TimesNet", "2405.14616": "TimeMixer"},
     "keywords": ["long-term time series forecasting transformer"]},
    {"key": "survey", "title": "综述", "order": 3, "fresh": "steep",
     "cap": 3, "study_n": 1, "judge": _J_SURVEY,
     "seeds": {},
     "keywords": ["spatio-temporal prediction survey transformer"]},
    {"key": "crossdomain", "title": "跨领域借鉴", "order": 4, "fresh": "steep",
     "cap": 3, "study_n": 0, "judge": _J_CROSS,
     "seeds": {},
     "keywords": ["spatio-temporal fusion transformer traffic", "air quality spatio-temporal transformer",
                  "remote sensing spatiotemporal fusion", "two-stream transformer action recognition"]},
]
SEC_BY_KEY = {s["key"]: s for s in SECTIONS}

# 种子标题校验关键词（不含则告警，keyword-fallback；仍保留）
_EXPECT_KW = {
    "2510.20868": "crisis-resilient", "2511.00085": "magnet", "2604.20204": "anti-crosstalk",
    "1912.09363": "temporal fusion transformer", "2106.13008": "autoformer",
    "2310.06625": "itransformer", "2211.14730": "64 words", "2210.02186": "timesnet",
    "2405.14616": "timemixer",
}

# 便宜预筛：省 LLM 调用，扔掉明显无关（物理/纯数学等）。命中学科分类或时空/时序/融合词即放行。
_PRE_RE = re.compile(
    r"(spatio[- ]?temporal|spatiotemporal|time[- ]?series|forecast|transformer|fusion|"
    r"stock|portfolio|multi[- ]?asset|时空|时序|融合|股票)", re.I)
_PRE_CATS = ("q-fin", "cs.", "eess", "stat.")


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
# 取数（逐区 seeds + 关键词网；跨区按 order 优先去重：finance 赢 crossdomain）
# ---------------------------------------------------------------------------
_TITLE = {}   # aid -> title（取数时缓存，dry-run 免二次 arXiv）


def _cache_title(m):
    if m and m.get("arxiv_id"):
        _TITLE[m["arxiv_id"]] = m.get("title") or ""


def _title_of(conn, aid):
    if aid in _TITLE:
        return _TITLE[aid]
    return run_boards._title_of(conn, aid)


def retrieve_section(sec, window_years, seen):
    """返回 (seed_meta:{id:meta}, cand:[meta])。seeds 按 ID 取+校验；候选走关键词网。
    seen: 跨区已占用的 aid 集合（按 order 优先，本区跳过已被更高优先区占用者）。"""
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=int(window_years * 365))
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
        for m in run_boards.fetch_search(q, 50):
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
# 隔离持久化：topic_paper（topic='sttf' + section）
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
    已有 sttf 行且未 --refresh-pool → 跳过（复用已审阅池）。跨区按 order 优先去重。"""
    if _pool_count(conn) > 0 and not refresh:
        print(f"[sttf] topic_paper 已有 {_pool_count(conn)} 行，跳过判定（--refresh-pool 强制重判）。")
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
        print(f"\n[sttf] === 分区 {sec['order']} {sec['title']} 判定 ===")
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
    print(f"\n[sttf] 判定完成：topic_paper 共 {_pool_count(conn)} 行。")


# ---------------------------------------------------------------------------
# DRY-RUN：逐区 keep/drop 池（无评分/深读/推送）
# ---------------------------------------------------------------------------
def _rows_for_section(conn, key):
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name, published, judge_keep, judge_reason
                       FROM topic_paper WHERE topic=%s AND section=%s""", (TOPIC, key))
        return cur.fetchall()


def dry_run(window_years, refresh):
    print(f"\n===== DRY-RUN 主题榜单 [sttf] {TOPIC_LABEL} =====")
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
    print(f"\n[汇总] 4 区 keep 合计（含种子）= {total_keep} 篇。"
          f"\n下一步（等你 green-light）：python daily_bot/run_sttf.py --score")


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


def _persist_score(conn, aid, res, composite, mode):
    norm = res["norm"]
    auth = res.get("authority") or {}
    dom = res.get("domain_relevance") or {}
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO topic_score
              (topic, arxiv_id, freshness_score, freshness_mode, repro_score, novelty_total,
               paper_type, domain_relevance_score, authority_score, authority_na, authority_venue,
               composite_score, composite_reason, score_path, scored_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (topic, arxiv_id) DO UPDATE SET
              freshness_score=EXCLUDED.freshness_score, freshness_mode=EXCLUDED.freshness_mode,
              repro_score=EXCLUDED.repro_score, novelty_total=EXCLUDED.novelty_total,
              paper_type=EXCLUDED.paper_type, domain_relevance_score=EXCLUDED.domain_relevance_score,
              authority_score=EXCLUDED.authority_score, authority_na=EXCLUDED.authority_na,
              authority_venue=EXCLUDED.authority_venue, composite_score=EXCLUDED.composite_score,
              composite_reason=EXCLUDED.composite_reason, score_path=EXCLUDED.score_path, scored_at=NOW()
        """, (TOPIC, aid, res["fresh"]["score"], mode, res["repro_total"], res["novelty_total"],
              norm["paper_type"], dom.get("score"),
              (None if auth.get("na") else auth.get("score")), bool(auth.get("na", True)),
              auth.get("venue"), (None if composite["na"] else composite["score"]),
              composite["reason"] or None, res["path"]))
    conn.commit()


def score_one(conn, aid, sec):
    """按区 freshness（steep/relaxed）+ 交叉复核 sol → generate_score → generate_composite → 落 topic_score。
    空签名/异常 → 抛出交由上游跳过（绝不伪造 0）。"""
    fc = STEEP if sec["fresh"] == "steep" else RELAXED
    res = scorer.generate_score(aid, cross_check_on=True, model=SCORE_MAIN,
                                fresh_center=fc["fresh_center"], fresh_scale=fc["fresh_scale"],
                                cross_model=CROSS_MODEL)
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
    conn2 = db.ensure(conn)
    _persist_score(conn2, aid, res, composite, sec["fresh"])
    return composite


def _kept_in_section(conn, key):
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name FROM topic_paper
                       WHERE topic=%s AND section=%s AND judge_keep=TRUE""", (TOPIC, key))
        return cur.fetchall()


def score_stage():
    global conn
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sttf_score.log")

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    if _pool_count(conn) == 0:
        print("[sttf] 池为空，请先 --dry-run。"); return
    logln("=== score_stage sttf（逐区 freshness，generate-only）===")
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
        print(f"\n[sttf] 评分失败 {len(failed)} 篇（可重跑 --score 增量补齐）：")
        for k, aid, e in failed:
            print(f"  ✗ [{k}] {aid}：{e}")
    print(f"\n[sttf] ↑ 审阅 4 区排名 + 深读目标。确认后：python daily_bot/run_sttf.py --study")


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
    """深读目标：finance 前 2 + backbone 前 1 + survey 指定 2506.12809 + crossdomain 0。
    survey 若无 2506.12809 评分则回退该区 composite 第一。"""
    targets = []
    fin = [r for r in _ranked_section(conn, "finance") if r[5] is not None][:2]
    targets += [(r[0], "finance") for r in fin]
    bb = [r for r in _ranked_section(conn, "backbone") if r[5] is not None][:1]
    targets += [(r[0], "backbone") for r in bb]
    sv = [r for r in _ranked_section(conn, "survey") if r[5] is not None]
    pick = next((r for r in sv if r[0] == "2506.12809"), (sv[0] if sv else None))
    if pick:
        targets.append((pick[0], "survey"))
    return targets


def _print_rankings(conn):
    print(f"\n{'#'*74}\n  STTF 4 区排名（各区独立 composite；权重=cap+顺序，非分数混合）\n{'#'*74}")
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
    print(f"\n深读分配（真跑 --study）：finance 2 + backbone 1 + survey 1（指定 2506.12809）+ crossdomain 0 = 4 篇；"
          f"逐-paper sol↔terra 轮换（本主题开启）。")


# ---------------------------------------------------------------------------
# 阶段②：深读（2+1+1，sol↔terra 逐-paper 轮换）→ 4 区有序概览 → COS 双链（generate-only）
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
        print("[sttf] 无深读目标（请先 --score）。"); return
    rotate_on = STUDY_MODEL_ROTATION if model_rotation is None else model_rotation

    def _pm(i):
        return MODEL_ROTATION[i % len(MODEL_ROTATION)] if rotate_on else STUDY_MODEL
    paper_model = {aid: _pm(i) for i, (aid, _s) in enumerate(targets)}
    print(f"[sttf] 深读目标 {len(targets)} 篇（rotation={'ON' if rotate_on else 'OFF'}）："
          + "，".join(f"{a}({s})->{paper_model[a]}" for a, s in targets))

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sttf_study.log")
    BACKOFF = [10, 15, 20, 25, 30]
    start = time.time()
    CAP = 5.0 * 3600
    pending = list(targets)
    consec_relay, content_fails = 0, {a: 0 for a, _ in targets}
    completed = []

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

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
                        cur.execute("""UPDATE topic_score SET study_path=%s, themed_path=%s,
                                       study_complete=TRUE WHERE topic=%s AND arxiv_id=%s""",
                                    (path, themed, TOPIC, aid))
                    conn.commit()
                    completed.append((aid, sec)); pending.remove((aid, sec)); consec_relay = 0
                    logln(f"round={rnd} paper={aid} model={model} outcome=success nbig={nbig}")
                else:
                    content_fails[aid] += 1
                    why = "begging" if begging else f"nbig={nbig}<4"
                    if content_fails[aid] >= 3:
                        pending.remove((aid, sec))
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
                        pending.remove((aid, sec))
                    logln(f"round={rnd} paper={aid} outcome=content-error attempt={content_fails[aid]} ({str(e)[:50]})")

    logln(f"FINISHED completed={[a for a,_ in completed]} pending={[a for a,_ in pending]}")

    conn = db.ensure(conn)
    overview = _build_overview(conn)
    date_str = datetime.date.today().isoformat()
    links = []
    try:
        pv, dl = cos_upload.upload_and_links(overview, f"{date_str}_sttf_overview.html")
        links.append(("概览", pv, dl))
    except Exception as e:
        print(f"[sttf] 概览 COS 上传失败：{e}")
    for aid, sec in completed:
        sc = _get_score(conn, aid) or {}
        sp = sc.get("themed_path") or sc.get("study_path")
        if sp and os.path.exists(sp):
            try:
                pv, dl = cos_upload.upload_and_links(sp, f"{date_str}_sttf_{aid.replace('/','_')}_study.html")
                links.append((aid, pv, dl))
            except Exception as e:
                print(f"[sttf] {aid} COS 上传失败：{e}")

    print(f"\n[sttf] 生成完成。概览：{overview}")
    print(f"[sttf] COS 链接（{len(links)} 项，预览 inline / 下载 attachment）：")
    for name, pv, dl in links:
        print(f"  {name}: 预览 {pv}\n         下载 {dl}")
    print("[sttf] webhook 为空 → 只生成、不推送。" if not webhook
          else "[sttf] （首轮仍只生成不自动推送——人工确认后推送。）")
    return {"completed": [a for a, _ in completed], "overview": overview, "links": links}


def _build_overview(conn):
    """自包含 4 区有序概览（隔离）：每区 composite 排名 + cap + ★深读徽标；架构注记。"""
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
    parts.append(f"<p>生成日 {date_str} · 4 区加权（金融→跨领域，权重/条数递减）· "
                 f"各区独立 composite 排序 · 与每日/其它主题完全隔离（topic=sttf）。</p>")
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
    out = os.path.join(scorer.OUTPUT_DIR, f"{date_str}_sttf_overview.html")
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
