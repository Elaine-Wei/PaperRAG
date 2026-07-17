#!/usr/bin/env python3
"""
wecom —— 企业微信群机器人推送。

对一篇论文推送两条：
  1) markdown 文本（标题 / 方向 / 摘要 / 评分）
  2) 完整报告 HTML 作为【文件】（webhook 先 upload_media 拿 media_id，再发 file 消息）

去重与限流由调用方（run.py）负责：调用前用 db.already_pushed 跳过已推的；
每次调用内部 send 之间 sleep，调用方在论文之间也 sleep（20 条/分钟）。
webhook 从环境变量 WECOM_WEBHOOK_URL 读取（放在 daily_bot/.env）。
"""

import os
import re
import time

import requests

WEBHOOK = os.environ.get("WECOM_WEBHOOK_URL")
UPLOAD_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
SEND_GAP = 4.0  # 每次 send 之间的间隔（秒），避免触发 20/min 限流


def _key(url):
    m = re.search(r"[?&]key=([^&]+)", url or "")
    return m.group(1) if m else None


def send_markdown(webhook, content):
    r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"content": content}},
                      timeout=30)
    j = r.json()
    return j.get("errcode") == 0, j


def upload_file(webhook, file_path):
    key = _key(webhook)
    if not key:
        return None, {"error": "webhook 里没有 key"}
    with open(file_path, "rb") as f:
        files = {"media": (os.path.basename(file_path), f, "text/html")}
        r = requests.post(f"{UPLOAD_URL}?key={key}&type=file", files=files, timeout=60)
    j = r.json()
    return (j.get("media_id") if j.get("errcode") == 0 else None), j


def send_file(webhook, media_id):
    r = requests.post(webhook, json={"msgtype": "file", "file": {"media_id": media_id}},
                      timeout=30)
    j = r.json()
    return j.get("errcode") == 0, j


def _short_venue(v):
    """把可能很长的发表信息压成简短会议/期刊名（WeCom markdown 有限）。"""
    v = (v or "").strip()
    m = re.search(r"\(([^)]{2,42})\)\s*$", v)   # 结尾括号如 (AISTATS 2011)
    if m:
        return m.group(1).strip()
    return v[:48] + ("…" if len(v) > 48 else "")


def format_scores(sc):
    """
    把 db.get_score 的一行整理成群消息用的简短评分块（含两个新维度）。
    权威性得体披露：N/A 只显示「N/A（来自 [机构]）」或「N/A」，绝不显示 0 或贬低。
    """
    if not sc:
        return "（暂无评分）"
    lines = [f"新鲜度 {sc['freshness_score']} · 可复现 {sc['repro_score']} · "
             f"新颖度 {sc['novelty_total']}（{sc['paper_type']}）"]

    dr = sc.get("domain_relevance_score")
    dr_txt = f"领域相关性 {dr}/5" if dr is not None else "领域相关性 —"

    insts = sc.get("authority_institutions") or []
    inst_str = "、".join(insts[:2])
    venue = sc.get("authority_venue")
    if sc.get("authority_na") or sc.get("authority_score") is None:
        auth_txt = f"权威性 N/A（来自 {inst_str}）" if inst_str else "权威性 N/A"
    else:
        disc = []
        if inst_str:
            disc.append(f"来自 {inst_str}")
        if venue:
            disc.append(f"刊登于 {_short_venue(venue)}")
        disc_txt = f"（{'｜'.join(disc)}）" if disc else ""
        auth_txt = f"权威性 {sc['authority_score']}/5{disc_txt}"

    lines.append(f"{dr_txt} · {auth_txt}")
    return "\n".join(lines)


def push_paper(title, area, summary, scores_text, file_path, webhook=None):
    """推送一篇：markdown + 文件。返回 (ok, detail)。"""
    webhook = webhook or WEBHOOK
    if not webhook:
        return False, "WECOM_WEBHOOK_URL 未设置"
    md = (f"### 📄 {title}\n"
          f"> 方向：<font color=\"info\">{area}</font>\n\n"
          f"{summary}\n\n"
          f"**评分**：{scores_text}")
    md = md[:4000]  # WeCom markdown 上限约 4096 字节
    ok1, j1 = send_markdown(webhook, md)
    if not ok1:
        return False, f"markdown 失败: {j1}"
    time.sleep(SEND_GAP)
    media_id, ju = upload_file(webhook, file_path)
    if not media_id:
        return False, f"上传文件失败: {ju}"
    time.sleep(SEND_GAP)
    ok2, j2 = send_file(webhook, media_id)
    if not ok2:
        return False, f"发送文件消息失败: {j2}"
    return True, f"ok (media_id={str(media_id)[:10]}…)"
