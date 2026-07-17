#!/usr/bin/env python3
"""
scorer —— 单篇论文打分模块（独立于 deep_study / simple / 每日 digest）。

四个维度：
  新鲜度 freshness   —— 代码按发表日期算（非 LLM）：score = 5*(1 - t/7)，t>7 归 0。
  可复现性 repro     —— Claude(claude-fable-5) 给 4 个子项各打 0/0.5/1，代码算加权总分。
  方法新颖度 novelty —— 先由 Claude 判定论文类型(paper_type)，再按该类型口径给 4 个子项
                       各打 0/0.5/1，代码按类型权重算加权总分（综述等不会因"无新算法"被低估）。
  领域占比 domain    —— LLM 估计各领域大致百分比（不是分数）。

交叉复核（--cross-check，默认开，仅一轮）：
  Claude 打分 → 复核模型(gpt-5.6-luna) 复核（认可就说「认可，无补充」）→ Claude 最终裁定。

输出：daily_bot/output/{date}_{arxiv_id}_score.html —— 小而自包含的 HTML 片段
（雷达图 + 各维度分数与理由 + 领域占比条 + 评分标准 + 复核补充）。

复用 relay.py（relay_chat / extract_json）。
用法： python daily_bot/scorer.py <arxiv_id> [--no-cross-check]
"""

import datetime
import html
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

import run      # 导入即加载 daily_bot/.env，并提供 arXiv XML 解析
import relay
import affiliation

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
ARXIV_API_URL = "http://export.arxiv.org/api/query"
UA = {"User-Agent": "PaperRAG-scorer/0 (mailto:elaine.wei@xpef.org)"}

MAIN_MODEL = "claude-fable-5"       # 主评分
CROSSCHECK_MODEL = "gpt-5.6-luna"   # 交叉复核（用不同家族的模型做异构复核）
MAX_TEXT_CHARS = 50_000
FRESHNESS_WINDOW_DAYS = 7

# 可复现性子项权重（合计=1）；每子项 0/0.5/1，加权后 ×5 得 0-5 分
REPRO_WEIGHTS = {
    "code_available": 0.35,
    "data_obtainable": 0.25,
    "hyperparams_disclosed": 0.25,
    "env_clear": 0.15,
}
REPRO_LABELS = {
    "code_available": "开源代码可获取",
    "data_obtainable": "数据可获取",
    "hyperparams_disclosed": "关键超参/配置已披露",
    "env_clear": "运行环境/依赖清晰",
}

# 方法新颖度：4 子项各 0/0.5/1，代码按【论文类型】选权重算加权总分（0-5）。
# 关键：不新增子项，只按类型重新解释「方法新颖性」并调整权重（避免综述因"无新算法"被低估）。
NOVELTY_SUBS = ["problem_novelty", "method_novelty",
                "theoretical_contribution", "delta_over_prior"]
NOVELTY_LABELS = {
    "problem_novelty": "问题新颖性",
    "method_novelty": "方法新颖性",
    "theoretical_contribution": "理论贡献",
    "delta_over_prior": "相对已有工作的增量",
}
NOVELTY_TYPE_NAMES = {
    "method": "新方法/模型",
    "survey": "综述/立场",
    "benchmark": "基准/数据集",
    "theory": "理论",
    "application": "应用/实证",
}
# 各类型的子项权重（每行合计=1）——同样 4 子项，按类型重新加权
NOVELTY_TYPE_WEIGHTS = {
    "method":      {"problem_novelty": 0.25, "method_novelty": 0.35,
                    "theoretical_contribution": 0.20, "delta_over_prior": 0.20},
    "survey":      {"problem_novelty": 0.30, "method_novelty": 0.35,
                    "theoretical_contribution": 0.10, "delta_over_prior": 0.25},
    "benchmark":   {"problem_novelty": 0.25, "method_novelty": 0.40,
                    "theoretical_contribution": 0.10, "delta_over_prior": 0.25},
    "theory":      {"problem_novelty": 0.20, "method_novelty": 0.30,
                    "theoretical_contribution": 0.35, "delta_over_prior": 0.15},
    "application": {"problem_novelty": 0.25, "method_novelty": 0.35,
                    "theoretical_contribution": 0.15, "delta_over_prior": 0.25},
}
# 「方法新颖性」子项在不同类型下的重新解释（其余 3 子项含义稳定）
NOVELTY_METHOD_INTERP = {
    "method": "方法/模型本身的新颖程度",
    "survey": "综述/立场的价值：是否识别真实痛点、综述是否系统、是否给出有用方向",
    "benchmark": "基准/数据集的价值与所填补的空白",
    "theory": "理论框架的新颖性",
    "application": "应用/迁移的新颖性（而非算法原创性）",
}
DEFAULT_PTYPE = "method"

# 领域占比条配色（前三为 skill 调色板）
PALETTE = ["#c0392b", "#16786a", "#2c5aa0", "#b8860b", "#7d5ba6", "#777777"]


# ---------------------------------------------------------------------------
# 取元数据 / PDF 全文
# ---------------------------------------------------------------------------

def fetch_metadata(arxiv_id):
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(
        {"id_list": arxiv_id, "max_results": 1})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        data = r.read()
    papers = run.parse_arxiv_xml(data)
    return papers[0] if papers else None


def fetch_fulltext(meta):
    """下载 PDF 抽全文（供复现性/新颖度判断）。失败则退回摘要。"""
    pdf_url = meta.get("pdf_url") or f"https://arxiv.org/pdf/{meta.get('arxiv_id')}"
    try:
        with urllib.request.urlopen(urllib.request.Request(pdf_url, headers=UA),
                                    timeout=120) as r:
            pdf_bytes = r.read()
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
        doc.close()
        return text.strip()[:MAX_TEXT_CHARS]
    except Exception as e:
        print(f"[scorer][WARN] 取全文失败（{e}），改用摘要打分")
        return meta.get("abstract") or ""


# ---------------------------------------------------------------------------
# 新鲜度（代码计算）
# ---------------------------------------------------------------------------

def freshness(published_str, today=None):
    today = today or datetime.date.today()
    try:
        pub = datetime.date.fromisoformat((published_str or "")[:10])
        t = (today - pub).days
    except Exception:
        return {"score": 0.0, "days": None, "label": "发表日期未知"}
    score = 5.0 * (1 - t / FRESHNESS_WINDOW_DAYS)
    score = max(0.0, min(5.0, score))  # t>7 → 0；未来日期 → 5
    if t <= 0:
        label = "今天发布"
    elif t == 1:
        label = "1 天前"
    elif t <= FRESHNESS_WINDOW_DAYS:
        label = f"{t} 天前"
    else:
        label = f"{t} 天前（已超出 {FRESHNESS_WINDOW_DAYS} 天新鲜窗口）"
    return {"score": round(score, 1), "days": t, "label": label}


# ---------------------------------------------------------------------------
# LLM 评分：主评 → 交叉复核 → 最终裁定
# ---------------------------------------------------------------------------

MAIN_SYS = (
    "你在给一篇论文打分，只依据给定的标题/分类/摘要/正文。输出 JSON，不要多余文字。\n"
    "1) 可复现性 reproducibility：设想「我要复现这篇，需要什么、有没有开源代码、有没有关键信息没披露」。"
    "对 4 子项各打 0/0.5/1 并各给一句 reason：code_available（开源代码是否可获取）、"
    "data_obtainable（数据能否获取）、hyperparams_disclosed（关键超参/配置是否披露）、"
    "env_clear（运行环境/依赖是否清楚）；再给 overall_reason，点出主要复现障碍。\n"
    "2) 论文类型 paper_type：先判定这篇属于以下哪一类（只选一个）并给一句 reason——"
    "method(新方法/模型) / survey(综述或立场) / benchmark(基准或数据集) / theory(理论) / "
    "application(应用或实证)。\n"
    "3) 方法新颖度 novelty：对 4 子项各打 0/0.5/1 并各给一句 reason，且【必须按上面判定的 paper_type 口径】打分：\n"
    "   - problem_novelty 问题新颖性：所研究的问题/视角是否新。\n"
    "   - method_novelty 方法新颖性：其含义随类型变化——"
    "method=方法/模型本身的新颖程度；"
    "survey=综述/立场的价值（是否识别真实痛点、综述是否系统、是否给出有用方向），"
    "【不要因为「没有提出新算法」而给低分】；"
    "benchmark=基准/数据集的价值与所填补的空白；"
    "theory=理论框架的新颖性；"
    "application=应用/迁移的新颖性（而非算法原创性）。\n"
    "   - theoretical_contribution 理论贡献：是否有理论分析/保证/证明。\n"
    "   - delta_over_prior 相对已有工作的增量：相比先前工作前进多少；其 reason【必须点名】它建立在哪些先前/相关工作之上。\n"
    "   再给一句 overall_reason。\n"
    "4) 领域占比 domain：估计各领域大致百分比（合计约 100）；至少给 2-3 个领域，务必填写、不要留空。\n"
    "只输出 JSON："
    '{"reproducibility":{"subitems":{"code_available":{"score":0,"reason":""},'
    '"data_obtainable":{"score":0,"reason":""},"hyperparams_disclosed":{"score":0,"reason":""},'
    '"env_clear":{"score":0,"reason":""}},"overall_reason":""},'
    '"paper_type":{"type":"method","reason":""},'
    '"novelty":{"subitems":{"problem_novelty":{"score":0,"reason":""},'
    '"method_novelty":{"score":0,"reason":""},"theoretical_contribution":{"score":0,"reason":""},'
    '"delta_over_prior":{"score":0,"reason":""}},"overall_reason":""},'
    '"domain":[{"area":"","pct":0}]}')

CROSS_SYS = (
    "下面是另一位评审(Claude)对一篇论文的打分与理由（含它判定的论文类型 paper_type；"
    "新颖度是按该类型口径打的 4 个子项）。请复核。对每个维度："
    "认可就 verdict=\"agree\"、reason「认可，无补充」；要改就给 reason 和 suggested。"
    "也请复核 paper_type 是否判对（类型会影响新颖度口径）。不要为挑而挑。\n"
    "只输出 JSON："
    '{"reproducibility":{"verdict":"agree","suggested":null,"reason":""},'
    '"paper_type":{"verdict":"agree","suggested":null,"reason":""},'
    '"novelty":{"verdict":"agree","suggested":null,"reason":""},'
    '"domain":{"verdict":"agree","suggested":null,"reason":""}}')

FINAL_SYS = (
    "这是你(Claude)之前对一篇论文的初评，以及另一位评审(复核模型)的复核意见。"
    "逐条判断复核意见是否有道理，做最终裁定。若改了 paper_type，请相应地按新类型口径重打新颖度子项。\n"
    "只输出 JSON：结构与初评完全相同"
    "（reproducibility.subitems 各含 score/reason 与 overall_reason；paper_type.type+reason；"
    "novelty.subitems 含 problem_novelty/method_novelty/theoretical_contribution/delta_over_prior 各 score/reason "
    "与 overall_reason；domain 列表），"
    '并额外加字段 "crosscheck_notes"：字符串数组，逐条说明接受/不接受了哪条、为什么；全维持则 []。')


def _extract_top_object(content):
    """
    稳健地取出【顶层 JSON 对象】：先直接 json.loads；否则用「字符串感知的花括号配平」
    找到第一个平衡的 {...} 再解析。比 relay.extract_json 的 first{..last} / [..] 兜底更可靠——
    避免把内层的 domain 数组元素误当成顶层对象（本次 bug 的根因）。
    """
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return next((x for x in obj if isinstance(x, dict)), None)
    except Exception:
        pass
    start = s.find("{")
    while start != -1:
        depth, instr, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if instr:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


DOMAIN_SYS = (
    "估计这篇论文大致由哪些学科/主题领域构成，用百分比表示（合计约 100），至少给 2-3 个领域。"
    '只输出 JSON：{"domain":[{"area":"领域名","pct":60},{"area":"...","pct":40}]}，不要多余文字。')


def score_domain(meta, model=MAIN_MODEL):
    """领域占比单独一小调用（主评的大 JSON 里 domain 常被模型漏掉/结构不稳，单独取更可靠）。"""
    content, _ = relay.relay_chat(DOMAIN_SYS, _paper_brief(meta), temperature=0.2,
                                  model=model, max_tokens=1_000)
    obj = _extract_top_object(content) or {}
    dom = []
    for d in (obj.get("domain") or []):
        if not isinstance(d, dict):
            continue
        try:
            pct = float(d.get("pct"))
        except (TypeError, ValueError):
            continue
        dom.append({"area": str(d.get("area") or "?").strip(), "pct": max(0.0, pct)})
    tot = sum(d["pct"] for d in dom)
    if tot > 0:
        for d in dom:
            d["pct"] = round(d["pct"] * 100 / tot, 1)
    return dom


def _paper_brief(meta, include_text=None):
    s = (f"标题：{meta.get('title')}\n"
         f"分类：{', '.join(meta.get('categories') or [])}\n"
         f"发表：{meta.get('published')}\n"
         f"摘要：{meta.get('abstract')}\n")
    if include_text:
        s += f"\n===== 正文（PDF 抽取，可能截断）=====\n{include_text}"
    return s


def score_main(meta, text, model=MAIN_MODEL):
    user = _paper_brief(meta, include_text=text)
    content, usage = relay.relay_chat(MAIN_SYS, user, temperature=0.2,
                                      model=model, max_tokens=8_000)
    return _extract_top_object(content), usage


def cross_check(meta, scores_for_llm, model=CROSSCHECK_MODEL):
    user = (_paper_brief(meta) + "\n===== Claude 的打分 =====\n"
            + json.dumps(scores_for_llm, ensure_ascii=False, indent=2))
    content, usage = relay.relay_chat(CROSS_SYS, user, temperature=0.3,
                                      model=model, max_tokens=8_000)
    return _extract_top_object(content), usage


def finalize(meta, scores_for_llm, critique, model=MAIN_MODEL):
    user = (_paper_brief(meta)
            + "\n===== 你的初评 =====\n" + json.dumps(scores_for_llm, ensure_ascii=False, indent=2)
            + "\n===== 复核意见 =====\n" + json.dumps(critique, ensure_ascii=False, indent=2))
    content, usage = relay.relay_chat(FINAL_SYS, user, temperature=0.2,
                                      model=model, max_tokens=8_000)
    return _extract_top_object(content), usage


# ---------------------------------------------------------------------------
# 领域相关性（LLM，0-5，按我们关注的具体方向打分；与「领域占比」构成不同）
# ---------------------------------------------------------------------------

# 我们跟踪的核心方向（core = 5 的锚点）
FOCUS_AREAS_DESC = {
    "agent": "LLM/AI agents、multi-agent、agentic workflow、tool use、autonomous planning",
    "quant": "量化金融、交易策略、组合/资产管理、市场预测、q-fin",
    "hpc": "高性能/分布式/并行计算、GPU、系统与基础设施、训练/推理效率",
    "lob": "限价订单簿、市场微观结构、流动性、做市、order flow",
    "ai4math": "AI for math、定理证明、形式化(Lean/Coq)、数学推理",
}

DOMAIN_REL_SYS = (
    "你在判断一篇论文与【我们关注的具体方向】的贴合程度，打 0-5 分（领域相关性）。\n"
    "我们的核心方向（core）：\n"
    + "".join(f"  - {k}：{v}\n" for k, v in FOCUS_AREAS_DESC.items())
    + "评分阶梯（判断这篇有多紧扣我们的具体方向）：\n"
    "  5   = 正中某个 core 方向（agent/quant/hpc/lob/ai4math 之一）；\n"
    "  ~4  = 泛化 AI（相关但不聚焦，如通用 LLM/CV/NLP，未直接命中 core）；\n"
    "  ~3  = 泛化 CS（外围，如一般软件工程/系统/理论）；\n"
    "  1-2 = 更远/边缘（与我们方向基本无关）。\n"
    "给 score 与一句 reason（点明命中/接近哪个方向，或为何偏远）。\n"
    '只输出 JSON：{"score":0,"area_hit":"agent|quant|hpc|lob|ai4math|general-ai|general-cs|other","reason":""}')


def score_domain_relevance(meta, model=MAIN_MODEL):
    """LLM 按我们的关注方向打 0-5 领域相关性。失败返回 None（上游标 na，绝不伪造）。"""
    content, _ = relay.relay_chat(DOMAIN_REL_SYS, _paper_brief(meta), temperature=0.2,
                                  model=model, max_tokens=800)
    obj = _extract_top_object(content) or {}
    try:
        score = float(obj.get("score"))
    except (TypeError, ValueError):
        return None
    return {"score": round(max(0.0, min(5.0, score)), 1),
            "area_hit": (obj.get("area_hit") or "").strip(),
            "reason": (obj.get("reason") or "").strip()}


# ---------------------------------------------------------------------------
# 权威性（LLM 知识优先 + OpenAlex 兜底；机构来自 PDF 第 1 页；失败/无信号 → na 非 0）
# ---------------------------------------------------------------------------

_VENUE_ACC = re.compile(r"(accepted|to appear|camera.?ready|published in|appearing in|"
                        r"proceedings|under review|forthcoming|presented at|to be published)", re.I)


def venue_from_meta(meta):
    """从 arXiv journal_ref / comment 提取发表信息 + 状态。返回 (display, status)。"""
    j = (meta.get("journal_ref") or "").strip()
    if j:
        return j, "published"
    c = (meta.get("arxiv_comment") or "").strip()
    if c and _VENUE_ACC.search(c):
        cl = c.lower()
        if "under review" in cl or "submitted to" in cl:
            status = "under-review"
        elif any(k in cl for k in ("accept", "to appear", "camera", "appearing", "published")):
            status = "accepted"
        else:
            status = "mentioned"
        return c, status
    return None, "none"


def openalex_institution(name):
    """LLM 不熟悉某机构时的兜底：OpenAlex 机构检索，返回规模/引用/主题强项。无 key；失败 None。"""
    try:
        import requests
        r = requests.get("https://api.openalex.org/institutions",
                         params={"search": name, "per_page": 1,
                                 "mailto": "elaine.wei@xpef.org"}, timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json() or {}).get("results") or []
        if not res:
            return None
        w = res[0]
        fields = [c.get("display_name") for c in (w.get("x_concepts") or [])[:6]
                  if c.get("display_name")]
        return {"name": w.get("display_name"), "country": w.get("country_code"),
                "works_count": w.get("works_count"), "cited_by_count": w.get("cited_by_count"),
                "top_fields": fields}
    except Exception:
        return None


AUTH_SYS = (
    "你在判断一篇论文的【权威性/发表质量】，打 0-5 分。这是【得体的信息披露】，绝不是贬低。\n"
    "原则：\n"
    "- 正当合法的机构给公允的中档分（≈3 起步）；不因你不熟悉就打低分；信息少但看着正规也给中档。\n"
    "- 看【全部作者与全部机构】，取整组里最强的信号，不受作者顺序限制——资深/大牛常在末位"
    "（PI/通讯作者）或中间，不要只看第一作者；任一位置出现该子领域的领军人物都算强信号。\n"
    "- 关键：该机构/实验室/作者是否在【这篇论文的具体方向】上强（如某实验室以计算金融著称，"
    "则该量化论文很权威，哪怕学校综合排名不顶尖）。\n"
    "- 若已被顶会/顶刊接收或正式发表（NeurIPS/ICML/ICLR/CVPR/ACL/顶级期刊等），加权更高；"
    "「under review」不等于已被接收，勿据此加满分。\n"
    "- 若你对某机构确实不熟、无法判断其在该方向的地位，把它列进 need_lookup（会给你外部资料再问一次）。\n"
    "评分参考：顶尖机构/大牛且方向对口 ≈5；知名机构或方向强 ≈4；正当普通高校/公司 ≈3；"
    "几乎无任何可辨识的机构/作者/发表信号 → na=true（不要给 0）。\n"
    "reason 用中文、得体披露口吻（例如「来自 X，在 Y 方向有积累」）。\n"
    '只输出 JSON：{"score":0,"reason":"","confidence":"high|med|low",'
    '"need_lookup":["不确定的机构名"],"na":false}')


INST_EXTRACT_SYS = (
    "从论文第 1 页文本中抽取【作者所属机构/单位】列表（大学/研究所/公司/实验室）。"
    "只抽取文本中真实出现的机构名，不要猜测、不要编造、不要根据作者名推断；"
    "若页面确实没有任何机构信息，就返回空列表 []。"
    "忽略「equal contribution / 共同一作」「corresponding author / 通讯作者」等与机构无关的脚注。"
    '只输出 JSON：{"institutions":["机构1","机构2"]}')


def _llm_extract_institutions(page1_text, model=MAIN_MODEL):
    """正则+邮箱都没抽到机构时的兜底：让 LLM 从第 1 页文本抽机构（格式异常的作者块）。
    真没有就返回 []（例如 VAIOM 只印了「equal contribution」而无任何机构）——保证 N/A 而非编造。"""
    try:
        content, _ = relay.relay_chat(INST_EXTRACT_SYS, (page1_text or "")[:3500],
                                      temperature=0, model=model, max_tokens=400)
        obj = _extract_top_object(content) or {}
        out = [str(x).strip() for x in (obj.get("institutions") or []) if str(x).strip()]
        return out[:6]
    except Exception:
        return []


def _authority_facts(meta, aff, venue_display, venue_status):
    direction = f"{meta.get('title')}｜分类 {', '.join(meta.get('categories') or [])}"
    return (f"论文方向：{direction}\n"
            f"摘要：{(meta.get('abstract') or '')[:600]}\n\n"
            f"{affiliation.summary_for_llm(aff)}\n\n"
            f"发表/投稿信息：{venue_display or '（arXiv 预印本，未见发表信息）'}"
            f"（状态：{venue_status}）")


def score_authority(meta, fulltext, model=MAIN_MODEL):
    """
    知识优先判权威性；仅当 LLM 对某机构不确定(need_lookup 且非 high 置信)才 OpenAlex 兜底再判一次。
    机构/作者/发表信息全缺 → na（不评分），任何失败 → na；绝不伪造 0。
    """
    aff = affiliation.extract_affiliations(fulltext, meta.get("authors"))
    # 兜底：正则+邮箱都没抽到机构 → 用一次便宜的 LLM 抽取（应对格式异常的作者块）。
    # 真的没有机构（如 VAIOM 只印 equal-contribution）→ LLM 返回 []，仍走 N/A，不编造。
    if not aff["institutions"] and not aff["email_domains"]:
        llm_inst = _llm_extract_institutions(fulltext, model)
        if llm_inst:
            aff["institutions"] = llm_inst
            print(f"[scorer] 权威性：正则未命中，LLM 兜底抽到机构 {llm_inst}")
    venue_display, venue_status = venue_from_meta(meta)
    has_signal = bool(aff["institutions"] or aff["email_domains"] or venue_status != "none")
    base = {"institutions": aff["institutions"], "email_domains": aff["email_domains"],
            "venue": venue_display, "venue_status": venue_status}
    if not has_signal:
        return {**base, "na": True, "score": None, "confidence": "low",
                "reason": "PDF 第 1 页未抽到可辨识的机构/邮箱，且无发表信息，本维度 N/A。"}

    facts = _authority_facts(meta, aff, venue_display, venue_status)
    try:
        content, _ = relay.relay_chat(AUTH_SYS, facts, temperature=0.2, model=model, max_tokens=900)
        obj = _extract_top_object(content) or {}
    except Exception as e:
        print(f"[scorer][WARN] 权威性打分失败：{e}")
        return {**base, "na": True, "score": None, "confidence": "low", "reason": ""}

    need = [x for x in (obj.get("need_lookup") or []) if isinstance(x, str) and x.strip()][:2]
    if need and str(obj.get("confidence", "")).lower() != "high":
        extra = []
        for nm in need:
            info = openalex_institution(nm)
            if info:
                extra.append(f"- {nm} → OpenAlex：作品 {info['works_count']}、被引 "
                             f"{info['cited_by_count']}、强项领域 "
                             f"{', '.join(info['top_fields'] or []) or '未知'}")
        if extra:
            print(f"[scorer] 权威性：OpenAlex 兜底 {len(extra)} 个不熟悉机构")
            user2 = facts + "\n\n===== 外部资料（OpenAlex，供判断不熟悉的机构）=====\n" + "\n".join(extra)
            try:
                content2, _ = relay.relay_chat(AUTH_SYS, user2, temperature=0.2,
                                               model=model, max_tokens=900)
                obj = _extract_top_object(content2) or obj
            except Exception:
                pass

    na = bool(obj.get("na"))
    try:
        score = float(obj.get("score"))
    except (TypeError, ValueError):
        score = None
    if score is None:
        na = True
    if na:
        return {**base, "na": True, "score": None,
                "confidence": (obj.get("confidence") or "low"),
                "reason": (obj.get("reason") or "").strip()
                or "缺足够可辨识的机构/作者/发表信号，本维度 N/A（不评分）。"}
    return {**base, "na": False, "score": round(max(0.0, min(5.0, score)), 1),
            "confidence": (obj.get("confidence") or "").strip(),
            "reason": (obj.get("reason") or "").strip()}


# ---------------------------------------------------------------------------
# 综评 composite（读 5 个真实子分 → 0-10 综评分 + 中文理由；加法式，绝不改子分）
# 结构对齐 score_main/cross_check/finalize，便于日后加 Claude→luna→Claude 复核。
# ---------------------------------------------------------------------------

COMPOSITE_SYS = (
    "你在给一篇论文打一个【综评分】(0-10)，用来把当天一批论文排序、挑出最值得读的。\n"
    "输入是这篇论文的 5 个【已定】子维度分（请勿改动、勿质疑它们，只做综合权衡）：\n"
    "  新鲜度(0-5)、可复现性(0-5)、方法新颖度(0-5)、领域相关性(0-5，对我们关注方向的贴合度)、"
    "权威性(0-5，机构/作者/发表；可能为 N/A)。\n"
    "综合判断这篇对【我们（agent/quant/hpc/lob/ai4math 方向的量化研究者）】当天的阅读价值："
    "领域相关性与新颖度通常更重要，可复现性/权威性为加分，新鲜度为次要；权威性 N/A 不扣分。\n"
    "给一个 0-10 的 composite 分（可含一位小数）和一句【中文】综评理由（≤40字，兼作『入选理由』）。\n"
    '只输出 JSON：{"composite":0,"reason":""}')


def composite_main(meta, sub_scores, model=MAIN_MODEL):
    """单次 Claude 调用：读 5 个子分 + 标题/方向 → {composite, reason}。解析失败返回 None。"""
    user = (f"标题：{meta.get('title')}\n方向：{meta.get('area') or '、'.join(meta.get('categories') or [])}\n"
            f"五个子维度分（已定，勿改）：\n{json.dumps(sub_scores, ensure_ascii=False, indent=2)}")
    content, _ = relay.relay_chat(COMPOSITE_SYS, user, temperature=0.2, model=model, max_tokens=500)
    return _extract_top_object(content)


def generate_composite(meta, sub_scores, cross_check_on=False, model=MAIN_MODEL):
    """
    综评编排。现在：单次 composite_main。返回 {score: float|None, reason: str, na: bool}。
    失败/空 → na=True, score=None（绝不伪造 0，排序置底）。
    预留 cross_check_on：日后可接 composite_review(luna) → composite_finalize(Claude)。
    """
    try:
        obj = composite_main(meta, sub_scores, model) or {}
    except Exception as e:
        print(f"[composite][WARN] {meta.get('arxiv_id')}: {e}")
        return {"score": None, "reason": "", "na": True}
    try:
        score = float(obj.get("composite"))
    except (TypeError, ValueError):
        return {"score": None, "reason": (obj.get("reason") or "").strip(), "na": True}
    return {"score": round(max(0.0, min(10.0, score)), 1),
            "reason": (obj.get("reason") or "").strip(), "na": False}
    # 日后：if cross_check_on: composite_review(luna) → composite_finalize(Claude)


# ---------------------------------------------------------------------------
# 归一化 / 计算
# ---------------------------------------------------------------------------

def _snap(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0.25 else (0.5 if v < 0.75 else 1.0)


def normalize_scores(parsed):
    """把 LLM 的评分 JSON 规整成稳定结构（缺失/异常都兜底）。"""
    p = parsed
    # 容错：模型有时把对象包在数组里（[{...}]），或返回非 dict —— 统一取出那个 dict
    if isinstance(p, list):
        p = next((x for x in p if isinstance(x, dict) and
                  ("reproducibility" in x or "novelty" in x or "domain" in x)), None) \
            or next((x for x in p if isinstance(x, dict)), {})
    if not isinstance(p, dict):
        p = {}
    rep = p.get("reproducibility") or {}
    subs_in = rep.get("subitems") or {}
    subs = {}
    for k in REPRO_WEIGHTS:
        it = subs_in.get(k) or {}
        subs[k] = {"score": _snap(it.get("score")),
                   "reason": (it.get("reason") or "").strip()}
    # 论文类型（决定新颖度口径/权重）
    pt = p.get("paper_type") or {}
    ptype = (pt.get("type") or "").strip().lower()
    if ptype not in NOVELTY_TYPE_WEIGHTS:
        ptype = DEFAULT_PTYPE
    # 方法新颖度 4 子项
    nov = p.get("novelty") or {}
    nsubs_in = nov.get("subitems") or {}
    nsubs = {}
    for k in NOVELTY_SUBS:
        it = nsubs_in.get(k) or {}
        nsubs[k] = {"score": _snap(it.get("score")),
                    "reason": (it.get("reason") or "").strip()}
    novelty_overall = (nov.get("overall_reason") or nov.get("reason") or "").strip()

    dom = []
    for d in (p.get("domain") or []):
        if not isinstance(d, dict):
            continue
        try:
            pct = float(d.get("pct"))
        except (TypeError, ValueError):
            continue
        dom.append({"area": str(d.get("area") or "?").strip(), "pct": max(0.0, pct)})
    tot = sum(d["pct"] for d in dom)
    if tot > 0:
        for d in dom:
            d["pct"] = round(d["pct"] * 100 / tot, 1)
    return {
        "repro_subs": subs,
        "repro_overall": (rep.get("overall_reason") or "").strip(),
        "paper_type": ptype,
        "paper_type_reason": (pt.get("reason") or "").strip(),
        "novelty_subs": nsubs,
        "novelty_overall": novelty_overall,
        "domain": dom,
    }


def repro_total(subs):
    return round(5.0 * sum(REPRO_WEIGHTS[k] * subs[k]["score"] for k in REPRO_WEIGHTS), 1)


def novelty_total(subs, ptype):
    """按 paper_type 选权重，代码算方法新颖度加权总分（0-5）。"""
    w = NOVELTY_TYPE_WEIGHTS.get(ptype, NOVELTY_TYPE_WEIGHTS[DEFAULT_PTYPE])
    return round(5.0 * sum(w[k] * subs[k]["score"] for k in NOVELTY_SUBS), 1)


def is_empty_score(norm):
    """
    「失败评分」签名：reproducibility 与 novelty 的所有子项分都为 0，
    且所有理由（8 个子项 reason + repro/novelty 两个 overall_reason）都为空。
    这是「模型什么都没产出」（HTTP/超时/截断/JSON 解析失败被 normalize 兜底成全零）的信号，
    而非真实判断——不能当成有效评分持久化/推送。

    与「真实的 0」区分：真实的 0 子项一定带非空 reason（例如 code_available=0 因未开源），
    或另一维度存在非零分；只有【全零且全无理由】才判为失败。
    """
    repro_scores = [norm["repro_subs"][k]["score"] for k in REPRO_WEIGHTS]
    nov_scores = [norm["novelty_subs"][k]["score"] for k in NOVELTY_SUBS]
    all_zero = not any(repro_scores) and not any(nov_scores)
    reasons = ([norm["repro_subs"][k]["reason"] for k in REPRO_WEIGHTS]
               + [norm["novelty_subs"][k]["reason"] for k in NOVELTY_SUBS]
               + [norm.get("repro_overall", ""), norm.get("novelty_overall", "")])
    all_reasons_empty = not any((r or "").strip() for r in reasons)
    return all_zero and all_reasons_empty


def scores_for_llm(norm):
    """把归一化结果转成给 LLM 复核/裁定看的紧凑结构。"""
    ptype = norm["paper_type"]
    return {
        "reproducibility": {
            "subitems": {k: {"score": v["score"], "reason": v["reason"]}
                         for k, v in norm["repro_subs"].items()},
            "total": repro_total(norm["repro_subs"]),
            "overall_reason": norm["repro_overall"],
        },
        "paper_type": {"type": ptype, "reason": norm["paper_type_reason"]},
        "novelty": {
            "paper_type": ptype,
            "subitems": {k: {"score": v["score"], "reason": v["reason"]}
                         for k, v in norm["novelty_subs"].items()},
            "total": novelty_total(norm["novelty_subs"], ptype),
            "overall_reason": norm["novelty_overall"],
        },
        "domain": norm["domain"],
    }


# ---------------------------------------------------------------------------
# 渲染：雷达图 + 领域占比条 + HTML
# ---------------------------------------------------------------------------

def _radar_svg(dims):
    """
    dims: [(label, score0-5 或 None), ...]（本模块用 5 维）。
    score=None 表示该维 N/A：几何上按中性 2.5 落点（不奖不罚），标签显示「N/A」而非数字。
    """
    cx, cy, R = 180, 175, 100
    n = len(dims)
    ang = [(-90 + i * 360.0 / n) * math.pi / 180 for i in range(n)]

    def pt(r, a):
        return (cx + r * math.cos(a), cy + r * math.sin(a))

    def geom(s):  # 几何用值：None → 2.5 中性
        return 2.5 if s is None else max(0.0, min(5.0, s))

    out = ['<svg viewBox="0 0 360 360" style="max-width:360px;width:100%" '
           'xmlns="http://www.w3.org/2000/svg">',
           '<rect width="360" height="360" fill="#faf8f5" rx="6"/>']
    for ring in range(1, 6):
        r = R * ring / 5
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(r, a) for a in ang))
        out.append(f'<polygon points="{poly}" fill="none" stroke="#e0ddd8" stroke-width="1"/>')
    for a in ang:
        x, y = pt(R, a)
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                   f'stroke="#c8c4be" stroke-width="1"/>')
    dpoly = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                     (pt(R * geom(s) / 5, a) for (l, s), a in zip(dims, ang)))
    out.append(f'<polygon points="{dpoly}" fill="#c0392b33" stroke="#c0392b" stroke-width="2"/>')
    for (l, s), a in zip(dims, ang):
        px, py = pt(R * geom(s) / 5, a)
        na = s is None
        col = "#999" if na else "#c0392b"
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="{col}"/>')
        lx, ly = pt(R + 24, a)
        anc = "middle" if abs(math.cos(a)) < 0.35 else ("start" if math.cos(a) > 0 else "end")
        out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anc}" font-size="12" '
                   f'fill="#1a1a1a" font-weight="700">{html.escape(l)}</text>')
        val = "N/A" if na else f"{s:.1f}"
        out.append(f'<text x="{lx:.1f}" y="{ly + 16:.1f}" text-anchor="{anc}" font-size="13" '
                   f'fill="{col}" font-weight="700">{val}</text>')
    out.append("</svg>")
    return "".join(out)


def _domain_bar(domain):
    if not domain:
        return '<p class="reason">（未给出领域占比）</p>'
    total = sum(d["pct"] for d in domain) or 1.0
    x0, W, y, H = 10, 340, 16, 34
    # 图例改为【纵向】：每项一行（色块 + 名称 + 百分比），避免长标签把后续项挤出 viewBox 而被裁掉。
    n = len(domain)
    line_h = 20
    legend_top = y + H + 20              # 条形下方留白后开始画图例
    vb_h = legend_top + n * line_h + 4   # 高度按项数自适应，容纳全部 N 项
    out = [f'<svg viewBox="0 0 360 {vb_h}" style="max-width:360px;width:100%" '
           f'xmlns="http://www.w3.org/2000/svg">']
    cur = x0
    for i, d in enumerate(domain):
        w = W * d["pct"] / total
        col = PALETTE[i % len(PALETTE)]
        out.append(f'<rect x="{cur:.1f}" y="{y}" width="{w:.1f}" height="{H}" fill="{col}"/>')
        if w > 30:
            out.append(f'<text x="{cur + w / 2:.1f}" y="{y + H / 2 + 4:.1f}" '
                       f'text-anchor="middle" font-size="11" fill="#fff">'
                       f'{d["pct"]:.0f}%</text>')
        cur += w
    for i, d in enumerate(domain):
        col = PALETTE[i % len(PALETTE)]
        ly = legend_top + i * line_h
        lab = f'{d["area"]} {d["pct"]:.0f}%'
        out.append(f'<rect x="10" y="{ly - 10}" width="11" height="11" fill="{col}"/>')
        out.append(f'<text x="27" y="{ly}" font-size="11.5" fill="#1a1a1a">'
                   f'{html.escape(lab)}</text>')
    out.append("</svg>")
    return "".join(out)


CSS = """
*{box-sizing:border-box}
body{font-family:'Source Sans 3',system-ui,-apple-system,'Noto Sans SC',sans-serif;
color:#1a1a1a;background:#fff;max-width:660px;margin:0 auto;padding:26px 20px 60px;line-height:1.7}
h1{font-size:1.25rem;margin:0 0 4px}
.meta{color:#777;font-size:0.85rem;margin-bottom:18px}
.grid{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:center}
.card{border:1px solid #e0ddd8;border-radius:8px;padding:14px 18px;margin:14px 0;background:#fff}
.dim{font-weight:700;font-size:1.05rem}
.score{font-family:'IBM Plex Mono',ui-monospace,monospace;color:#c0392b;font-weight:700}
.reason{color:#4a4a4a;font-size:0.93rem;margin:6px 0 0}
table{border-collapse:collapse;width:100%;font-size:0.9rem;margin:10px 0}
th,td{border:1px solid #e0ddd8;padding:5px 9px;text-align:left;vertical-align:top}
th{background:#faf8f5;font-weight:700}
td.s{font-family:'IBM Plex Mono',monospace;text-align:center;white-space:nowrap}
.rubric{background:#faf8f5;border-left:3px solid #c8c4be;padding:10px 14px;font-size:0.84rem;color:#4a4a4a;border-radius:0 6px 6px 0}
.note{background:#2c5aa015;border-left:3px solid #2c5aa0;padding:9px 13px;font-size:0.9rem;border-radius:0 6px 6px 0;margin-top:10px}
.sec-label{font-family:'IBM Plex Mono',monospace;font-size:0.72rem;letter-spacing:0.1em;text-transform:uppercase;color:#777;margin-bottom:2px}
code{background:#f4f1ec;padding:1px 4px;border-radius:3px;font-size:0.85em}
.ptype-badge{display:inline-block;background:#16786a15;border:1px solid #16786a;color:#16786a;
font-weight:700;border-radius:14px;padding:3px 12px;font-size:0.92rem;margin:2px 0 14px}
.ptype-why{color:#777;font-weight:400;font-size:0.85rem}
"""


AREA_HIT_ZH = {
    "agent": "正中 agent 核心方向", "quant": "正中 quant 核心方向",
    "hpc": "正中 hpc 核心方向", "lob": "正中 lob 核心方向",
    "ai4math": "正中 ai4math 核心方向", "general-ai": "泛化 AI（非核心聚焦）",
    "general-cs": "泛化 CS（外围）", "other": "与我们方向较远",
}


def _authority_disclosure(authority):
    """得体披露行：来自 [机构] · 刊登于 [venue]。"""
    if not authority:
        return ""
    insts = authority.get("institutions") or []
    bits = []
    if insts:
        bits.append("来自 " + "、".join(insts[:3]))
    v = authority.get("venue")
    if v:
        st = authority.get("venue_status")
        verb = {"published": "刊登于", "accepted": "被接收于",
                "under-review": "投稿于"}.get(st, "见于")
        bits.append(f"{verb} {v}")
    return " · ".join(bits)


def render_html(meta, fresh, norm, cross_notes, dom_rel=None, authority=None):
    esc = html.escape
    arxiv_id = meta.get("arxiv_id", "")
    abs_url = f"https://arxiv.org/abs/{arxiv_id}"
    title = meta.get("title") or ""
    rtot = repro_total(norm["repro_subs"])

    ptype = norm["paper_type"]
    ptname = NOVELTY_TYPE_NAMES.get(ptype, ptype)
    nweights = NOVELTY_TYPE_WEIGHTS.get(ptype, NOVELTY_TYPE_WEIGHTS[DEFAULT_PTYPE])
    ntot = novelty_total(norm["novelty_subs"], ptype)

    dr_score = dom_rel["score"] if dom_rel else None
    auth_na = (not authority) or authority.get("na")
    auth_score = None if auth_na else authority.get("score")

    radar = _radar_svg([("新鲜度", fresh["score"]),
                        ("可复现性", rtot),
                        ("新颖度", ntot),
                        ("领域相关性", dr_score),
                        ("权威性", auth_score)])
    dombar = _domain_bar(norm["domain"])

    # 可复现性子项表
    rows = []
    for k in REPRO_WEIGHTS:
        v = norm["repro_subs"][k]
        rows.append(f"<tr><td>{esc(REPRO_LABELS[k])}</td>"
                    f"<td class='s'>{v['score']:.1f}</td>"
                    f"<td class='s'>{REPRO_WEIGHTS[k]:.2f}</td>"
                    f"<td class='reason'>{esc(v['reason'])}</td></tr>")
    repro_table = ("<table><thead><tr><th>子项</th><th>0/0.5/1</th><th>权重</th>"
                   "<th>说明</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    # 方法新颖度子项表（权重随 paper_type 变化；method_novelty 标签按类型重新解释）
    nrows = []
    for k in NOVELTY_SUBS:
        v = norm["novelty_subs"][k]
        label = NOVELTY_LABELS[k]
        if k == "method_novelty":
            label += f"（本类型口径：{NOVELTY_METHOD_INTERP[ptype]}）"
        nrows.append(f"<tr><td>{esc(label)}</td>"
                     f"<td class='s'>{v['score']:.1f}</td>"
                     f"<td class='s'>{nweights[k]:.2f}</td>"
                     f"<td class='reason'>{esc(v['reason'])}</td></tr>")
    novelty_table = ("<table><thead><tr><th>子项</th><th>0/0.5/1</th><th>权重(按类型)</th>"
                     "<th>说明</th></tr></thead><tbody>" + "".join(nrows) + "</tbody></table>")

    notes_html = ""
    if cross_notes:
        items = "".join(f"<li>{esc(n)}</li>" for n in cross_notes)
        notes_html = (f'<div class="note"><strong>交叉复核补充（Claude 采纳的复核意见）：</strong>'
                      f'<ul style="margin:6px 0 0">{items}</ul></div>')

    doc_title = f"评分 · {title}" if title else "论文评分"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(doc_title)}</title>
<style>{CSS}</style></head><body>

<div class="sec-label">论文评分卡 · Paper Score</div>
<h1>{esc(title)}</h1>
<div class="meta">{esc(', '.join(meta.get('categories') or []))} · 发表 {esc(meta.get('published') or '?')}
 · <a href="{abs_url}">arXiv:{esc(arxiv_id)}</a></div>

<div class="ptype-badge">论文类型：{esc(ptname)}
 <span class="ptype-why">— {esc(norm['paper_type_reason'])}</span></div>

<div class="grid">{radar}</div>

<div class="card">
  <div class="dim">新鲜度 Freshness · <span class="score">{fresh['score']:.1f}</span> / 5</div>
  <p class="reason">{esc(fresh['label'])}。由代码按发表日期计算（非 LLM）：
  <code>score = 5·(1 − t/7)</code>，t 为距今天数，t&gt;7 记 0。该维度只衡量 7 天窗口内的新鲜度（catch-up 用途）。</p>
</div>

<div class="card">
  <div class="dim">可复现性 Reproducibility · <span class="score">{rtot:.1f}</span> / 5</div>
  <p class="reason">总分由代码按下列子项（各 0/0.5/1）加权计算，非 LLM 直接给的小数：</p>
  {repro_table}
  <p class="reason"><strong>总体：</strong>{esc(norm['repro_overall'])}</p>
</div>

<div class="card">
  <div class="dim">方法新颖度 Novelty · <span class="score">{ntot:.1f}</span> / 5
   <span class="ptype-why">（类型：{esc(ptname)}，评分口径与权重随类型调整）</span></div>
  <p class="reason">总分由代码按下列 4 子项（各 0/0.5/1）× 该类型权重加权计算：</p>
  {novelty_table}
  <p class="reason"><strong>总体：</strong>{esc(norm['novelty_overall'])}</p>
</div>

<div class="card">
  <div class="dim">领域相关性 Domain relevance · <span class="score">{(f"{dr_score:.1f}" if dr_score is not None else "N/A")}</span> / 5</div>
  <p class="reason">{esc(AREA_HIT_ZH.get((dom_rel or {}).get("area_hit"), "") ) if dom_rel else ""}
   {("— " + esc(dom_rel["reason"])) if (dom_rel and dom_rel.get("reason")) else "（本维度未能评分，N/A）"}</p>
  <p class="reason" style="color:#777">衡量与我们关注方向（agent/quant/hpc/lob/ai4math）的贴合度：正中核心=5、泛化 AI≈4、泛化 CS≈3、更远 1-2。</p>
</div>

<div class="card">
  <div class="dim">权威性 Authority · <span class="score">{(f"{auth_score:.1f}" if auth_score is not None else "N/A")}</span> / 5</div>
  <p class="reason"><strong>{esc(_authority_disclosure(authority)) or "机构/发表信息不足"}</strong></p>
  <p class="reason">{esc((authority or {}).get("reason") or "")}</p>
  <p class="reason" style="color:#777">由 LLM 依据【全部作者 + 全部机构 + 发表信息】综合判断该团队在【本文方向】上的分量（不看作者排位、取最强信号）；不熟悉的机构会查 OpenAlex 再判；正当机构给公允中档分，信息不足记 N/A 而非 0。</p>
</div>

<div class="card">
  <div class="dim">领域占比 Domain composition</div>
  <p class="reason" style="color:#777">论文由哪些学科/主题构成（构成，非分数；不进雷达）。</p>
  <div class="grid" style="margin-top:8px">{dombar}</div>
</div>

<div class="rubric">
<strong>评分标准（透明公开）</strong><br>
• <strong>新鲜度</strong>：<code>5·(1 − t/7)</code>，t=距发表天数，clamp 到 0–5；7 天以上记 0。<br>
• <strong>可复现性</strong>：4 子项各 0/0.5/1，权重
 开源代码 {REPRO_WEIGHTS['code_available']:.2f}、数据 {REPRO_WEIGHTS['data_obtainable']:.2f}、
 超参/配置 {REPRO_WEIGHTS['hyperparams_disclosed']:.2f}、环境 {REPRO_WEIGHTS['env_clear']:.2f}；
 加权和 ×5 得总分。框架：「要复现需要什么、缺哪些关键信息」。<br>
• <strong>方法新颖度</strong>：4 子项各 0/0.5/1（问题新颖性、方法新颖性、理论贡献、相对已有工作的增量），
 加权和 ×5 得总分。<strong>评分标准随论文类型自适应</strong>：先判定 paper_type
 （新方法/综述/基准/理论/应用），再据此重解释「方法新颖性」并调权重——
 例如<strong>综述/立场</strong>：「方法新颖性」= 综述/痛点识别的价值（不因「无新算法」而扣分）、理论贡献权重降低；
 <strong>基准/数据集</strong>：= 基准/数据集的价值与填补的空白；
 <strong>理论</strong>：理论贡献权重升高；
 <strong>应用/实证</strong>：= 应用/迁移的新颖性。「相对已有工作的增量」理由须点名所建立的先前工作。<br>
• <strong>领域相关性</strong>：LLM 按我们跟踪的具体方向打分——正中 agent/quant/hpc/lob/ai4math 之一=5、泛化 AI≈4、泛化 CS≈3、更远 1-2。<br>
• <strong>权威性</strong>：LLM 依据【全部作者+全部机构+发表信息】判断团队在本文方向上的分量（取最强信号、不看作者排位；不熟悉机构查 OpenAlex 兜底）。得体披露而非贬低：正当机构给公允中档分，信息不足记 <strong>N/A</strong> 而非 0。<br>
• <strong>领域占比</strong>：非分数，LLM 估计的领域构成百分比（独立于领域相关性，不进雷达）。
</div>
{notes_html}

</body></html>
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate_score(arxiv_id, cross_check_on=True, model=MAIN_MODEL):
    print(f"[scorer] 取元数据 {arxiv_id} …")
    meta = fetch_metadata(arxiv_id)
    if not meta:
        raise RuntimeError(f"arXiv 查不到 {arxiv_id}")
    print(f"[scorer] 取全文 …")
    text = fetch_fulltext(meta)

    fresh = freshness(meta.get("published"))
    print(f"[scorer] 新鲜度（代码）：{fresh['score']} （{fresh['label']}）")

    print(f"[scorer] 主评分（{model}）…")
    parsed, _ = score_main(meta, text, model)
    if not parsed:
        raise RuntimeError("主评分未能解析出 JSON")
    norm = normalize_scores(parsed)
    # 主评分为空签名（全零且无理由）→ 判为失败，绝不产出/持久化全零分（应交由上游延后重试）
    if is_empty_score(norm):
        raise RuntimeError("主评分为空/无效（所有子项为 0 且无理由，疑似 HTTP/超时/解析失败），"
                           "判为失败评分，不产出全零分")

    cross_notes = []
    if cross_check_on:
        try:
            print(f"[scorer] 交叉复核（{CROSSCHECK_MODEL}）…")
            critique, _ = cross_check(meta, scores_for_llm(norm), CROSSCHECK_MODEL)
            if critique:
                print(f"[scorer] 最终裁定（{model}）…")
                final_parsed, _ = finalize(meta, scores_for_llm(norm), critique, model)
                if final_parsed:
                    fp = final_parsed
                    if isinstance(fp, list):
                        fp = next((x for x in fp if isinstance(x, dict)), {})
                    notes = fp.get("crosscheck_notes") if isinstance(fp, dict) else None
                    cross_notes = [str(n).strip() for n in (notes or []) if str(n).strip()]
                    prev = norm  # 初评归一化结果
                    norm = normalize_scores(final_parsed)
                    # 稳健：finalize 若漏掉某字段（常见：domain 被丢空），用初评值兜底，避免维度丢失
                    if not norm["domain"] and prev["domain"]:
                        norm["domain"] = prev["domain"]
                    if not any(norm["novelty_subs"][k]["score"] for k in NOVELTY_SUBS) \
                            and any(prev["novelty_subs"][k]["score"] for k in NOVELTY_SUBS):
                        norm["novelty_subs"] = prev["novelty_subs"]
                        norm["paper_type"] = prev["paper_type"]
                    # 同理兜底 reproducibility：finalize 若把 repro 子项清零而初评有分，回退初评，避免维度丢失
                    if not any(norm["repro_subs"][k]["score"] for k in REPRO_WEIGHTS) \
                            and any(prev["repro_subs"][k]["score"] for k in REPRO_WEIGHTS):
                        norm["repro_subs"] = prev["repro_subs"]
                        norm["repro_overall"] = prev["repro_overall"]
            else:
                print("[scorer][WARN] 复核未解析出 JSON，跳过交叉复核")
        except Exception as e:
            print(f"[scorer][WARN] 交叉复核失败（{e}），仅用主评分")

    # 领域占比兜底：主评/裁定常把 domain 漏空 → 单独一小调用取，保证该维度始终有值
    if not norm["domain"]:
        try:
            print("[scorer] 领域占比单独获取（主评未给）…")
            norm["domain"] = score_domain(meta, model)
        except Exception as e:
            print(f"[scorer][WARN] 领域占比单独获取失败：{e}")

    # 最终防线：无论主评/复核/裁定哪一步出问题，绝不落库/推送一个全零无理由的伪评分
    if is_empty_score(norm):
        raise RuntimeError("评分结果为空/无效（所有子项为 0 且无理由），判为失败，不落库")

    # 领域相关性（新维度，LLM 按我们关注方向打 0-5；失败 → None，标 na）
    try:
        print("[scorer] 领域相关性（对我们关注方向的贴合度）…")
        dom_rel = score_domain_relevance(meta, model)
    except Exception as e:
        print(f"[scorer][WARN] 领域相关性失败：{e}")
        dom_rel = None

    # 权威性（新维度，机构/作者/发表信息；知识优先 + OpenAlex 兜底；失败/无信号 → na 非 0）
    try:
        print("[scorer] 权威性（机构/作者/发表信息）…")
        authority = score_authority(meta, text, model)
    except Exception as e:
        print(f"[scorer][WARN] 权威性失败：{e}")
        authority = {"na": True, "score": None, "reason": "", "confidence": "low",
                     "institutions": [], "venue": None, "venue_status": "none"}

    doc = render_html(meta, fresh, norm, cross_notes, dom_rel, authority)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today = datetime.date.today().isoformat()
    safe_id = arxiv_id.replace("/", "_")
    path = os.path.join(OUTPUT_DIR, f"{today}_{safe_id}_score.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)

    return {"meta": meta, "fresh": fresh, "norm": norm,
            "repro_total": repro_total(norm["repro_subs"]),
            "novelty_total": novelty_total(norm["novelty_subs"], norm["paper_type"]),
            "domain_relevance": dom_rel, "authority": authority,
            "cross_notes": cross_notes, "path": path,
            "cross_check": cross_check_on}


def main():
    args = sys.argv[1:]
    cross_on = "--no-cross-check" not in args
    args = [a for a in args if a not in ("--no-cross-check", "--cross-check")]
    if not args:
        print("用法: python daily_bot/scorer.py <arxiv_id> [--no-cross-check]")
        sys.exit(1)
    r = generate_score(args[0].strip(), cross_check_on=cross_on)
    n = r["norm"]
    print("\n== 评分完成 ==")
    print(f"title      : {r['meta'].get('title')}")
    print(f"cross-check: {'ON' if r['cross_check'] else 'OFF'}")
    print(f"新鲜度     : {r['fresh']['score']}/5  （{r['fresh']['label']}）")
    print(f"可复现性   : {r['repro_total']}/5")
    for k in REPRO_WEIGHTS:
        v = n["repro_subs"][k]
        print(f"    - {REPRO_LABELS[k]}: {v['score']}  — {v['reason'][:50]}")
    print(f"    overall: {n['repro_overall'][:80]}")
    pt = n["paper_type"]
    print(f"论文类型   : {NOVELTY_TYPE_NAMES.get(pt, pt)} — {n['paper_type_reason'][:60]}")
    print(f"方法新颖度 : {r['novelty_total']}/5  （按 {pt} 口径/权重）")
    for k in NOVELTY_SUBS:
        v = n["novelty_subs"][k]
        w = NOVELTY_TYPE_WEIGHTS[pt][k]
        print(f"    - {NOVELTY_LABELS[k]} (w={w}): {v['score']}  — {v['reason'][:45]}")
    print(f"    overall: {n['novelty_overall'][:80]}")
    print(f"领域占比   : " + ", ".join(f"{d['area']} {d['pct']:.0f}%" for d in n["domain"]))
    if r["cross_notes"]:
        print("交叉复核采纳：")
        for note in r["cross_notes"]:
            print(f"    · {note}")
    print(f"output     : {r['path']}")


if __name__ == "__main__":
    main()
