"""
relay —— 与 OpenAI 兼容 relay 的通用调用（供 digest 与 stage-B 共用，避免重复/循环依赖）。

配置从环境变量读取（daily_bot/.env 里的 RELAY_API_KEY 等，由 run.py 在启动时加载）：
  RELAY_API_KEY（必需，=A）、RELAY_API_KEY_2（可选，=B）、RELAY_API_KEY_3（可选，=C）、
  RELAY_BASE_URL（默认 a6）、RELAY_MODEL（默认 claude-fable-5）。
env 在调用时读取（而非 import 时），确保 .env 已被 run.py 加载。

多 key（最多 3 把）负载均衡（RELAY_API_KEY_2/_3 存在时启用；每把 key 在同一 relay 上各有独立的按-key 限流）：
  ·主动轮询（主机制）：相邻 relay_chat 调用轮流用 A/B/C，把负载 ~1/n 摊开，各把都远离各自限流。
  ·失败切换（安全网）：某次调用遇 503/429/连接类错误 → 抖动 2-3s 后立刻用下一把 key 重试同一调用，循环所有 key。
  ·按-key 冷却（让被限的那把歇会儿）：某 key 抛 503/429 → 标记冷却 120s，其间优先路由到未冷却的 key。
  ·只有 503/429/连接级错误触发切换/冷却；400/内容类错误立即抛出、不切换。
  ·仅当【所有】可用 key 在同一次调用里都失败才抛出（疑似账号/IP 级或 relay-wide 抖动，轮换无法缓解）。
  ·RELAY_API_KEY_2/_3 均未设置 → 单 key，行为与从前逐字节一致（无切换/冷却/日志、异常与返回形状不变）。
"""

import http.client
import json
import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.request

# 单一真源：relay 的默认模型与 base_url（run.py 等一律引用这里，避免默认值分叉）
DEFAULT_MODEL = "claude-fable-5"
DEFAULT_BASE_URL = "https://a6.a6api.com/v1"

COOLDOWN_S = 120                       # 某 key 抛 503/429 后冷却时长（其间优先另一把）
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}

# 模块级状态（这些 runner 都是单线程顺序调用，无需加锁；进程内存活，长跑中持续轮询）
_next = 0                              # 轮询计数器：每次 relay_chat +1
_cooldown = {}                         # label -> 冷却截止的 unix 时间（按需填充，支持任意把数）
_served = {}                           # label -> 累计服务次数（用于观察 ~1/n 分流）


def _base_url():
    return os.environ.get("RELAY_BASE_URL", DEFAULT_BASE_URL)


def _model():
    return os.environ.get("RELAY_MODEL", DEFAULT_MODEL)


def _keys():
    """返回 [("A",kA)]（单）/ [...("B",kB)]（双）/ [...("C",kC)]（三）。无 A → 抛错。"""
    a = os.environ.get("RELAY_API_KEY")
    if not a:
        raise RuntimeError("环境变量 RELAY_API_KEY 未设置")
    keys = [("A", a)]
    for label, env in (("B", "RELAY_API_KEY_2"), ("C", "RELAY_API_KEY_3")):
        v = os.environ.get(env)
        if v:
            keys.append((label, v))
    return keys


# 兼容旧调用点：仍暴露单 key 配置（内部负载均衡不再走它，但保留以防外部引用）
def _relay_config():
    return (os.environ.get("RELAY_API_KEY"), _base_url(), _model())


def _is_retryable(exc):
    """只有 503/429 与连接级错误（超时/EOF/reset/握手）才触发切换+冷却；400/内容类不触发。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_HTTP
    return isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout,
                            ConnectionError, http.client.IncompleteRead,
                            http.client.RemoteDisconnected, ssl.SSLError))


def _errbrief(exc):
    return f"{type(exc).__name__}:{str(exc)[:44]}"


def _log(msg):
    print(f"[relay] {msg}", flush=True)


def _call(api_key, base_url, model, system_prompt, user_prompt, temperature, timeout, max_tokens):
    """底层单次 HTTP 调用：成功返回 (content, usage)，否则抛出（HTTP/网络/JSON 错误照常传播）。"""
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


def relay_chat(system_prompt, user_prompt, temperature=0.3, timeout=90,
               max_tokens=None, model=None):
    """
    发一条 system+user 消息，返回 (content, usage)。
    无 API key → 抛 RuntimeError；网络/HTTP/JSON 错误照常抛出，由调用方处理。
    max_tokens：可选，限制/放开输出长度（深度精读需要很长输出时传大值）。
    model：可选，覆盖默认模型（用于按模型切换，如 claude-fable-5 / gpt-5.6-luna）。

    多 key（2/3 把）存在时：主动轮询 + 失败切换 + 120s 冷却（见模块 docstring）。
    单 key 时：仅尝试一次，异常/返回与从前逐字节一致（无切换/冷却/日志）。
    """
    global _next
    keys = _keys()
    base_url, env_model = _base_url(), _model()
    model = model or env_model
    n = len(keys)
    multi = n > 1

    if multi:
        start = _next % n
        _next += 1
        rotated = keys[start:] + keys[:start]      # 轮询：主选在前，其余按序其后
        now = time.time()
        # 冷却路由：未冷却的排前面（保持轮询序），冷却中的沉底、仅在其余都失败时兜底尝试
        non_cooling = [k for k in rotated if _cooldown.get(k[0], 0.0) <= now]
        cooling = [k for k in rotated if _cooldown.get(k[0], 0.0) > now]
        order = non_cooling + cooling
    else:
        order = keys  # 单 key：仅此一把

    for idx, (label, key) in enumerate(order):
        try:
            content, usage = _call(key, base_url, model, system_prompt, user_prompt,
                                   temperature, timeout, max_tokens)
            if multi:
                _served[label] = _served.get(label, 0) + 1
                tally = " ".join(f"{lbl}:{_served.get(lbl, 0)}" for lbl, _ in keys)
                _log(f"key={label} ({tally})" + (" [failover]" if idx > 0 else ""))
            return content, usage
        except Exception as e:
            if not _is_retryable(e):
                raise                      # 400/内容类：立即抛，不切换、不冷却
            has_more = idx < len(order) - 1
            if multi:
                _cooldown[label] = time.time() + COOLDOWN_S
                if has_more:
                    _log(f"key={label} 可重试错误({_errbrief(e)}) → 冷却 {COOLDOWN_S}s，切到下一把 key")
                else:
                    _log(f"key={label} 可重试错误({_errbrief(e)}) → {n} 把 key 全部失败；"
                         f"疑似账号/IP 级或 relay-wide 限流/连接抖动（同 relay 轮换无法缓解），抛出交上游退避")
            if has_more:
                time.sleep(2 + random.random())   # 2-3s 抖动后切换下一把
                continue
            raise                          # 单 key 首次失败 / 多 key 全失败 → 抛出（上游退避处理）


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
