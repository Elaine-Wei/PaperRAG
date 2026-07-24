#!/usr/bin/env python3
"""
selfcheck —— PaperRAG 能力自检（静态/内省，不调 relay、不连 DB）。

一条命令给出真相表：每条流水线（daily run.py / topic run_topic.py）实际具备哪些韧性能力，
免得再从日志里猜。检查项：
  (a) relay 多 key 轮换：relay.py 代码能力(最多 3) + 当前 env 生效把数；畸形响应是否已收敛为可重试。
  (b) checkpoint/resume：deep_study.generate_multipass 是否具备断点续跑逻辑。
  (c) 逐-paper 模型轮换：每条流水线的深读循环是否 thread 了 per-paper 模型 + 开关状态 + 默认模型。

用法：python daily_bot/selfcheck.py
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run            # noqa: E402  导入即加载 .env（无 relay/DB 调用）
import run_topic      # noqa: E402
import deep_study     # noqa: E402
import relay          # noqa: E402


def _yn(b):
    return "✓" if b else "✗"


def _src(fn):
    try:
        return inspect.getsource(fn)
    except Exception:
        return ""


def gather():
    r = {}

    # (a) relay 多 key
    max_keys = 3  # relay.py 读 RELAY_API_KEY / _2 / _3
    try:
        live = relay._keys()
        r["keys_live"] = [lbl for lbl, _ in live]
    except Exception as e:
        r["keys_live"] = []
        r["keys_err"] = str(e)[:50]
    r["keys_max"] = max_keys
    r["malformed_handled"] = hasattr(relay, "RelayResponseError") and \
        ("RelayResponseError" in _src(relay._is_retryable)) and \
        ("RelayResponseError" in _src(relay._call))

    # (b) checkpoint/resume（两条流水线共用 deep_study）
    cp_helpers = all(hasattr(deep_study, h) for h in
                     ("_cp_save", "_cp_load", "_cp_delete", "_fingerprint", "CHECKPOINT_DIR"))
    mp_src = _src(deep_study.generate_multipass)
    r["resume"] = cp_helpers and ("_cp_save" in mp_src) and ("fingerprint" in mp_src)

    # (c) 逐-paper 模型轮换
    daily_src = _src(run.run_study_with_backoff)
    topic_src = _src(run_topic.study_stage)
    r["daily_rotation"] = "paper_model" in daily_src
    r["topic_rotation"] = "paper_model" in topic_src
    r["daily_toggle"] = getattr(run, "STUDY_MODEL_ROTATION", None)
    r["topic_toggle"] = getattr(run_topic, "STUDY_MODEL_ROTATION", None)
    r["daily_pool"] = getattr(run, "MODEL_ROTATION", None)
    r["topic_pool"] = getattr(run_topic, "MODEL_ROTATION", None)
    # 默认深读模型（rotation OFF 时实际所用）
    r["daily_default"] = deep_study.DEFAULT_MODEL           # daily OFF → paper_model=DEFAULT_MODEL
    r["topic_default"] = getattr(run_topic, "STUDY_MODEL", None)  # topic OFF → STUDY_MODEL
    return r


def report():
    r = gather()
    W = 74
    print("=" * W)
    print(" PaperRAG 能力自检 (capability self-check) — 静态检查，不调 relay/DB")
    print("=" * W)

    live_n = len(r["keys_live"])
    print("\n[Relay 多 key 轮换 — 两条流水线共用 relay.py]")
    print(f"  代码能力       : 最多 {r['keys_max']} 把 (RELAY_API_KEY / _2 / _3)")
    print(f"  当前 env 生效  : {live_n} 把 {r['keys_live']}"
          + (f"   (注: {r['keys_err']})" if r.get("keys_err") else ""))
    print(f"  畸形/空响应处理: {_yn(r['malformed_handled'])} "
          "RelayResponseError → 可重试(跨 key 切换)、失败清抛，不再裸 KeyError")

    def toggle(t):
        return "ON" if t is True else ("OFF" if t is False else f"?{t}")

    rows = [
        ("N-key relay rotation", f"{live_n} live / {r['keys_max']} max", f"{live_n} live / {r['keys_max']} max"),
        ("Checkpoint / resume", f"{_yn(r['resume'])} (shared deep_study)", f"{_yn(r['resume'])} (shared deep_study)"),
        ("Per-paper model rotation",
         f"{_yn(r['daily_rotation'])} available toggle={toggle(r['daily_toggle'])}",
         f"{_yn(r['topic_rotation'])} available toggle={toggle(r['topic_toggle'])}"),
        ("  study loop", "run_study_with_backoff", "study_stage"),
        ("Default study model", str(r["daily_default"]), str(r["topic_default"])),
        ("Rotation pool", str(r["daily_pool"]), str(r["topic_pool"])),
    ]
    c0, c1, c2 = 30, 26, 26
    print("\n[功能矩阵]")
    print(f"  {'Feature':<{c0}}| {'daily (run.py)':<{c1}}| {'topic (run_topic.py)':<{c2}}")
    print(f"  {'-'*c0}|{'-'*(c1+1)}|{'-'*(c2+1)}")
    for f, d, t in rows:
        print(f"  {f:<{c0}}| {d:<{c1}}| {t:<{c2}}")

    # daily 就绪检查（对应 green-light 条件）
    daily_ready = {
        "3-key rotation": live_n >= 3,
        "checkpoint/resume": r["resume"],
        "model rotation available": r["daily_rotation"],
        "default study model = sol": r["daily_default"] == "gpt-5.6-sol",
    }
    print("\n[daily 就绪检查 (green-light 条件)]")
    for k, v in daily_ready.items():
        print(f"  {_yn(v)}  {k}")
    print("\n  => daily " + ("READY ✓" if all(daily_ready.values()) else "NOT READY ✗"))
    return r, all(daily_ready.values())


if __name__ == "__main__":
    report()
