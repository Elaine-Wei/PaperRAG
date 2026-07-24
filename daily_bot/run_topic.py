#!/usr/bin/env python3
"""
run_topic —— 一次性【主题综述榜单】编排（单一主题、单一群），与每日流水线(daily_*)和
因子榜单(board_*)【完全隔离】：只写 topic_paper / topic_score / topic_push（以 topic 命名空间）。
复用 run_boards 的取数、scorer 的评分/综评、deep_study+exp_theme_summary 的深读、cos_upload 的双链。

与因子榜单的差别：无 A/B 分类、无 classics/latest 分区；sol 判定只做 keep/drop（不分板块）；
单一榜单按 composite 排 Top-N；奠基之作(较老的锚点)单列「🌱 奠基之作」不参与排名。

用法：
  python daily_bot/run_topic.py --topic selfevo --dry-run     # 广召回→预筛→sol keep/drop（写 topic_paper 池，无评分/深读/推送）
  python daily_bot/run_topic.py --topic selfevo --score       # 便宜重要性预排→shortlist top-N → full-score(steep)+综评→单一排名 Top-N + 深读目标
  python daily_bot/run_topic.py --topic selfevo --study       # 深读 top-N(sol+theme)→概览→COS 双链（webhook 空=只生成）
  flags: --topic X  --dry-run|--score|--study  --top-n 10  --study-top 3  --window-years 2  --refresh-pool  --webhook URL
         --study-ids id1,id2  --model-rotation / --no-model-rotation（覆盖 STUDY_MODEL_ROTATION 常量，默认 OFF=全 sol）
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
import run_boards     # noqa: E402  复用 fetch_ids / fetch_search / _get

STUDY_MODEL = "gpt-5.6-sol"      # 深读 + theme-summary（luna 宕机）
CROSS_MODEL = "gpt-5.6-sol"      # 交叉复核走 sol
JUDGE_MODEL = "gpt-5.6-sol"      # keep/drop 判定 + 重要性预排
# 主评分：本主题改用 sol。原因：fable-5 经 relay 在大评分 prompt 上持续返回空(empty-score 守卫拦截)，
# 而 luna 宕机——sol 是唯一健康主模型。副作用：交叉复核变同族(sol 自审)，暂无异构复核（fable-5 恢复后可重评）。
SCORE_MAIN = "gpt-5.6-sol"
# 逐-paper（非逐-call）研究模型交替：把不同论文分摊到不同模型后端（sol-wide 抖动未必命中 terra）。
# 每篇研究全程单一模型（风格一致），仅在论文【之间】交替；同一篇的所有 pass 用同一模型。
# 默认【关闭】（全 sol）——terra 研究质量验证通过前保持安全态；CLI --model-rotation 可临时开启。
MODEL_ROTATION = ["gpt-5.6-sol", "gpt-5.6-terra"]
STUDY_MODEL_ROTATION = False
FOUNDATIONAL_DAYS = 365          # 锚点若早于 today-此天数 → 归入「🌱 奠基之作」不参与排名

TOPIC_LABEL = {"selfevo": "Agent 自进化（自进化 / 自我改进的 LLM 智能体）"}

# 5 个种子（已校验，标题精确匹配）
SEEDS = {
    "2508.07407": "Survey of Self-Evolving AI Agents",
    "2510.16079": "EvolveR",
    "2511.10395": "AgentEvolver",
    "2508.02085": "SE-Agent",
    "2602.04837": "Group-Evolving Agents",
}
# 7 个具名锚点，已提升为「按 ID 取 + 校验标题」（older 者进奠基之作）
ANCHORS = {
    "2508.05004": "R-Zero",
    "2505.03335": "Absolute Zero Reasoner",
    "2410.04444": "Gödel Agent",
    "2410.06153": "AgentSquare",
    "2408.08435": "ADAS",
    "2509.26354": "Your Agent May Misevolve",
    "2305.16291": "Voyager",
}
VERIFIED = {**SEEDS, **ANCHORS}
# 标题校验关键词（命中则视为匹配；不命中 → 警告并 keyword-fallback，但保留）
_EXPECT_KW = {
    "2508.07407": "self-evolving", "2510.16079": "evolver", "2511.10395": "agentevolver",
    "2508.02085": "se-agent", "2602.04837": "group-evolving", "2508.05004": "r-zero",
    "2505.03335": "absolute zero", "2410.04444": "gödel", "2410.06153": "agentsquare",
    "2408.08435": "automated design of agentic", "2509.26354": "misevolve", "2305.16291": "voyager",
}

# 9 个用户关键词网 + 3 个补充子流派网（覆盖全部 5 类）
KW = [
    "self-evolving agents", "self-evolving LLM agents", "self-improving LLM agent",
    "agent self-evolution", "lifelong agentic", "self-play LLM reasoning",
    "experience-driven agent", "recursive self-improvement agent", "multi-agent co-evolution",
    # 补充：确保覆盖 自对弈共进化 / 自改代码 / 工具自进化
    "proposer solver co-evolution", "agent self-improve own code", "tool learning self-evolving agent",
]

# 便宜预筛（替换 run_boards 的金融预筛）：cs.* 或提到 agent/self-evolv/self-play/LLM/智能体…
_TOPIC_RE = re.compile(
    r"(agent|agentic|self[- ]?evolv|self[- ]?improv|self[- ]?play|lifelong|recursive self|"
    r"co[- ]?evolution|\bLLM\b|large language model|language model|智能体|自进化|自我改进|自我进化)", re.I)


def _prefilter(meta):
    if any((c or "").startswith("cs.") for c in (meta.get("categories") or [])):
        return True
    return bool(_TOPIC_RE.search(f"{meta.get('title','')} {meta.get('abstract','')}"))


# ---------------------------------------------------------------------------
# sol 判定（keep/drop，不分板块）
# ---------------------------------------------------------------------------
_JUDGE_SYS = (
    "判断这篇论文是否属于【自进化/自我改进的 LLM 智能体】研究——即一个 LLM agent 系统通过以下任一机制"
    "自我改进：①经验/记忆驱动进化 ②自对弈/无数据自训练(proposer-solver 共进化) ③架构/工具自进化 "
    "④递归自我改进(agent 改写自身代码) ⑤多智能体共进化。"
    "属于→true；通用 agent（无自我改进闭环）、通用 RL、纯 prompt 工程、与自进化无关→false。"
    '只输出 JSON：{"is_self_evolving_agent":true或false,"reason":"一句中文理由"}')


def judge_topic(meta, model=JUDGE_MODEL):
    """sol 一次调用做 keep/drop：{is_self_evolving_agent, reason}。503 退避重试。"""
    import relay
    user = f"标题：{meta.get('title')}\n摘要：{(meta.get('abstract') or '')[:1400]}"
    for k in range(3):
        try:
            content, _ = relay.relay_chat(_JUDGE_SYS, user, temperature=0, model=model, max_tokens=200)
            obj = relay.extract_json(content)
            if obj is not None and "is_self_evolving_agent" in obj:
                return bool(obj.get("is_self_evolving_agent")), (obj.get("reason") or "").strip()
        except Exception as e:
            print(f"    [judge] {meta.get('arxiv_id')} 异常({str(e)[:35]})，退避 {k+1}/3", flush=True)
            time.sleep(20 * (k + 1))
    return False, "（判定失败）"


# ---------------------------------------------------------------------------
# 便宜 sol 「领域重要性」预排 → shortlist（省评分调用；长尾进不了 Top-10）
# ---------------------------------------------------------------------------
_IMPORTANCE_SYS = (
    "你在为【自进化/自我改进 LLM 智能体】主题综述榜单做重要性预排。根据标题+摘要，给这篇论文在该领域的"
    "【重要性/代表性】打 1-10 分：landmark/开创性方法、被广泛参照、某子流派的强代表、方法新颖且影响大→高(8-10)；"
    "扎实但常规的增量工作→中(5-7)；小众领域应用、纯基准/评测、边缘相关→低(1-4)。"
    '只输出 JSON：{"importance":1到10的整数,"reason":"一句中文"}')


def field_importance(meta, model=JUDGE_MODEL):
    """一次 sol 调用给领域重要性打分(1-10)。失败→(None, 理由)。503 退避重试。"""
    import relay
    user = f"标题：{meta.get('title')}\n摘要：{(meta.get('abstract') or '')[:1400]}"
    for k in range(3):
        try:
            content, _ = relay.relay_chat(_IMPORTANCE_SYS, user, temperature=0, model=model, max_tokens=180)
            obj = relay.extract_json(content)
            if obj is not None and "importance" in obj:
                try:
                    imp = float(obj.get("importance"))
                except (TypeError, ValueError):
                    imp = None
                if imp is not None:
                    return max(0.0, min(10.0, imp)), (obj.get("reason") or "").strip()
        except Exception as e:
            print(f"    [imp] {meta.get('arxiv_id')} 异常({str(e)[:35]})，退避 {k+1}/3", flush=True)
            time.sleep(20 * (k + 1))
    return None, "（重要性判定失败）"


def _nonseed_keeps(conn, topic):
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, importance FROM topic_paper
                       WHERE topic=%s AND judge_keep=TRUE AND is_seed=FALSE""", (topic,))
        return cur.fetchall()


def _set_importance(conn, topic, aid, imp, reason):
    with conn.cursor() as cur:
        cur.execute("""UPDATE topic_paper SET importance=%s, importance_reason=%s
                       WHERE topic=%s AND arxiv_id=%s""", (imp, reason, topic, aid))
    conn.commit()


def ensure_shortlist(conn, topic, top_n, log):
    """对 109 个非种子 keep 逐篇 sol 重要性打分(增量，跳过已打)，取 top_n 置 shortlist=TRUE。"""
    rows = _nonseed_keeps(conn, topic)
    need = [aid for aid, imp in rows if imp is None]
    if need:
        log(f"shortlist: 需重要性打分 {len(need)}/{len(rows)} 篇（增量）")
        metas = {}
        for i in range(0, len(need), 50):
            metas.update(run_boards.fetch_ids(need[i:i + 50]))
        for j, aid in enumerate(need, 1):
            m = metas.get(aid) or {}
            imp, reason = field_importance(m) if m else (None, "（取数失败）")
            _set_importance(conn, topic, aid, imp, reason)
            if j % 15 == 0 or j == len(need):
                log(f"shortlist: 重要性进度 {j}/{len(need)}")
    else:
        log(f"shortlist: 全部 {len(rows)} 篇已打过重要性（增量跳过）")
    # 取 top_n 重排 shortlist 标记
    rows = _nonseed_keeps(conn, topic)
    # 确定性 tie-break：importance 有大量并列(如一堆 8.0)，仅按分数排序会每次取到不同子集→shortlist 抖动、
    # 评分永不收敛。加 arxiv_id 作稳定次键，保证 top_n 截断在多次运行间一致。
    scored = sorted([(aid, float(imp)) for aid, imp in rows if imp is not None],
                    key=lambda x: (x[1], x[0]), reverse=True)
    keep_ids = {aid for aid, _ in scored[:top_n]}
    with conn.cursor() as cur:
        cur.execute("""UPDATE topic_paper SET shortlist=FALSE
                       WHERE topic=%s AND judge_keep=TRUE AND is_seed=FALSE""", (topic,))
        if keep_ids:
            cur.execute("""UPDATE topic_paper SET shortlist=TRUE
                           WHERE topic=%s AND arxiv_id = ANY(%s)""", (topic, list(keep_ids)))
    conn.commit()
    cutoff = scored[top_n - 1][1] if len(scored) >= top_n else (scored[-1][1] if scored else 0)
    log(f"shortlist: 打分成功 {len(scored)} 篇，取前 {min(top_n, len(scored))}（重要性≥{cutoff}）置 shortlist")
    return len(keep_ids)


def load_to_score(conn, topic):
    """需 full-score 的集合 = 全部验证锚点 + shortlist=TRUE 的候选。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name, importance FROM topic_paper
                       WHERE topic=%s AND judge_keep=TRUE AND (is_seed=TRUE OR shortlist=TRUE)""", (topic,))
        rows = cur.fetchall()
    return [{"arxiv_id": r[0], "is_seed": r[1], "seed_name": r[2], "importance": r[3]} for r in rows]


# ---------------------------------------------------------------------------
# 取数：验证锚点 + 关键词网（去重、时间窗；验证锚点不受时间窗限制）
# ---------------------------------------------------------------------------
def _is_foundational(published):
    try:
        d = datetime.date.fromisoformat((published or "")[:10])
    except Exception:
        return False
    return d < (datetime.date.today() - datetime.timedelta(days=FOUNDATIONAL_DAYS))


def retrieve_topic(window_years=2, max_per_query=50):
    """返回 (verified_meta: {id:meta}, candidates: [meta])。验证锚点按 ID 取+校验；候选走关键词网。"""
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=int(window_years * 365))
    verified_meta = run_boards.fetch_ids(list(VERIFIED))
    for aid, name in VERIFIED.items():
        m = verified_meta.get(aid)
        if not m:
            print(f"  ⚠ 锚点 {aid}（{name}）按 ID 取数失败——建议 keyword-fallback")
            continue
        kw = _EXPECT_KW.get(aid, "")
        if kw and kw.lower() not in (m.get("title") or "").lower():
            print(f"  ⚠ 锚点 {aid}（{name}）标题校验不符：{m.get('title')!r}（keyword-fallback 待定，暂保留）")

    pool = {}
    for q in KW:
        for m in run_boards.fetch_search(q, max_per_query):
            aid = m["arxiv_id"]
            if aid in VERIFIED or aid in pool:
                continue
            try:
                if datetime.date.fromisoformat((m.get("published") or "")[:10]) < cutoff:
                    continue
            except Exception:
                continue
            pool[aid] = m
    return verified_meta, list(pool.values())


# ---------------------------------------------------------------------------
# 隔离持久化：只读写 topic_*
# ---------------------------------------------------------------------------
def _persist_paper(conn, topic, aid, is_seed, seed_name, source, published, keep, reason):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO topic_paper
              (topic, arxiv_id, is_seed, seed_name, source, published, judge_keep, judge_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (topic, arxiv_id) DO UPDATE SET
              is_seed=EXCLUDED.is_seed, seed_name=EXCLUDED.seed_name, source=EXCLUDED.source,
              published=EXCLUDED.published, judge_keep=EXCLUDED.judge_keep, judge_reason=EXCLUDED.judge_reason
        """, (topic, aid, is_seed, seed_name, source, (published or None), keep, reason))
    conn.commit()


def _pool_count(conn, topic):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM topic_paper WHERE topic=%s", (topic,))
        return cur.fetchone()[0]


def ensure_pool(conn, topic, window_years=2, refresh=False):
    """判定一次并落 topic_paper：验证锚点(is_seed,keep=true,含 sanity 备注) + 候选(sol keep/drop)。
    topic_paper 已有该 topic 行且未 --refresh-pool → 跳过（增量/复用已审阅的池）。"""
    if _pool_count(conn, topic) > 0 and not refresh:
        print(f"[topic] topic_paper 已有 {_pool_count(conn, topic)} 行（topic={topic}），跳过判定"
              f"（--refresh-pool 可强制重判）。")
        return
    print(f"[topic] 首次判定候选池（topic={topic}：验证锚点 + 关键词网 → 预筛 → sol keep/drop）…")
    verified_meta, cand = retrieve_topic(window_years)

    # 验证锚点：report-and-keep（sanity 判定仅记录，不丢弃）
    for aid, name in VERIFIED.items():
        m = verified_meta.get(aid) or {}
        src = "seed" if aid in SEEDS else "anchor"
        if m:
            keep, reason = judge_topic(m)
            note = reason if keep else f"[sanity:sol判为非自进化] {reason}"
            if not keep:
                print(f"  ⚠ 锚点 {aid} {name}: sol 判为非自进化（report-and-keep）")
        else:
            note = "（锚点取数失败，保留占位）"
        _persist_paper(conn, topic, aid, True, name, src,
                       (m.get("published") or "")[:10] or None, True, note)

    pre = [m for m in cand if _prefilter(m)]
    print(f"[topic] 候选 {len(cand)} → 预筛 {len(pre)} → sol 判定中…")
    kept = 0
    for i, m in enumerate(pre, 1):
        keep, reason = judge_topic(m)
        _persist_paper(conn, topic, m["arxiv_id"], False, None, "keyword",
                       (m.get("published") or "")[:10] or None, keep, reason)
        kept += 1 if keep else 0
        if i % 20 == 0:
            print(f"    ...sol 判定进度 {i}/{len(pre)}（keep={kept}）", flush=True)
    print(f"[topic] 判定完成：候选 keep={kept}/{len(pre)}；topic_paper 共 {_pool_count(conn, topic)} 行"
          f"（含 {len(VERIFIED)} 验证锚点）。")


def load_kept(conn, topic):
    """返回该 topic 所有 judge_keep=true 的论文（验证锚点 + 命中候选）。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name, source, published, judge_reason
                       FROM topic_paper WHERE topic=%s AND judge_keep=TRUE""", (topic,))
        rows = cur.fetchall()
    return [{"arxiv_id": r[0], "is_seed": r[1], "seed_name": r[2], "source": r[3],
             "published": str(r[4]) if r[4] else None, "judge_reason": r[5]} for r in rows]


def _get_score(conn, topic, aid):
    with conn.cursor() as cur:
        cur.execute("""SELECT composite_score, freshness_score, repro_score, novelty_total,
                              domain_relevance_score, authority_score, authority_na,
                              composite_reason, study_complete, study_path, themed_path, score_path
                       FROM topic_score WHERE topic=%s AND arxiv_id=%s""", (topic, aid))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["composite_score", "freshness_score", "repro_score", "novelty_total",
            "domain_relevance_score", "authority_score", "authority_na", "composite_reason",
            "study_complete", "study_path", "themed_path", "score_path"]
    return dict(zip(keys, r))


def _persist_score(conn, topic, aid, res, composite):
    norm = res["norm"]
    auth = res.get("authority") or {}
    dom = res.get("domain_relevance") or {}
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO topic_score
              (topic, arxiv_id, freshness_score, freshness_mode, repro_score, novelty_total,
               paper_type, domain_relevance_score, authority_score, authority_na, authority_venue,
               composite_score, composite_reason, score_path, scored_at)
            VALUES (%s,%s,%s,'steep',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (topic, arxiv_id) DO UPDATE SET
              freshness_score=EXCLUDED.freshness_score, repro_score=EXCLUDED.repro_score,
              novelty_total=EXCLUDED.novelty_total, paper_type=EXCLUDED.paper_type,
              domain_relevance_score=EXCLUDED.domain_relevance_score,
              authority_score=EXCLUDED.authority_score, authority_na=EXCLUDED.authority_na,
              authority_venue=EXCLUDED.authority_venue, composite_score=EXCLUDED.composite_score,
              composite_reason=EXCLUDED.composite_reason, score_path=EXCLUDED.score_path, scored_at=NOW()
        """, (topic, aid, res["fresh"]["score"], res["repro_total"], res["novelty_total"],
              norm["paper_type"], dom.get("score"),
              (None if auth.get("na") else auth.get("score")), bool(auth.get("na", True)),
              auth.get("venue"), (None if composite["na"] else composite["score"]),
              composite["reason"] or None, res["path"]))
    conn.commit()


# ---------------------------------------------------------------------------
# DRY-RUN：池 → 预筛 → sol keep/drop（写 topic_paper 池；无评分/深读/推送）
# ---------------------------------------------------------------------------
def dry_run(topic, window_years, refresh):
    label = TOPIC_LABEL.get(topic, topic)
    print(f"\n===== DRY-RUN 主题榜单 [{topic}] {label} =====")
    print("广召回（验证锚点 + 关键词网）→ 便宜预筛 → sol keep/drop。"
          "本轮把判定结果写入隔离表 topic_paper（无评分/深读/推送）。")
    ensure_pool(conn, topic, window_years, refresh)

    with conn.cursor() as cur:
        cur.execute("""SELECT arxiv_id, is_seed, seed_name, source, published, judge_keep, judge_reason
                       FROM topic_paper WHERE topic=%s""", (topic,))
        rows = cur.fetchall()
    verified = [r for r in rows if r[1]]
    cand = [r for r in rows if not r[1]]
    kept_c = [r for r in cand if r[5]]
    drop_c = [r for r in cand if not r[5]]

    print(f"\n(a) 池规模：验证锚点 {len(verified)}；候选(预筛后进 sol) {len(cand)} → "
          f"keep {len(kept_c)}，drop {len(drop_c)}。榜单候选合计 = 锚点 {len(verified)} + keep {len(kept_c)}。")

    def _line(aid, name, pub, reason, tag):
        nm = (name + "｜") if name else ""
        found = " 🌱奠基" if _is_foundational(pub) else ""
        print(f"    {aid:12} {str(pub or '?'):11}{found:8} {tag} {nm}{run_boards._title_of(conn, aid)[:44]}")
        if reason:
            print(f"        ← {reason[:80]}")

    print(f"\n(b) 🔑 验证锚点（种子 5 + 具名锚点 7，report-and-keep；🌱=奠基之作，概览里单列不参与排名）：")
    for aid, is_seed, name, source, pub, keep, reason in sorted(verified, key=lambda r: r[4] or "", reverse=True):
        _line(aid, name, pub, reason, f"[{source}]")

    print(f"\n(c) ✅ sol 判定 KEEP 的候选 {len(kept_c)} 篇（进入榜单，真跑按 composite 排 Top-N）：")
    for aid, is_seed, name, source, pub, keep, reason in sorted(kept_c, key=lambda r: r[4] or "", reverse=True):
        _line(aid, name, pub, reason, "[kw]")

    print(f"\n(d) ❌ sol 判定 DROP 的候选 {len(drop_c)} 篇（剔除，附理由供你复核误杀）：")
    for aid, is_seed, name, source, pub, keep, reason in sorted(drop_c, key=lambda r: r[4] or "", reverse=True):
        print(f"    {aid:12} {str(pub or '?'):11} {run_boards._title_of(conn, aid)[:46]}")
        if reason:
            print(f"        ✗ {reason[:80]}")

    print(f"\n[汇总] 榜单候选池 = {len(verified) + len(kept_c)} 篇（{len(verified)} 锚点 + {len(kept_c)} keep）。"
          f"\n下一步（等你 green-light + 确认 relay 空闲）：python daily_bot/run_topic.py --topic {topic} --score")


# ---------------------------------------------------------------------------
# 阶段①：评分（steep）+ 综评 → 单一 Top-N 排名 + 奠基之作 + 深读目标
# ---------------------------------------------------------------------------
def score_one(conn, topic, aid):
    """steep freshness（scorer 默认 45/15）+ 交叉复核 sol → generate_composite → 落 topic_score。"""
    res = scorer.generate_score(aid, cross_check_on=True, model=SCORE_MAIN,
                                cross_model=CROSS_MODEL)   # 默认 center=45 scale=15 = steep
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
    _persist_score(conn2, topic, aid, res, composite)
    return composite


def score_stage(topic, top_n, study_top, window_years, refresh, shortlist_top):
    global conn
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topic_score_retry.log")

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    ensure_pool(conn, topic, window_years, refresh)
    logln(f"=== score_stage topic={topic} shortlist_top={shortlist_top} (steep, generate-only) ===")

    # ① 便宜重要性预排 → shortlist top_n
    n_short = ensure_shortlist(conn, topic, shortlist_top, logln)
    conn = db.ensure(conn)

    # ② full-score：验证锚点 + shortlist
    to_score = load_to_score(conn, topic)
    n_seed = sum(1 for d in to_score if d["is_seed"])
    logln(f"full-score 集合 {len(to_score)} 篇 = 锚点 {n_seed} + shortlist {n_short}（steep，增量）")

    failed, done = [], 0
    for idx, d in enumerate(to_score, 1):
        aid = d["arxiv_id"]
        prev = _get_score(conn, topic, aid)
        if prev and prev.get("composite_score") is not None:
            logln(f"[skip] {aid} 已评分（{idx}/{len(to_score)}）")
            continue
        tag = d.get("seed_name") or f"imp={d.get('importance')}"
        logln(f"[score {idx}/{len(to_score)}] {aid}（{tag}）…")
        for attempt in range(2):
            try:
                comp = score_one(conn, topic, aid)
                done += 1
                logln(f"  → {aid} composite={'N/A' if comp['na'] else comp['score']}  {(comp['reason'] or '')[:40]}")
                break
            except Exception as e:
                kind = run._classify_study_error(e)
                retryable = kind == "relay" or "为空/无效" in str(e)  # 空评分多为 relay 抖动，非真内容问题
                if retryable and attempt == 0:
                    logln(f"  [relay/empty] {aid} 退避 60s 重试一次…（{str(e)[:50]}）")
                    time.sleep(60)
                    conn = db.ensure(conn)
                    continue
                logln(f"  [FAIL] {aid} 评分失败，跳过（{str(e)[:60]}）")
                failed.append((aid, str(e)[:60]))
                break

    logln(f"=== score 完成：成功 {done}，失败 {len(failed)} ===")
    _print_ranking(conn, topic, top_n, study_top)
    if failed:
        print(f"\n[topic] 评分失败 {len(failed)} 篇（可重跑本阶段增量补齐）：")
        for aid, e in failed:
            print(f"  ✗ {aid}：{e}")
    print(f"\n[topic] ↑ 审阅 Top-{top_n} + 奠基之作 + 深读目标（前 {study_top}）。确认后："
          f"\n         python daily_bot/run_topic.py --topic {topic} --study")


def _ranked(conn, topic, exclude_foundational=True):
    """按 composite 降序返回 (aid, seed_name, published, comp, fresh, repro, novl, domr, auth, auth_na, done, foundational)。
    奠基之作(较老锚点)默认排除出排名（单列展示）。未评分/na 置底。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT tp.arxiv_id, tp.seed_name, tp.published, ts.composite_score,
                              ts.freshness_score, ts.repro_score, ts.novelty_total,
                              ts.domain_relevance_score, ts.authority_score, ts.authority_na,
                              ts.study_complete
                       FROM topic_paper tp LEFT JOIN topic_score ts
                         ON tp.topic=ts.topic AND tp.arxiv_id=ts.arxiv_id
                       WHERE tp.topic=%s AND tp.judge_keep=TRUE""", (topic,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        # 奠基之作仅指【较老的验证锚点】（seed_name 非空 且 早于 cutoff）；
        # 普通关键词候选即便较老也照常参与排名（steep 新鲜度自然压低，凭复现/新颖/权威竞争）。
        found = bool(r[1]) and _is_foundational(str(r[2]) if r[2] else None)
        if exclude_foundational and found:
            continue
        out.append(tuple(r) + (found,))
    out.sort(key=lambda r: (r[3] is not None, r[3] if r[3] is not None else -1), reverse=True)
    return out


def _foundational_rows(conn, topic):
    with conn.cursor() as cur:
        cur.execute("""SELECT tp.arxiv_id, tp.seed_name, tp.published, ts.composite_score
                       FROM topic_paper tp LEFT JOIN topic_score ts
                         ON tp.topic=ts.topic AND tp.arxiv_id=ts.arxiv_id
                       WHERE tp.topic=%s AND tp.judge_keep=TRUE""", (topic,))
        rows = cur.fetchall()
    # 只列【较老的验证锚点】（seed_name 非空 且 早于 cutoff）
    found = [r for r in rows if bool(r[1]) and _is_foundational(str(r[2]) if r[2] else None)]
    found.sort(key=lambda r: r[2] or "", reverse=True)
    return found


def _print_ranking(conn, topic, top_n, study_top):
    label = TOPIC_LABEL.get(topic, topic)
    ranked = _ranked(conn, topic)[:top_n]
    print(f"\n{'='*74}\n  主题榜单 [{topic}] {label} — Top {top_n}（steep freshness，按 composite 排）\n{'='*74}")
    print(f"  {'#':>2} {'comp':>4} {'fresh':>5} {'repr':>4} {'novl':>4} {'domR':>4} {'auth':>4}  {'id':12} title")
    for i, r in enumerate(ranked, 1):
        (aid, name, pub, comp, fresh, repro, novl, domr, auth, auth_na, done, found) = r
        star = " ★深读" if i <= study_top else ""
        comp_s = f"{comp:.1f}" if comp is not None else " na"
        auth_s = " na" if auth_na else (f"{auth:.1f}" if auth is not None else " -")
        nm = (name + "｜") if name else ""
        print(f"  {i:>2} {comp_s:>4} {str(fresh) if fresh is not None else '-':>5} "
              f"{str(repro) if repro is not None else '-':>4} {str(novl) if novl is not None else '-':>4} "
              f"{str(domr) if domr is not None else '-':>4} {auth_s:>4}  {aid:12} "
              f"{nm}{run_boards._title_of(conn, aid)[:40]}{star}")

    found = _foundational_rows(conn, topic)
    print(f"\n🌱 奠基之作（{len(found)} 篇，不参与 Top-{top_n} 排名，概览单列）：")
    for aid, name, pub, comp in found:
        comp_s = f"comp={comp:.1f}" if comp is not None else "comp=na"
        print(f"    {aid:12} {str(pub or '?'):11} {comp_s:9} {name}｜{run_boards._title_of(conn, aid)[:44]}")


# ---------------------------------------------------------------------------
# 阶段②：深读（sol+theme）→ 单区概览 → COS 双链（webhook 空=只生成）
# ---------------------------------------------------------------------------
def _study_targets(conn, topic, study_top):
    ranked = [r for r in _ranked(conn, topic) if r[3] is not None]
    return [r[0] for r in ranked[:study_top]]


def study_stage(topic, study_top, top_n, webhook, override_ids=None, model_rotation=None):
    import exp_theme_summary
    import assemble
    import cos_upload
    global conn
    if override_ids:
        targets = list(override_ids)
        print(f"[topic] {topic} 深读目标（人工指定，覆盖 composite 前 {study_top}）{len(targets)} 篇：{targets}")
    else:
        targets = _study_targets(conn, topic, study_top)
        print(f"[topic] {topic} 深读目标（composite 前 {study_top}）{len(targets)} 篇：{targets}")
    if not targets:
        print("[topic] 无深读目标（override 为空或无已评分目标，请先运行 --score）。")
        return

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topic_study_retry.log")
    BACKOFF = [10, 15, 20, 25, 30]
    start = time.time()
    CAP = 5.0 * 3600
    pending = list(targets)
    consec_relay, content_fails = 0, {a: 0 for a in targets}
    completed = []

    def logln(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # 逐-paper 模型：按【原始 targets 下标】定一次，重试各轮沿用同一模型（稳定、逐-paper 而非逐-call）
    rotate_on = STUDY_MODEL_ROTATION if model_rotation is None else model_rotation

    def _paper_model(i):
        return MODEL_ROTATION[i % len(MODEL_ROTATION)] if rotate_on else STUDY_MODEL
    paper_model = {aid: _paper_model(i) for i, aid in enumerate(targets)}
    logln(f"模型计划（rotation={'ON' if rotate_on else 'OFF'}）："
          + "，".join(f"{a}->{m}" for a, m in paper_model.items()))

    for aid in list(pending):
        sc = _get_score(conn, topic, aid) or {}
        sp = sc.get("study_path")
        if sc.get("study_complete") and sp and os.path.exists(sp) and run._count_big_sections(sp) >= 4:
            completed.append(aid)
            pending.remove(aid)
            logln(f"paper={aid} outcome=already-complete")

    rnd = 0
    while pending and (CAP - (time.time() - start)) > 0:
        rnd += 1
        for aid in list(pending):
            if (CAP - (time.time() - start)) <= 0:
                break
            model = paper_model.get(aid, STUDY_MODEL)   # 该论文全程单一模型（含 theme-summary）
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
                        logln(f"paper={aid} theme-summary 失败（{str(e)[:50]}），仅用 per-section 深读")
                    sc = _get_score(conn, topic, aid) or {}
                    try:
                        assemble.prepend_score_card_to_study(themed or path, sc.get("score_path"))
                    except Exception as e:
                        logln(f"paper={aid} 评分卡前置失败（{str(e)[:40]}）")
                    conn = db.ensure(conn)
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE topic_score SET study_path=%s, themed_path=%s,
                                       study_complete=TRUE WHERE topic=%s AND arxiv_id=%s""",
                                    (path, themed, topic, aid))
                    conn.commit()
                    completed.append(aid)
                    pending.remove(aid)
                    consec_relay = 0
                    logln(f"round={rnd} paper={aid} outcome=success nbig={nbig}")
                else:
                    content_fails[aid] += 1
                    why = "begging" if begging else f"nbig={nbig}<4"
                    if content_fails[aid] >= 3:
                        pending.remove(aid)
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
                        pending.remove(aid)
                    logln(f"round={rnd} paper={aid} outcome=content-error attempt={content_fails[aid]} ({str(e)[:50]})")

    logln(f"FINISHED topic={topic} completed={completed} pending={pending}")

    conn = db.ensure(conn)   # 深读可能跑数小时，构建概览前重连（Supabase pooler 会关空闲连接）
    overview = _build_overview(conn, topic, top_n)
    date_str = datetime.date.today().isoformat()
    links = []
    try:
        ov_pv, ov_dl = cos_upload.upload_and_links(overview, f"{date_str}_topic_{topic}_overview.html")
        links.append(("概览", ov_pv, ov_dl))
    except Exception as e:
        print(f"[topic] 概览 COS 上传失败：{e}")
    for aid in completed:
        sc = _get_score(conn, topic, aid) or {}
        sp = sc.get("themed_path") or sc.get("study_path")
        if sp and os.path.exists(sp):
            try:
                pv, dl = cos_upload.upload_and_links(sp, f"{date_str}_topic_{topic}_{aid.replace('/','_')}_study.html")
                links.append((aid, pv, dl))
            except Exception as e:
                print(f"[topic] {aid} COS 上传失败：{e}")

    print(f"\n[topic] {topic} 生成完成。概览：{overview}")
    print(f"[topic] COS 链接（{len(links)} 项，预览 inline / 下载 attachment）：")
    for name, pv, dl in links:
        print(f"  {name}: 预览 {pv}\n         下载 {dl}")
    if webhook:
        print(f"[topic] （webhook 已提供，但首轮仍只生成不自动推送——人工确认后推送。）")
    else:
        print(f"[topic] webhook 为空 → 只生成、不推送。请人工审阅后手动推送到量化群。")
    return {"topic": topic, "completed": completed, "overview": overview, "links": links}


_TITLE_LOCAL = {}


def _local_title(conn, topic, aid):
    """标题优先从本地评分 HTML(<h1>) 取——离线、且避免 arXiv 限流；仅在缺失时才回退 arXiv。"""
    if aid in _TITLE_LOCAL:
        return _TITLE_LOCAL[aid]
    t = ""
    with conn.cursor() as cur:
        cur.execute("SELECT score_path FROM topic_score WHERE topic=%s AND arxiv_id=%s", (topic, aid))
        r = cur.fetchone()
    if r and r[0] and os.path.exists(r[0]):
        try:
            html = open(r[0], encoding="utf-8").read()
            m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
            if m:
                t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        except Exception:
            pass
    if not t:
        try:
            t = run_boards._title_of(conn, aid)   # 最后才碰 arXiv（可能限流）
        except Exception:
            t = ""
    _TITLE_LOCAL[aid] = t
    return t


def _build_overview(conn, topic, top_n):
    """自包含单区概览（隔离）：Top-N composite 排名 + 🌱 奠基之作单列。"""
    conn = db.ensure(conn)   # 防御：长跑后连接可能已断
    label = TOPIC_LABEL.get(topic, topic)
    date_str = datetime.date.today().isoformat()
    ranked = _ranked(conn, topic)[:top_n]
    found = _foundational_rows(conn, topic)
    parts = ["<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>主题榜单 · {label}</title>",
             "<style>body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
             "max-width:920px;margin:24px auto;padding:0 16px;line-height:1.6;color:#1a1a1a}"
             "h1{font-size:22px}h2{font-size:18px;margin-top:28px;border-bottom:2px solid #eee;padding-bottom:6px}"
             "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid #eee;"
             "padding:6px 8px;text-align:left;vertical-align:top}th{background:#fafafa}"
             ".c{font-weight:700;color:#2c7a3f}.st{color:#c0392b;font-weight:700}"
             ".f{background:#f6faf4}</style></head><body>"]
    parts.append(f"<h1>🌐 主题榜单 · {label}</h1>")
    parts.append(f"<p>生成日 {date_str} · 全场综述（覆盖经验/自对弈/架构/递归自改/多智能体共进化 5 类）· "
                 f"与每日流水线、因子榜单完全隔离（topic_* 表）。</p>")
    parts.append(f"<h2>🏆 Top {top_n}（steep 新鲜度，按 composite 排）</h2><table>"
                 "<tr><th>#</th><th>综评</th><th>论文</th><th>新鲜/复现/新颖/领域/权威</th></tr>")
    for i, r in enumerate(ranked, 1):
        (aid, name, pub, comp, fresh, repro, novl, domr, auth, auth_na, done, foundational) = r
        comp_s = f"<span class='c'>{comp:.1f}</span>" if comp is not None else "na"
        st = " <span class='st'>★深读</span>" if done else ""
        nm = (f"<b>{name}</b> · " if name else "")
        sub = (f"{fresh if fresh is not None else '-'} / {repro if repro is not None else '-'} / "
               f"{novl if novl is not None else '-'} / {domr if domr is not None else '-'} / "
               f"{'na' if auth_na else (auth if auth is not None else '-')}")
        parts.append(f"<tr><td>{i}</td><td>{comp_s}</td>"
                     f"<td>{nm}<a href='https://arxiv.org/abs/{aid}'>{aid}</a>{st}<br>"
                     f"<small>{_local_title(conn, topic, aid)}</small></td><td>{sub}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>🌱 奠基之作（不参与排名的领域奠基/参照锚点）</h2><table class='f'>"
                 "<tr><th>论文</th><th>年份</th></tr>")
    for aid, name, pub, comp in found:
        parts.append(f"<tr><td><b>{name}</b> · <a href='https://arxiv.org/abs/{aid}'>{aid}</a><br>"
                     f"<small>{_local_title(conn, topic, aid)}</small></td><td>{str(pub)[:7]}</td></tr>")
    parts.append("</table></body></html>")

    out = os.path.join(scorer.OUTPUT_DIR, f"{date_str}_topic_{topic}_overview.html")
    os.makedirs(scorer.OUTPUT_DIR, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return out


conn = None


def main():
    global conn
    args = sys.argv[1:]
    if "--topic" not in args:
        print("用法: python daily_bot/run_topic.py --topic X [--dry-run|--score|--study] "
              "[--top-n 10] [--study-top 3] [--window-years 2] [--refresh-pool] [--webhook URL]")
        sys.exit(1)
    topic = args[args.index("--topic") + 1].strip()

    def _argi(name, d):
        return int(args[args.index(name) + 1]) if name in args else d
    def _args(name, d):
        return args[args.index(name) + 1] if name in args else d
    top_n = _argi("--top-n", 10)
    study_top = _argi("--study-top", 3)
    window_years = _argi("--window-years", 2)
    shortlist_top = _argi("--shortlist-top", 30)
    refresh = "--refresh-pool" in args
    webhook = _args("--webhook", "").strip()
    override_ids = [x.strip() for x in _args("--study-ids", "").split(",") if x.strip()] or None
    # 模型轮换开关：显式 CLI 覆盖模块常量 STUDY_MODEL_ROTATION；都没给则用常量
    model_rotation = None
    if "--no-model-rotation" in args:
        model_rotation = False
    elif "--model-rotation" in args:
        model_rotation = True

    conn = db.get_connection()
    if "--dry-run" in args:
        dry_run(topic, window_years, refresh)
    elif "--score" in args:
        score_stage(topic, top_n, study_top, window_years, refresh, shortlist_top)
    elif "--study" in args:
        study_stage(topic, study_top, top_n, webhook, override_ids, model_rotation)
    else:
        print("请指定阶段：--dry-run（池+判定）| --score（评分+排名）| --study（深读+概览+COS）")


if __name__ == "__main__":
    main()
