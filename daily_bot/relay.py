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

# 必须显式带 UA：relay 前置的 Cloudflare 会以 error 1010 封掉 urllib 默认的
# "Python-urllib/3.x"（403，且不可重试 → 会把整轮评分全判 FAIL）。任意常规 UA 均可放行。
_USER_AGENT = "PaperRAG-daily_bot/1.0"

# ---------------------------------------------------------------------------
# DeepSeek 直连兜底（relay 整体不可用时用）——【逐篇】而非逐调用，见各 runner 的 study/score 循环。
#   ·DS_API_KEY 未设置 → 兜底关闭，全部行为与从前逐字节一致。
#   ·DS_MODEL_TAG 是【伪模型名】：调用方把它当 model 传给 relay_chat，即整条调用改走直连 DS。
#    这样 scorer/deep_study 内部那些 relay.relay_chat(...) 全部自动跟着走 DS，无需逐处改签名；
#    同时它就是 checkpoint 文件名 / 输出 HTML 名 / DB model 列里的标签（与 relay 代理的
#    deepseek-v4-pro 明确区分开：那条路仍然经 relay，这条不经）。
# ---------------------------------------------------------------------------
DS_MODEL_TAG = "ds-direct"
DS_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DS_DEFAULT_MODEL = "deepseek-v4-flash"
DS_DEFAULT_TIMEOUT = 300      # 实测冷启动可达 165s，远超 relay 的 90s 默认；给足余量
# DS V4 是推理模型：reasoning_tokens 与正文【共用】max_tokens 预算（实测 max_tokens=16 时
# 16 个全被 reasoning 吃掉、finish_reason=length）。沿用调用方为 relay 定的小预算会截断正文，
# 对 judge(220)/composite(500) 这类小额度尤其致命 → 统一放大并设下限。
DS_TOKEN_HEADROOM = 3
DS_MIN_MAX_TOKENS = 2048


class RelayResponseError(RuntimeError):
    """响应畸形/空（无 choices[0].message.content）——如某模型当前返回坏包。
    视作可重试：先在多 key 间切换，全部失败再抛，交上游退避/跳过；不再抛裸 KeyError。"""

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


def _ds_key():
    return os.environ.get("DS_API_KEY")


def _ds_base_url():
    return os.environ.get("DS_BASE_URL", DS_DEFAULT_BASE_URL)


def _ds_model():
    return os.environ.get("DS_MODEL", DS_DEFAULT_MODEL)


def ds_enabled():
    """DS 兜底是否已武装（仅看 key 是否存在，不发网络请求；selfcheck 也用它）。"""
    return bool(_ds_key())


def _ds_budget(max_tokens):
    """把为 relay 定的 max_tokens 放大到 DS 推理模型能用的额度（None=不限，原样透传）。"""
    if max_tokens is None:
        return None
    return max(int(max_tokens) * DS_TOKEN_HEADROOM, DS_MIN_MAX_TOKENS)


# ---------------------------------------------------------------------------
# provider 轮换（rolling failover）——哪一家【持续】不行就换另一家，来回滚动，绝不吊死在一家上。
#   ·任一次成功 → 失败计时清零（抖动不算"持续失败"）。
#   ·同一家连续失败 >= PROVIDER_ROLL_AFTER_S（默认 2h）→ 切到另一家，并给新的一家重新计时；
#    新的一家若也连续挂 2h，再切回来。如此往复（rolling），无需人工介入。
#   ·DS 未武装（无 DS_API_KEY），或本次运行未显式 --ds（未 armed）→ 无处可切，留在 relay。
#   ·PROVIDER_ROLL_AFTER_S 可用环境变量覆盖（秒）。
# ---------------------------------------------------------------------------
def _roll_after_s():
    try:
        return float(os.environ.get("PROVIDER_ROLL_AFTER_S", 2 * 3600))
    except (TypeError, ValueError):
        return 2 * 3600


_roll = {"provider": "relay", "fail_since": None, "switches": 0, "armed": False}


def roll_init(provider):
    """设定起始 provider（"ds" | "relay"）。--ds 启动即 roll_init("ds")——armed 跟随 provider：
    只有本次运行显式以 "ds" 初始化过，relay→ds 的自动滚动才被允许（--ds 场景下 ds→relay→ds
    的双向滚动因此完全不受影响）；未调用过（即未 --ds）时 armed 恒 False，relay→ds 的自动
    滚动被禁止——不再需要用户不知情地被自动切到可靠性同样存疑的 DeepSeek。"""
    _roll["provider"] = provider
    _roll["fail_since"] = None
    _roll["armed"] = (provider == "ds")


def roll_current():
    return _roll["provider"]


def roll_status():
    fs = _roll["fail_since"]
    return {"provider": _roll["provider"], "switches": _roll["switches"],
            "failing_for_s": (0 if fs is None else max(0.0, time.time() - fs))}


def roll_model(relay_model):
    """把"这一篇原本要用的 relay 模型"翻译成当前 provider 实际该用的模型名。"""
    return DS_MODEL_TAG if _roll["provider"] == "ds" else relay_model


def roll_record(ok, now=None):
    """记一次调用结果；连续失败够久就切换 provider。返回是否发生了切换。"""
    now = time.time() if now is None else now
    if ok:
        _roll["fail_since"] = None
        return False
    if _roll["fail_since"] is None:
        _roll["fail_since"] = now          # 开始计时
        return False
    if now - _roll["fail_since"] < _roll_after_s():
        return False
    other = "relay" if _roll["provider"] == "ds" else "ds"
    if other == "ds" and not (ds_enabled() and _roll["armed"]):
        return False                        # DS 未武装，或本次运行未显式 --ds → 不自动滚到 ds
    hrs = (now - _roll["fail_since"]) / 3600.0
    _roll["provider"] = other
    _roll["fail_since"] = now               # 新 provider 重新计时（它也可能不行）
    _roll["switches"] += 1
    _log(f"provider 连续失败 {hrs:.1f}h（>= {_roll_after_s()/3600:.1f}h）→ 轮换到 {other}"
         f"（第 {_roll['switches']} 次轮换）")
    return True


def relay_chat_ds(system_prompt, user_prompt, temperature=0.3, timeout=DS_DEFAULT_TIMEOUT,
                  max_tokens=None, model=None):
    """
    直连 DeepSeek（不经 relay、不参与多 key 轮换/冷却）。签名与 relay_chat 一致，返回 (content, usage)。
    DS_API_KEY 未设置 → 抛 RuntimeError，调用方据此认定"兜底不可用"并按原逻辑放弃。
    HTTP/网络/空响应错误照常抛出（形状与 relay_chat 相同），由调用方处理。
    """
    key = _ds_key()
    if not key:
        raise RuntimeError("DS_API_KEY 未设置 → DeepSeek 直连兜底未启用")
    ds_model = model or _ds_model()
    content, usage = _call(key, _ds_base_url(), ds_model, system_prompt, user_prompt,
                           temperature, timeout, _ds_budget(max_tokens))
    _log(f"ds-direct model={ds_model} tokens={(usage or {}).get('total_tokens', '?')}")
    return content, usage


def _is_retryable(exc):
    """503/429、连接级错误（超时/EOF/reset/握手）、以及畸形/空响应 → 触发切换+冷却；400/内容类不触发。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_HTTP
    return isinstance(exc, (RelayResponseError, urllib.error.URLError, TimeoutError, socket.timeout,
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
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    # 防御：某模型可能返回缺 content 的畸形/空包（曾致 relay.py 抛裸 KeyError('content')）。
    # 统一转成可重试的 RelayResponseError → 先跨 key 切换，全失败再抛，交上游退避/跳过。
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not content:
        snippet = json.dumps(payload, ensure_ascii=False)[:200]
        raise RelayResponseError(f"模型 {model} 返回空/畸形响应（无 content）：{snippet}")
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

    model == DS_MODEL_TAG（"ds-direct"）：整条调用改走直连 DeepSeek，不碰 relay key/轮换/冷却。
    """
    global _next
    # DS 兜底分派：放在 _keys() 之前——relay key 缺失/失效时兜底仍须可用。
    if model == DS_MODEL_TAG:
        return relay_chat_ds(system_prompt, user_prompt, temperature=temperature,
                             timeout=max(timeout, DS_DEFAULT_TIMEOUT), max_tokens=max_tokens)
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
