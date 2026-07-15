"""
stage_b（乙）—— 用 LLM 读摘要做真正的相关性判断 + 方向分类。

甲（paper_filter）是宽松的关键词粗召回网，会带进噪声（例如仅因出现 "factor" 就把
一篇 LLM 论文标成 quant）。stage-B 把每篇的 title+abstract 交给 relay LLM
（claude-fable-5，复用 relay.relay_chat），让它判断论文究竟属于哪个方向、还是 not_relevant，
并给出一句基于摘要的理由（便于人工核对，不编造）。

- 多篇论文批量塞进一次调用；按 arxiv_id 回填结果（与顺序无关，稳健）。
- 解析稳健：JSON 坏了 / 漏了某篇 → 不崩，记为 uncertain（在报告里列出）。
- 任一批次失败只影响该批；若整体一篇都没判成功，交由上层回退到甲。
"""

import relay

# stage-B 使用的方向定义（与 paper_filter.FOCUS_AREAS 的键一致）
AREA_DEFS = {
    "quant": "量化交易/金融：因子(factor)/alpha、组合优化、做市、回测、统计套利、资产定价、"
             "执行、波动率、衍生品定价等，或用 AI/ML 做上述金融任务。",
    "ai4math": "AI for math：定理证明、形式化数学(Lean/Coq)、自动形式化、数学推理、"
               "符号推理、竞赛/奥数题求解。注意：普通 ML 优化算法不算。",
    "lob": "限价订单簿 / 市场微观结构：limit order book、order book、撮合引擎、订单流、"
           "bid-ask、做市微观结构。",
    "hpc": "高性能/低延迟/分布式系统（面向计算或交易的系统性能）：low-latency、high-frequency "
           "交易系统、distributed/parallel 计算、GPU kernel、调度、推理服务性能。"
           "注意：仅仅用到 GPU 训练模型不算，重点是系统/性能本身。",
    "agent": "LLM agent / 多智能体 / 工具使用 / RAG / 规划：以 LLM 智能体、multi-agent、"
             "tool use、function calling、agentic workflow、检索增强为核心的论文。",
}
VALID_AREAS = list(AREA_DEFS)  # 也是最终选择的方向顺序来源

BATCH_SIZE = 8  # 每次 LLM 调用塞多少篇


def _system_prompt():
    defs = "\n".join(f'- {a}: {d}' for a, d in AREA_DEFS.items())
    return (
        "你在为一个量化研究团队筛选 arXiv 论文。对每篇论文，只依据其摘要，判断它主要属于"
        "以下哪个方向；若都不属于，判为 not_relevant。\n"
        f"方向定义：\n{defs}\n"
        "规则：\n"
        "1. 只依据摘要判断，不要臆测摘要之外的内容。\n"
        "2. 每篇给出一句话理由(reason)，必须落在摘要能支持的事实上，不要编造结果或数字。\n"
        "3. 一篇最多归一个最主要的方向；确实都不沾边就 not_relevant。\n"
        '4. area 只能取：quant / ai4math / lob / hpc / agent / not_relevant。\n'
        '只输出 JSON，形如：'
        '{"verdicts":[{"id":"<arxiv_id>","area":"quant","reason":"..."}]}，不要额外文字。'
    )


def _user_prompt(batch):
    parts = ["请判断下列论文（共 %d 篇）：" % len(batch)]
    for p in batch:
        abstract = (p.get("abstract") or "")[:1500]
        parts.append(
            f'--- id: {p["arxiv_id"]}\n'
            f'标题: {p.get("title") or ""}\n'
            f'分类: {", ".join(p.get("categories") or [])}\n'
            f'摘要: {abstract}'
        )
    return "\n".join(parts)


def classify(candidates, batch_size=BATCH_SIZE):
    """
    对候选池做 stage-B 分类。就地给每篇写入 stage_b_area / stage_b_reason（成功时）。
    返回 {"verdicts": {id:(area,reason)}, "failed": [id...], "usages": [usage...]}。
    failed = 没能拿到有效判定的论文（uncertain）。
    """
    verdicts = {}
    failed = []
    usages = []

    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        try:
            content, usage = relay.relay_chat(_system_prompt(), _user_prompt(batch),
                                               temperature=0.0)
            if usage:
                usages.append(usage)
            parsed = relay.extract_json(content)
            if isinstance(parsed, dict):
                items = parsed.get("verdicts")
            elif isinstance(parsed, list):
                items = parsed
            else:
                items = None
            if not isinstance(items, list):
                raise ValueError("模型输出没有 verdicts 数组")

            by_id = {}
            for it in items:
                if isinstance(it, dict) and it.get("id") is not None:
                    by_id[str(it["id"]).strip()] = it

            for p in batch:
                it = by_id.get(p["arxiv_id"])
                area = (it.get("area") if it else "") or ""
                area = area.strip()
                reason = ((it.get("reason") if it else "") or "").strip()
                if not it or (area not in VALID_AREAS and area != "not_relevant"):
                    failed.append(p["arxiv_id"])       # uncertain：漏判或非法 area
                    continue
                p["stage_b_area"] = area               # 可能是某方向，或 "not_relevant"
                p["stage_b_reason"] = reason
                verdicts[p["arxiv_id"]] = (area, reason)

        except Exception as e:
            print(f"[WARN] stage-B 批次失败（papers {i}..{i + len(batch) - 1}）：{e}")
            for p in batch:
                failed.append(p["arxiv_id"])

    return {"verdicts": verdicts, "failed": failed, "usages": usages}


def format_report(candidates, result):
    """逐篇打印 stage-B 判定（方向/not_relevant/uncertain + 理由），供人工核对准确性。"""
    failed = set(result["failed"])
    lines = ["== stage-B 判定报告 / Stage-B verdicts =="]
    n_area = {}
    n_notrel = 0
    for p in candidates:
        aid = p["arxiv_id"]
        title = (p.get("title") or "")[:66]
        if aid in failed:
            lines.append(f"  [uncertain]     {aid} — {title}  (未拿到有效判定)")
            continue
        area = p.get("stage_b_area", "?")
        reason = p.get("stage_b_reason", "")
        if area == "not_relevant":
            n_notrel += 1
        else:
            n_area[area] = n_area.get(area, 0) + 1
        lines.append(f"  [{area:<13}] {aid} — {title}")
        if reason:
            lines.append(f"                  理由: {reason}")
    summary = ", ".join(f"{a}:{n_area[a]}" for a in sorted(n_area)) or "无"
    lines.append(f"小结：相关 {sum(n_area.values())} 篇（{summary}）；"
                 f"not_relevant {n_notrel} 篇；uncertain {len(failed)} 篇。")
    if result["usages"]:
        tot = sum((u or {}).get("total_tokens", 0) for u in result["usages"])
        lines.append(f"stage-B 调用 {len(result['usages'])} 次，合计 total_tokens≈{tot}。")
    return "\n".join(lines)
