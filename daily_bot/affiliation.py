#!/usr/bin/env python3
"""
affiliation —— 从论文 PDF 第 1 页抽取【作者机构 + 邮箱域名】。

recon 验证：单独的 arXiv <arxiv:affiliation> API 字段几乎恒空（~0%），
但 PDF 第 1 页作者块几乎必印机构（~80% 关键词可命中，加邮箱域名合计 ~93%）。
本模块只做「事实抽取」，不做任何评分/排名——把机构清单交给 LLM 判断权威性。

用法：extract_affiliations(fulltext_or_page1, authors) -> dict
  - fulltext：scorer 已下载的 PDF 全文（取开头一段即为第 1 页），避免二次下载。
  - authors：arXiv 元数据里的作者名列表（全部作者，用于「谁站在这篇后面」的判断）。
返回：{"institutions": [...], "email_domains": [...], "authors": [...]}
"""

import re

# 机构关键词（中英）：命中即认为该行含机构名
_INST_RE = re.compile(
    r"\b(University|Universit[eéà]|Universität|Institute|Institut|Laborator|"
    r"Department|College|School of|Academy|Acad[eé]mie|Corporation|Research|"
    r"Technolog|Politecnico|Tsinghua|Peking|MIT|Google|Microsoft|Meta AI|"
    r"DeepMind|OpenAI|Anthropic|Amazon|IBM|Nvidia|NVIDIA|Huawei|Alibaba|"
    r"Tencent|ByteDance|Baidu|Samsung|Inc\.|Ltd|GmbH|Labs?)\b")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

# 免费邮箱域名：出现也不代表机构，抽取时丢弃（避免把 gmail 当机构）
_FREE_MAIL = {"gmail.com", "hotmail.com", "outlook.com", "163.com", "126.com",
              "qq.com", "yahoo.com", "foxmail.com", "icloud.com", "protonmail.com",
              "live.com", "aol.com"}


def _clean_line(l):
    return re.sub(r"\s+", " ", l).strip()


def extract_affiliations(fulltext, authors=None):
    """
    从 PDF 全文开头（≈第 1 页）抽机构行 + 邮箱域名。纯事实，不评分。
    机构行为空不代表没有机构——可能格式特殊，交由上游 LLM 兜底（或标注 na）。
    """
    authors = list(authors or [])
    head = (fulltext or "")[:2600]  # 第 1 页作者块通常在最前
    lines = [_clean_line(l) for l in head.split("\n") if l.strip()]

    inst_lines, seen = [], set()
    for l in lines[:45]:
        if not (3 < len(l) < 110):
            continue
        if l.lower().startswith("abstract"):
            break  # 到摘要就过了作者块
        if _INST_RE.search(l):
            key = l.lower()
            if key not in seen:
                seen.add(key)
                inst_lines.append(l)
        if len(inst_lines) >= 6:
            break

    domains = []
    for m in _EMAIL_RE.finditer(head):
        d = m.group(1).lower()
        if d in _FREE_MAIL or d in domains:
            continue
        domains.append(d)

    return {"institutions": inst_lines, "email_domains": domains, "authors": authors}


def summary_for_llm(aff):
    """把抽取结果整理成喂给 LLM 的紧凑文本（不含任何分数/排名倾向）。"""
    inst = aff.get("institutions") or []
    dom = aff.get("email_domains") or []
    auth = aff.get("authors") or []
    parts = []
    parts.append("机构（PDF 第 1 页抽取）：" + ("；".join(inst) if inst else "（正则未命中，见邮箱域名/作者）"))
    parts.append("邮箱域名（可辅助判断机构）：" + ("、".join(dom) if dom else "（无）"))
    parts.append("全部作者（不分先后，senior/PI 常在末位）：" + ("、".join(auth) if auth else "（未知）"))
    return "\n".join(parts)
