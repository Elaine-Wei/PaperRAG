import time
import xml.etree.ElementTree as ET

import requests

from config import ARXIV_BATCH_SIZE, ARXIV_DELAY

ARXIV_API_URL = "http://export.arxiv.org/api/query"

NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def parse_arxiv_xml(xml_data):
    """解析 Atom XML，提取所有字段，返回论文 dict 列表。"""
    papers = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        print(f"[WARN] parse_arxiv_xml: 无法解析 XML: {e}")
        return papers

    for entry in root.findall("atom:entry", NAMESPACES):
        # arxiv_id：从 <id> 提取，去掉版本号
        id_el = entry.find("atom:id", NAMESPACES)
        if id_el is None or not id_el.text:
            continue
        raw_id = id_el.text.strip()
        arxiv_id = raw_id.split("/abs/")[-1].split("v")[0]

        title_el = entry.find("atom:title", NAMESPACES)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        abstract_el = entry.find("atom:summary", NAMESPACES)
        abstract = (
            abstract_el.text.strip()
            if abstract_el is not None and abstract_el.text
            else ""
        )

        authors = []
        for author in entry.findall("atom:author", NAMESPACES):
            name_el = author.find("atom:name", NAMESPACES)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        categories = []
        for cat in entry.findall("atom:category", NAMESPACES):
            term = cat.get("term")
            if term:
                categories.append(term)

        published_el = entry.find("atom:published", NAMESPACES)
        published = ""
        if published_el is not None and published_el.text:
            # 形如 2024-01-15T00:00:00Z -> 2024-01-15
            published = published_el.text.strip()[:10]

        # pdf_url：找 rel="related"/title="pdf" 的 link，退回构造
        pdf_url = ""
        for link in entry.findall("atom:link", NAMESPACES):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
                break
        if not pdf_url and arxiv_id:
            pdf_url = f"http://arxiv.org/pdf/{arxiv_id}"

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "abstract": abstract,
                "authors": authors,
                "categories": categories,
                "published": published,
                "pdf_url": pdf_url,
            }
        )

    return papers


def fetch_arxiv(query, start, max_results):
    """单次 HTTP GET，返回解析后的论文列表。失败返回空列表。"""
    params = {
        "search_query": query,
        "start": start,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        resp = requests.get(ARXIV_API_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[WARN] fetch_arxiv: 请求失败 (start={start}): {e}")
        return []

    return parse_arxiv_xml(resp.content)


def fetch_all_recent(query, total):
    """分页抓取，内置限速 sleep。最多返回 total 篇。"""
    results = []
    start = 0
    while len(results) < total:
        max_results = min(ARXIV_BATCH_SIZE, total - len(results))
        batch = fetch_arxiv(query, start, max_results)
        if not batch:
            break
        results.extend(batch)
        start += len(batch)
        # 返回条数不足一批，说明没有更多结果了
        if len(batch) < max_results:
            break
        time.sleep(ARXIV_DELAY)
    return results[:total]


def fetch_new_papers(query, total, seen_ids):
    """调用 fetch_all_recent，过滤已见过的 arxiv_id。"""
    papers = fetch_all_recent(query, total)
    if seen_ids is None:
        seen_ids = set()
    return [p for p in papers if p["arxiv_id"] not in seen_ids]
