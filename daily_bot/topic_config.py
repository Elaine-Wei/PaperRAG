"""Config loader for data-driven run_topic boards.

The legacy selfevo board remains hardcoded in run_topic.py.  Config entries
are opt-in and load their verified seeds/anchors from topic_bootstrap JSON
artifacts without requiring Python edits.
"""

import json
import os
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "topics_config.json"
WEIGHT_KEYS = ("freshness", "reproducibility", "novelty", "domain_relevance", "authority")
WEIGHT_ALIASES = {"repro": "reproducibility"}


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 无法解析: {path}: {exc}") from exc


def _validate_weights(raw, topic):
    if not isinstance(raw, dict):
        raise ValueError(f"主题 {topic!r} 的 weights 必须是对象")
    weights = {}
    for key, value in raw.items():
        key = WEIGHT_ALIASES.get(key, key)
        if key not in WEIGHT_KEYS:
            raise ValueError(f"主题 {topic!r} 有未知权重字段: {key}")
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"主题 {topic!r} 的权重 {key} 不是数字") from exc
        if value < 0:
            raise ValueError(f"主题 {topic!r} 的权重 {key} 不能为负")
        weights[key] = value
    missing = [key for key in WEIGHT_KEYS if key not in weights]
    if missing:
        raise ValueError(f"主题 {topic!r} 缺少权重字段: {', '.join(missing)}")
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        raise ValueError(f"主题 {topic!r} 的权重合计必须为 1（当前 {sum(weights.values()):.6f}）")
    return weights


def _verified_maps(artifact, topic):
    verified = artifact.get("verified_anchors")
    if not isinstance(verified, list):
        raise ValueError(f"主题 {topic!r} 的 bootstrap artifact 缺少 verified_anchors 数组")
    seeds, anchors, expected = {}, {}, {}
    for item in verified:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("arxiv_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not aid or not title:
            continue
        target = seeds if item.get("category") == "foundational" else anchors
        target[aid] = title
    if not seeds and not anchors:
        raise ValueError(f"主题 {topic!r} 的 bootstrap artifact 没有可用 verified anchors")
    return seeds, anchors, expected


def _flatten_keywords(artifact, topic):
    nets = artifact.get("keyword_nets") or []
    queries = []
    for net in nets:
        for item in (net.get("queries") or []) if isinstance(net, dict) else []:
            query = item.get("query") if isinstance(item, dict) else item
            if str(query or "").strip():
                queries.append(str(query).strip())
    if not queries:
        snippet = artifact.get("copy_ready_snippet") or {}
        queries = [str(q).strip() for q in (snippet.get("KW") or []) if str(q).strip()]
    if not queries:
        raise ValueError(f"主题 {topic!r} 的 bootstrap artifact 没有 keyword queries")
    return list(dict.fromkeys(queries))


def _judge_prompt(label, description):
    return (
        f"判断这篇论文是否属于【{label}】主题：{description}。"
        "只依据标题和摘要；属于该主题输出 true，否则输出 false。"
        '只输出 JSON：{"is_topic":true或false,"reason":"一句中文理由"}'
    )


def _importance_prompt(label, description):
    return (
        f"你在为【{label}】主题榜单做重要性预排。主题范围：{description}。"
        "根据标题和摘要给领域重要性打 1-10 分：开创性、代表性、影响力强→高；"
        '增量或边缘工作→低。只输出 JSON：{"importance":1到10的整数,"reason":"一句中文"}'
    )


def load_topics_config(path=None):
    path = Path(path or CONFIG_PATH)
    raw = _read_json(path)
    topics = raw.get("topics") if isinstance(raw, dict) else None
    if not isinstance(topics, dict):
        raise ValueError(f"{path} 必须包含 topics 对象")
    required = ("label", "description", "bootstrap_json", "prefilter_terms",
                "weights", "top_n", "study_top", "shortlist_top")
    out = {}
    for topic, item in topics.items():
        if not isinstance(item, dict):
            raise ValueError(f"主题 {topic!r} 配置必须是对象")
        missing = [key for key in required if key not in item]
        if missing:
            raise ValueError(f"主题 {topic!r} 缺少字段: {', '.join(missing)}")
        terms = [str(x).strip() for x in item["prefilter_terms"] if str(x).strip()]
        if not terms:
            raise ValueError(f"主题 {topic!r} 的 prefilter_terms 不能为空")
        try:
            numeric = {key: int(item[key]) for key in ("top_n", "study_top", "shortlist_top")}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"主题 {topic!r} 的榜单数量必须是整数") from exc
        if any(value < 1 for value in numeric.values()):
            raise ValueError(f"主题 {topic!r} 的榜单数量必须为正数")
        bootstrap = Path(str(item["bootstrap_json"]))
        if not bootstrap.is_absolute():
            candidates = [path.parent / bootstrap, path.parent.parent / bootstrap]
            bootstrap = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        artifact = _read_json(bootstrap)
        seeds, anchors, expected = _verified_maps(artifact, topic)
        out[topic] = {
            "topic": topic,
            "label": str(item["label"]).strip(),
            "description": str(item["description"]).strip(),
            "seeds": seeds,
            "anchors": anchors,
            "verified": {**seeds, **anchors},
            "expected_keywords": item.get("expected_keywords") or expected,
            "keywords": _flatten_keywords(artifact, topic),
            "prefilter_pattern": re.compile("|".join(re.escape(x) for x in terms), re.I),
            "judge_system": item.get("judge_prompt") or _judge_prompt(item["label"], item["description"]),
            "importance_system": item.get("importance_prompt") or _importance_prompt(item["label"], item["description"]),
            "foundational_days": int(item.get("foundational_days", 365)),
            "weights": _validate_weights(item["weights"], topic),
            **numeric,
        }
    return out


def load_topic(topic, path=None):
    """Return one validated config topic, or None for a legacy topic."""
    return load_topics_config(path).get(topic)
