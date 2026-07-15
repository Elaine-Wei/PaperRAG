import re
import time

import requests

from config import ENRICHMENT_DELAY, OPENALEX_API_KEY, OPENALEX_MAILTO

OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def _openalex_auth_params():
    """每个 OpenAlex 请求都带上 api_key + mailto（都可选，缺失就不带）。"""
    params = {}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    return params


def _openalex_fields(work):
    """从一个 OpenAlex work 对象抽取我们要的字段。缺失一律 None（不伪造 0）。"""
    ref_count = work.get("referenced_works_count")
    if ref_count is None and isinstance(work.get("referenced_works"), list):
        ref_count = len(work["referenced_works"])
    return {
        "citation_count": work.get("cited_by_count"),
        "reference_count": ref_count,
        "year": work.get("publication_year"),
        # OpenAlex 没有“influential citations”这一概念：保留键以对齐 schema，但不编造。
        "influential_citation_count": None,
    }


def _norm_tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _titles_match(ours, theirs):
    """search 回退可能匹配错论文：要求返回标题与我们标题有高重叠度才信任。"""
    a, b = _norm_tokens(ours), _norm_tokens(theirs)
    if not a or not b:
        return False
    overlap = len(a & b) / len(a)  # 我们标题里的词有多大比例出现在对方标题
    return overlap >= 0.6


def enrich_openalex(arxiv_id, title):
    """
    OpenAlex 引用信息（替代原 Semantic Scholar）。任何失败返回 {}。

    - 主路径（免费）：按 DOI 单条查询 works/https://doi.org/10.48550/arXiv.{arxiv_id}。
      DOI 精确，命中即用，无需标题校验、无选错论文风险。
    - 回退（花费额度，仅当 DOI 404）：search=<title>，且必须通过标题相似度校验才信任。
    """
    try:
        # ---- 主路径：DOI 单条查询（1 credit，近乎免费）----
        doi_url = (OPENALEX_WORKS_URL
                   + f"/https://doi.org/10.48550/arXiv.{arxiv_id}")
        resp = requests.get(doi_url, params=_openalex_auth_params(), timeout=30)
        if resp.status_code == 200:
            work = resp.json()
            if isinstance(work, dict) and work.get("id"):
                print(f"[openalex] {arxiv_id}: DOI 单条命中")
                return _openalex_fields(work)
            # 200 但结构异常，当作未命中继续回退
        elif resp.status_code != 404:
            resp.raise_for_status()  # 其它非 404 状态：交给 except 记录

        # ---- 回退：title search（仅 DOI 404 时；花费额度）----
        print(f"[openalex] {arxiv_id}: DOI 未命中(404)，回退 search")
        # 去掉标点：OpenAlex 的 search 解析器遇到 ? : 等字符会返回 400
        search_term = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).strip()
        search_term = re.sub(r"\s+", " ", search_term)
        if not search_term:
            return {}
        params = _openalex_auth_params()
        params.update({"search": search_term, "per_page": 1})
        resp = requests.get(OPENALEX_WORKS_URL, params=params, timeout=30)
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
        if not results:
            print(f"[openalex] {arxiv_id}: search 无结果，放弃")
            return {}
        cand = results[0]
        if not _titles_match(title, cand.get("display_name")):
            print(f"[openalex] {arxiv_id}: search 结果标题不匹配，放弃")
            return {}
        print(f"[openalex] {arxiv_id}: search 命中并通过标题校验")
        return _openalex_fields(cand)

    except (requests.RequestException, ValueError) as e:
        print(f"[WARN] enrich_openalex({arxiv_id}): {e}")
        return {}


def enrich_hugging_face(arxiv_id):
    """Hugging Face Papers 社区点赞数。任何失败返回 {}。"""
    url = f"https://huggingface.co/api/papers/{arxiv_id}"

    try:
        resp = requests.get(url, timeout=30)
        # 未被 HF 社区收录
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARN] enrich_hugging_face({arxiv_id}): {e}")
        return {}

    # HF 有时返回列表，有时返回单对象；两种都防御性处理
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    if not isinstance(data, dict):
        return {}

    upvotes = data.get("upvotes")
    if upvotes is None:
        # 部分响应把字段包在 paper 对象里
        paper = data.get("paper")
        if isinstance(paper, dict):
            upvotes = paper.get("upvotes")

    # 保留 None 表示“未知”，与其它增强源一致
    return {"hf_upvotes": upvotes}


def enrich_paper(arxiv_id, title, include_hf=True):
    """
    依次调用各增强源，合并结果。任一失败不影响其余。
    每次外部请求后都 sleep(ENRICHMENT_DELAY)，保证任意两次外部调用之间都有间隔。

    include_hf：仅首次增强时为 True（跑 HF）。OpenAlex 重试（论文已尝试过、仍缺引用
    数据、7 天内）时传 False —— 重试只为补 OpenAlex，不再重复请求 HF。
    """
    merged = {}

    merged.update(enrich_openalex(arxiv_id, title))
    time.sleep(ENRICHMENT_DELAY)

    if include_hf:
        merged.update(enrich_hugging_face(arxiv_id))
        time.sleep(ENRICHMENT_DELAY)

    return merged
