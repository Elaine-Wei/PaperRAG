"""
relay —— 与 OpenAI 兼容 relay 的通用调用（供 digest 与 stage-B 共用，避免重复/循环依赖）。

配置从环境变量读取（daily_bot/.env 里的 RELAY_API_KEY 等，由 run.py 在启动时加载）：
  RELAY_API_KEY（必需）、RELAY_BASE_URL（默认 a6）、RELAY_MODEL（默认 claude-fable-5）。
env 在调用时读取（而非 import 时），确保 .env 已被 run.py 加载。
"""

import json
import os
import urllib.request

# 单一真源：relay 的默认模型与 base_url（run.py 等一律引用这里，避免默认值分叉）
DEFAULT_MODEL = "claude-fable-5"
DEFAULT_BASE_URL = "https://a6.a6api.com/v1"


def _relay_config():
    return (
        os.environ.get("RELAY_API_KEY"),
        os.environ.get("RELAY_BASE_URL", DEFAULT_BASE_URL),
        os.environ.get("RELAY_MODEL", DEFAULT_MODEL),
    )


def relay_chat(system_prompt, user_prompt, temperature=0.3, timeout=90,
               max_tokens=None, model=None):
    """
    发一条 system+user 消息，返回 (content, usage)。
    无 API key → 抛 RuntimeError；网络/HTTP/JSON 错误照常抛出，由调用方处理。
    max_tokens：可选，限制/放开输出长度（深度精读需要很长输出时传大值）。
    model：可选，覆盖默认模型（用于按模型切换，如 claude-fable-5 / gpt-5.6-luna）。
    """
    api_key, base_url, env_model = _relay_config()
    if not api_key:
        raise RuntimeError("环境变量 RELAY_API_KEY 未设置")
    model = model or env_model

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    content = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage")  # OpenAI 格式：prompt/completion/total_tokens
    return content, usage


def extract_json(text):
    """
    尽力从模型输出中解析 JSON：去掉 ```fence``` → 直接解析 → 截取首个 {...} 或 [...] 再解析。
    解析失败返回 None。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                pass
    return None
