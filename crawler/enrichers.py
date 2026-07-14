import time

import requests

from config import ENRICHMENT_DELAY, S2_API_KEY


def enrich_semantic_scholar(arxiv_id):
    """Semantic Scholar 引用信息。任何失败返回 {}。"""
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
    params = {"fields": "citationCount,influentialCitationCount,referenceCount,year"}

    headers = {}
    if S2_API_KEY:
        headers["x-api-key"] = S2_API_KEY

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        # 论文太新尚未收录
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[WARN] enrich_semantic_scholar({arxiv_id}): {e}")
        return {}

    # 限速统一由 enrich_paper 在每次调用后处理，这里不再单独 sleep
    # 保留 None 表示“未知”，不要 coalesce 成 0（区别于真实的 0）
    return {
        "citation_count": data.get("citationCount"),
        "influential_citation_count": data.get("influentialCitationCount"),
        "reference_count": data.get("referenceCount"),
        "year": data.get("year"),
    }


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


def enrich_paper(arxiv_id):
    """
    依次调用各增强源（Semantic Scholar、Hugging Face），合并结果。任一失败不影响其余。

    每次外部请求后都 sleep(ENRICHMENT_DELAY)，保证任意两次外部调用之间都有间隔——
    对同一个 API（尤其是 Semantic Scholar 匿名访问）的两次相邻命中，间隔至少是
    处理一整篇论文的时间，稳妥地待在限速线以下。
    """
    merged = {}

    merged.update(enrich_semantic_scholar(arxiv_id))
    time.sleep(ENRICHMENT_DELAY)

    merged.update(enrich_hugging_face(arxiv_id))
    time.sleep(ENRICHMENT_DELAY)

    return merged
