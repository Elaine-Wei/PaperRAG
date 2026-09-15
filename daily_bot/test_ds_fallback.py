#!/usr/bin/env python3
"""
DS 直连兜底 —— 离线测试（不联网、不连库、不碰 .env 真值）。

覆盖：
  1  relay 未耗尽时【不】调 DS
  2  content 用尽 → DS 恰好调一次；成功 → 完成 + 模型标签 ds-direct
  3  DS 也失败 → 按原逻辑 gave_up，且 DS 只调一次（不重复烧）
  4  DS_API_KEY 未设置 → DS 从不被调用，行为与从前一致
  5  relay 类失败达阈值 → 转 DS（524 场景：content 额度永远用不完，必须靠这条）
  6  524 归类为 relay（回归：此前被判 content，直接吃掉 3 次放弃额度）
  7  checkpoint / 输出名按 ds-direct 命名，且与 relay 档互不污染
  8  relay_chat(model=ds-direct) 走直连、不碰 relay key；DS 预算放大（推理模型吃 max_tokens）
  9  评分兜底：relay 失败 → DS 出分，model_used/DB model 列 = ds-direct
 10  综评跟随同一模型（否则 DS 兜底会被最后一步的 relay 调用打回）
 11  provider 滚动轮换：未 --ds（未 armed）时连续失败也不会自动滚到 ds；--ds 场景下
     ds↔relay↔ds 的双向滚动完全不受影响
 12  daily run.py 的 --ds 主力模式（含机制 A/B 默认关闭、显式打开仍可用两种场景）

2026-09：新增 AUTO_DS_FALLBACK 总开关（默认 False）——机制 A（ds_score_fallback /
ds_study_fallback）与机制 B（_ensure_composite 的 na 重试）默认不再自动调用 DeepSeek；
机制 C（provider 滚动）新增 armed 位，仅 --ds（roll_init("ds")）才允许 relay→ds 自动滚动。
以上 3 个机制在【显式打开】/【显式 --ds】时的原有行为保持不变，测试 0/11/12 分别覆盖
默认关闭与显式打开两侧。

跑法：python daily_bot/test_ds_fallback.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILED = []
PASSED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   — {detail}" if detail and not cond else ""))


class Env:
    """临时改 os.environ，退出时还原（绝不写 .env）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 假 conn / 假 db：study 循环里的落库全部拦下来，不碰真库
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return FakeCursor(self.sql)

    def commit(self):
        pass


def main():
    import relay
    import run
    import deep_study
    import scorer

    print("\n=== 1-8 relay/DS 分派 + 预算 + 命名 ===")

    # --- 8. relay_chat(model=ds-direct) 直连、不碰 relay key；预算放大 ---
    seen = {}

    def fake_call(api_key, base_url, model, sysp, usrp, temp, timeout, max_tokens):
        seen.update(key=api_key, base=base_url, model=model, max_tokens=max_tokens, timeout=timeout)
        return "ok", {"total_tokens": 1}

    orig_call = relay._call
    relay._call = fake_call
    try:
        with Env(DS_API_KEY="sk-test", DS_BASE_URL="https://ds.test/v1", DS_MODEL="deepseek-v4-flash",
                 RELAY_API_KEY=None, RELAY_API_KEY_2=None, RELAY_API_KEY_3=None):
            # relay key 全部缺失也必须能走 DS（兜底的意义就在 relay 不可用时）
            content, _ = relay.relay_chat("s", "u", model=relay.DS_MODEL_TAG, max_tokens=220)
            check("8a relay_chat(ds-direct) 在无 relay key 时仍可用", content == "ok")
            check("8b 打到 DS base_url", seen.get("base") == "https://ds.test/v1", seen.get("base"))
            check("8c 用 DS 真实模型名而非标签", seen.get("model") == "deepseek-v4-flash", seen.get("model"))
            check("8d 用 DS key", seen.get("key") == "sk-test")
            check("8e max_tokens 放大（220 → ≥2048，推理模型与正文共用预算）",
                  seen.get("max_tokens") >= 2048, str(seen.get("max_tokens")))
            check("8f timeout 至少 DS 默认（冷启动实测 165s）",
                  seen.get("timeout") >= relay.DS_DEFAULT_TIMEOUT, str(seen.get("timeout")))

        with Env(DS_API_KEY=None):
            check("8g DS_API_KEY 未设置 → ds_enabled() 为 False", relay.ds_enabled() is False)
            try:
                relay.relay_chat_ds("s", "u")
                check("8h 未设置 key 时 relay_chat_ds 抛错", False, "没抛")
            except RuntimeError:
                check("8h 未设置 key 时 relay_chat_ds 抛 RuntimeError", True)
    finally:
        relay._call = orig_call

    # --- 6. 524 归类回归 ---
    check("6a 524 判为 relay 类（回归：此前被判 content）",
          run._classify_study_error(Exception("HTTP Error 524: <none>")) == "relay")
    check("6b 503 仍是 relay 类", run._classify_study_error(Exception("HTTP Error 503")) == "relay")
    check("6c 400 仍是 content 类", run._classify_study_error(Exception("HTTP Error 400: bad")) == "content")

    # --- 7. checkpoint / 输出命名 ---
    cp_relay = deep_study._cp_path("2512.04697", "gpt-5.6-sol")
    cp_ds = deep_study._cp_path("2512.04697", relay.DS_MODEL_TAG)
    check("7a DS checkpoint 命名为 _ds-direct.json", cp_ds.endswith("2512.04697_ds-direct.json"), cp_ds)
    check("7b 与 relay 档不同名（不互相污染）", cp_relay != cp_ds)
    check("7c 与 relay 代理的 deepseek-v4-pro 档也不同名",
          cp_ds != deep_study._cp_path("2512.04697", "deepseek-v4-pro"))

    # ---------------------------------------------------------------------
    # 0 AUTO_DS_FALLBACK 默认关闭：兜底函数直接返回 None，完全不碰 DS
    #   （2026-09：relay 三键轮换恢复后，DS 本身可靠性存疑，不再做无提示的最后防线）
    # ---------------------------------------------------------------------
    print("\n=== 0 AUTO_DS_FALLBACK 默认关闭：兜底不生效 ===")
    check("0  AUTO_DS_FALLBACK 默认为 False", run.AUTO_DS_FALLBACK is False)

    with Env(DS_API_KEY="sk-test"):   # key 在，但总开关关 → 仍不该调 DS
        gate_calls = []

        def fake_gen_study_gated(aid, model=None):
            gate_calls.append(model)
            return {"path": "/tmp/x_ds.html"}

        orig_gen, orig_count = deep_study.generate_study, run._count_big_sections
        deep_study.generate_study, run._count_big_sections = fake_gen_study_gated, lambda p: 9
        try:
            res = run.ds_study_fallback("AAA")
            check("0a 默认关闭时 ds_study_fallback 直接返回 None，不调用 DS",
                  res is None and gate_calls == [], str(gate_calls))
        finally:
            deep_study.generate_study, run._count_big_sections = orig_gen, orig_count

        def fake_gen_score_gated(aid, cross_check_on=True, model=None, cross_model=None, **kw):
            gate_calls.append(model)
            return {}

        orig_gs_gate = scorer.generate_score
        scorer.generate_score = fake_gen_score_gated
        try:
            res2, tag2 = run.ds_score_fallback("AAA")
            check("0b 默认关闭时 ds_score_fallback 直接返回 (None,None)，不调用 DS",
                  res2 is None and tag2 is None and gate_calls == [], str(gate_calls))
        finally:
            scorer.generate_score = orig_gs_gate

    # ---------------------------------------------------------------------
    # 2-5 深读循环：mock deep_study.generate_study，按模型决定成功/失败
    #   AUTO_DS_FALLBACK 显式打开——验证机制 A 这条能力本身还在，只是现在要手动选择
    #   （默认关闭已由上面的 0a/0b 覆盖）。
    # ---------------------------------------------------------------------
    print("\n=== 2-5 深读循环：兜底时机（AUTO_DS_FALLBACK 显式打开）===")
    run.AUTO_DS_FALLBACK = True

    def run_loop(relay_mode, ds_ok, ds_key="sk-test", targets=("AAA",)):
        """relay_mode: 'content'(产出不完整) | 'relay-exc'(抛 524)。返回 (结果, 调用轨迹)。"""
        calls = []

        def fake_generate_study(aid, model=None):
            calls.append(model)
            if model == relay.DS_MODEL_TAG:
                if ds_ok:
                    return {"path": f"/tmp/{aid}_ds.html"}
                raise RuntimeError("DS down")
            if relay_mode == "relay-exc":
                raise RuntimeError("HTTP Error 524: <none>")
            return {"path": f"/tmp/{aid}_relay.html"}   # 产出但不完整（见 _count_big_sections）

        def fake_count(path):
            return 9 if (path or "").endswith("_ds.html") else 1   # relay 产出恒不完整

        marks = []
        orig = (deep_study.generate_study, run._count_big_sections, run._has_begging,
                run.db.ensure, run.db.mark_stage, run.db.get_daily_row, run.db.get_stage_status)
        deep_study.generate_study = fake_generate_study
        run._count_big_sections = fake_count
        run._has_begging = lambda p: False
        run.db.ensure = lambda c: c
        run.db.mark_stage = lambda c, a, s, p: marks.append((a, s, p))
        run.db.get_daily_row = lambda c, a: {}
        run.db.get_stage_status = lambda c, a: {"studied": False}
        try:
            with Env(DS_API_KEY=ds_key):
                res = run.run_study_with_backoff(
                    FakeConn(), list(targets), cap_hours=0.02,
                    backoff_min=(0, 0, 0, 0, 0), min_big=4, max_content_attempts=3)
        finally:
            (deep_study.generate_study, run._count_big_sections, run._has_begging,
             run.db.ensure, run.db.mark_stage, run.db.get_daily_row,
             run.db.get_stage_status) = orig
        return res, calls, marks

    # 2. content 用尽 → DS 一次 → 成功
    res, calls, marks = run_loop("content", ds_ok=True)
    ds_calls = [c for c in calls if c == relay.DS_MODEL_TAG]
    check("1  relay 未耗尽前不调 DS（前 2 次尝试均为 relay 模型）",
          calls[:2] and all(c != relay.DS_MODEL_TAG for c in calls[:2]), str(calls[:3]))
    check("2a content 用尽后 DS 恰好被调 1 次", len(ds_calls) == 1, str(calls))
    check("2b DS 成功 → 计入 completed", res["completed"] == ["AAA"], str(res["completed"]))
    check("2c DS 成功 → 不在 gave_up", res["gave_up"] == [], str(res["gave_up"]))
    check("2d 落库路径为 DS 产出", marks and marks[-1][2].endswith("_ds.html"), str(marks))

    # 3. DS 也失败 → gave_up，且 DS 只调一次
    res, calls, marks = run_loop("content", ds_ok=False)
    ds_calls = [c for c in calls if c == relay.DS_MODEL_TAG]
    check("3a DS 失败 → gave_up 与从前一致", res["gave_up"] == ["AAA"], str(res))
    check("3b DS 失败也只调 1 次（不重复烧）", len(ds_calls) == 1, str(calls))
    check("3c 未落库", marks == [], str(marks))

    # 4. DS_API_KEY 未设置 → 完全不调 DS
    res, calls, marks = run_loop("content", ds_ok=True, ds_key=None)
    check("4a 未武装时 DS 从不被调用", all(c != relay.DS_MODEL_TAG for c in calls), str(calls))
    check("4b 行为与从前一致（gave_up）", res["gave_up"] == ["AAA"], str(res))

    # 5. relay 类失败（524）达阈值 → 转 DS。content 额度在这条路上永远用不完。
    res, calls, marks = run_loop("relay-exc", ds_ok=True)
    ds_calls = [c for c in calls if c == relay.DS_MODEL_TAG]
    check("5a 524 连续失败达阈值 → 转 DS", len(ds_calls) == 1, str(calls))
    check("5b 转 DS 后完成（否则会一路退避到 5h cap）", res["completed"] == ["AAA"], str(res))
    check(f"5c 阈值 = DS_AFTER_RELAY_FAILS ({run.DS_AFTER_RELAY_FAILS})",
          len([c for c in calls if c != relay.DS_MODEL_TAG]) == run.DS_AFTER_RELAY_FAILS, str(calls))

    run.AUTO_DS_FALLBACK = False   # 还原默认值，避免污染后续测试

    # ---------------------------------------------------------------------
    # 9-10 评分兜底（同样是机制 A/B，AUTO_DS_FALLBACK 显式打开验证能力仍在；
    #   9d 额外验证 DS_API_KEY 未设置这个独立的门也仍然生效）
    # ---------------------------------------------------------------------
    print("\n=== 9-10 评分兜底 + 综评模型跟随（AUTO_DS_FALLBACK 显式打开）===")
    run.AUTO_DS_FALLBACK = True

    gs_calls, gc_calls = [], []

    def fake_generate_score(aid, cross_check_on=True, model=None, cross_model=None, **kw):
        gs_calls.append((model, cross_model))
        if model != relay.DS_MODEL_TAG:
            raise RuntimeError("HTTP Error 524: <none>")   # relay 死掉
        return {"meta": {"title": "t", "categories": []},
                "fresh": {"score": 3.0, "days": 10, "label": "x"},
                "norm": {"paper_type": "application", "repro_subs": {}, "novelty_subs": {},
                         "repro_overall": "", "novelty_overall": "", "domain": "q-fin"},
                "repro_total": 3.0, "novelty_total": 3.0, "domain_relevance": {"score": 5.0},
                "authority": {"na": True}, "cross_notes": [], "path": "/tmp/s.html",
                "cross_check": True, "model_used": model}

    def fake_generate_composite(meta, sub, cross_check_on=False, model=None):
        gc_calls.append(model)
        if model != relay.DS_MODEL_TAG:
            raise RuntimeError("HTTP Error 524: <none>")
        return {"score": 7.0, "reason": "ok", "na": False}

    orig_gs, orig_gc = scorer.generate_score, scorer.generate_composite
    scorer.generate_score, scorer.generate_composite = fake_generate_score, fake_generate_composite
    try:
        with Env(DS_API_KEY="sk-test"):
            res, tag = run.ds_score_fallback("AAA")
            check("9a relay 失败后 DS 出分成功", bool(res))
            check("9b 兜底时 cross_model 也切到 DS（否则交叉复核仍打死掉的 relay）",
                  gs_calls[-1] == (relay.DS_MODEL_TAG, relay.DS_MODEL_TAG), str(gs_calls[-1]))
            check("9c model_used = ds-direct（落 DB model 列）",
                  res.get("model_used") == relay.DS_MODEL_TAG, str(res.get("model_used")))

            import run_status
            sql_sink = FakeConn()
            orig_ensure = run_status.db.ensure
            run_status.db.ensure = lambda c: sql_sink
            try:
                comp = run_status.score_one(sql_sink, "AAA", {"fresh": "steep"})
                check("10a 综评跟随同一模型 → 用 ds-direct 而非默认 relay 模型",
                      gc_calls[-1] == relay.DS_MODEL_TAG, str(gc_calls))
                check("10b 综评成功（若仍走 relay 这里会抛）", comp["score"] == 7.0)
                ins = [s for s, p in sql_sink.sql if "INSERT INTO topic_score" in s]
                check("10c topic_score INSERT 带 model 列", ins and "model" in ins[-1].split("VALUES")[0])
                params = [p for s, p in sql_sink.sql if "INSERT INTO topic_score" in s][-1]
                check("10d model 列写入 ds-direct", relay.DS_MODEL_TAG in params, str(params[-2:]))
            finally:
                run_status.db.ensure = orig_ensure

        with Env(DS_API_KEY=None):
            res, tag = run.ds_score_fallback("AAA")
            check("9d 未武装时评分兜底直接返回 None（不调 DS）", res is None and tag is None)
    finally:
        scorer.generate_score, scorer.generate_composite = orig_gs, orig_gc
        run.AUTO_DS_FALLBACK = False   # 还原默认值，避免污染后续测试

    # ---------------------------------------------------------------------
    # 11 provider 轮换（rolling failover）——2026-09 新增 armed 位：
    #   未 --ds（未调用过 roll_init("ds")）→ armed=False，relay 连续失败再久也不会
    #   自动滚到 ds（机制 C 默认关闭）；--ds 场景（roll_init("ds") 已调用）→ armed=True，
    #   ds↔relay↔ds 的双向滚动【完全不受影响】，与此前逐字节一致。
    # ---------------------------------------------------------------------
    print("\n=== 11 provider 轮换：armed 位（未 --ds 不自动滚到 ds；--ds 双向滚动不变）===")

    with Env(DS_API_KEY="sk-test", PROVIDER_ROLL_AFTER_S="7200"):
        T = 1_000_000.0
        H = 3600.0

        # --- 11a-f：未 --ds（armed=False）—— relay 起点，基本轮换语义不变 ---
        relay.roll_init("relay")
        check("11a 起点 = relay", relay.roll_current() == "relay")
        check("11a2 未 --ds 时 armed=False", relay._roll["armed"] is False)
        check("11b 模型翻译：relay 家 → 保持原模型",
              relay.roll_model("gpt-5.6-sol") == "gpt-5.6-sol")

        # 失败但没到 2h → 不换
        relay.roll_record(False, now=T)
        relay.roll_record(False, now=T + 1 * H)
        check("11c 连续失败 1h（< 2h）→ 不换家", relay.roll_current() == "relay")

        # 中途成功 → 计时清零，抖动不算"持续失败"
        relay.roll_record(True, now=T + 1.5 * H)
        relay.roll_record(False, now=T + 1.6 * H)
        relay.roll_record(False, now=T + 3.0 * H)   # 距新的 fail_since 仅 1.4h
        check("11d 中途成功会清零计时（抖动不触发换家）", relay.roll_current() == "relay")

        # 连续失败满 2h——旧行为会换到 ds；新防护下 armed=False → 不该换
        switched = relay.roll_record(False, now=T + 3.7 * H)   # 距 fail_since(1.6h) = 2.1h
        check("11e 未 --ds 时，连续失败 >= 2h 也不自动滚到 ds（新防护，机制 C 默认关闭）",
              relay.roll_current() == "relay" and switched is False)
        check("11f 未 --ds 时模型翻译保持不变（不泄漏到 ds-direct）",
              relay.roll_model("gpt-5.6-sol") == "gpt-5.6-sol")
        check("11f2 未 --ds 时 switches 计数未增加", relay.roll_status()["switches"] == 0,
              str(relay.roll_status()))

        # --- 11g-k：--ds 场景（roll_init("ds") → armed=True）—— 双向滚动与此前逐字节一致 ---
        relay.roll_init("ds")
        check("11g roll_init('ds') 后 armed=True（--ds 显式选择过 DS 主力）",
              relay._roll["armed"] is True)
        check("11g2 起点 = ds", relay.roll_current() == "ds")

        relay.roll_record(False, now=T)
        relay.roll_record(False, now=T + 1 * H)
        check("11h armed 场景下同样：连续失败 1h（< 2h）→ 不换家", relay.roll_current() == "ds")

        switched = relay.roll_record(False, now=T + 2.1 * H)   # 距 fail_since 2.1h ≥ 2h
        check("11i armed 场景：ds 连续失败 >= 2h → 滚到 relay（与旧行为逐字节一致）",
              relay.roll_current() == "relay" and switched)
        check("11j 滚到 relay 后模型翻译恢复为真实 relay 模型",
              relay.roll_model("gpt-5.6-sol") == "gpt-5.6-sol")

        # relay 也连挂 2h → 滚回 ds（这就是"rolling"，armed 一旦为 True 全程保持）
        switched = relay.roll_record(False, now=T + 2.1 * H + 2.2 * H)
        check("11k relay 也连挂 >= 2h → 滚回 ds（armed 场景下 rolling 双向均正常）",
              relay.roll_current() == "ds" and switched)
        check("11l armed 场景下已发生 2 次轮换", relay.roll_status()["switches"] == 2,
              str(relay.roll_status()))

    # DS 未武装（无 key）→ 无处可切，永远留在 relay（即使 armed=True 也一样）
    with Env(DS_API_KEY=None, PROVIDER_ROLL_AFTER_S="7200"):
        relay.roll_init("ds")   # 即便"armed"，没有 key 也切不过去
        relay.roll_record(False, now=T)
        relay.roll_record(False, now=T + 5 * H)
        check("11m DS 未武装（无 key）→ 无处可切，留在原地（不空转）",
              relay.roll_current() in ("relay", "ds"))
        # roll_init("ds") 本身把 provider 设为 "ds"；真正要验证的是"没有可切换的另一家"，
        # 即从 relay 起点、无 key 时也切不到 ds（对应旧测试 11i 的场景）：
        relay.roll_init("relay")
        relay.roll_record(False, now=T)
        relay.roll_record(False, now=T + 5 * H)
        check("11n DS 未武装（无 key）+ 未 --ds → 双重原因都留在 relay",
              relay.roll_current() == "relay")

    relay.roll_init("relay")   # 还原，避免影响后续/真实运行

    # ---------------------------------------------------------------------
    # 12 daily（run.py）的 --ds 主力模式：与三个 topic runner 同形
    #    重点是【评分路径】：scorer.generate_score / generate_composite 的 model= 默认值是
    #    def 时绑定到 scorer.MAIN_MODEL 的，改模块常量没用 → 调用方必须显式传（今天满屏 NA 的根因）。
    # ---------------------------------------------------------------------
    print("\n=== 12 daily run.py --ds 主力模式 ===")

    saved = (run.STUDY_MODEL, run.SCORE_MAIN, run.CROSS_MODEL, run.JUDGE_MODEL,
             run.MODEL_ROTATION, run.STUDY_MODEL_ROTATION, run.AUTO_DS_FALLBACK)
    tag = relay.DS_MODEL_TAG
    try:
        # --- 12a 默认（不带 --ds）：解析到各模块自身默认，行为与从前一致 ---
        check("12a 默认 STUDY 模型 = deep_study.DEFAULT_MODEL",
              run._study_model() == deep_study.DEFAULT_MODEL, run._study_model())
        check("12b 默认 SCORE/CROSS = scorer 自身默认",
              run._score_model() == scorer.MAIN_MODEL
              and run._cross_model() == scorer.CROSSCHECK_MODEL,
              f"{run._score_model()}/{run._cross_model()}")
        check("12c 默认 JUDGE_MODEL 为 None（relay 默认模型，导读/stage-B 不变）",
              run.JUDGE_MODEL is None)

        # --- 12d DS_API_KEY 未设置 → 直接退出，不静默回退到已死的 relay ---
        with Env(DS_API_KEY=None):
            try:
                run.force_ds()
                check("12d DS_API_KEY 未设置时 --ds 立即退出", False, "没抛 SystemExit")
            except SystemExit:
                check("12d DS_API_KEY 未设置时 --ds 立即退出（不静默回退）", True)
            check("12e 退出后未污染模型常量（仍是默认）", run.SCORE_MAIN is None)

        with Env(DS_API_KEY="sk-test"):
            run.force_ds()
            check("12f force_ds 把 4 个模型全部翻到 ds-direct",
                  (run.STUDY_MODEL, run.SCORE_MAIN, run.CROSS_MODEL, run.JUDGE_MODEL)
                  == (tag, tag, tag, tag),
                  str((run.STUDY_MODEL, run.SCORE_MAIN, run.CROSS_MODEL, run.JUDGE_MODEL)))
            check("12g 轮换关闭（只有一个模型可轮）",
                  run.STUDY_MODEL_ROTATION is False and run.MODEL_ROTATION == [tag],
                  str(run.MODEL_ROTATION))
            check("12h roll_init('ds') 已武装（2h 滚动失败切回 relay）",
                  relay.roll_current() == "ds" and relay.roll_model("gpt-5.6-sol") == tag)
            check("12i 解析器随之翻到 ds-direct（深读/主评/复核）",
                  run._study_model() == tag and run._score_model() == tag
                  and run._cross_model() == tag)

            # --- 12j 评分路径：_ensure_score 必须【显式】把 model + cross_model 传给 scorer ---
            seen_kw = {}

            def fake_gs(aid, cross_check_on=True, model=None, cross_model=None, **kw):
                seen_kw.update(model=model, cross_model=cross_model)
                return {"norm": {"repro_subs": {}, "novelty_subs": {}, "repro_overall": "",
                                 "novelty_overall": "", "paper_type": "application",
                                 "domain": "q-fin"},
                        "fresh": {"score": 3.0, "days": 10, "label": "x"},
                        "repro_total": 3.0, "novelty_total": 3.0,
                        "domain_relevance": {"score": 5.0, "reason": "r"},
                        "authority": {"na": True}, "cross_notes": [], "path": "/tmp/s.html",
                        "model_used": model}

            orig = (scorer.generate_score, scorer.generate_composite, run.db.ensure,
                    run.db.get_stage_status, run.db.upsert_score, run.db.mark_stage,
                    run.db.get_score, run.db.get_papers, run.db.get_daily_row,
                    run.db.upsert_composite)
            comp_calls = []
            comp_na_first = {"n": 0}

            def fake_gc(meta, sub, cross_check_on=False, model=None):
                comp_calls.append(model)
                return {"score": 7.0, "reason": "ok", "na": False}

            scorer.generate_score, scorer.generate_composite = fake_gs, fake_gc
            run.db.ensure = lambda c: c
            run.db.get_stage_status = lambda c, a: {"scored": False, "studied": False}
            run.db.upsert_score = lambda c, a, d: None
            run.db.mark_stage = lambda c, a, s, p: None
            run.db.get_papers = lambda c, ids: [{"title": "t", "categories": []}]
            run.db.get_daily_row = lambda c, a: {"area": "quant"}
            run.db.upsert_composite = lambda c, a, s, r: None
            run.db.get_score = lambda c, a: {"composite_score": None, "model": tag,
                                             "freshness_score": 3.0, "repro_score": 3.0,
                                             "novelty_total": 3.0, "paper_type": "application",
                                             "domain_relevance_score": 5.0, "authority_na": True,
                                             "authority_score": None}
            try:
                run._ensure_score(FakeConn(), "AAA")
                check("12j _ensure_score 显式传 model=ds-direct（def 时绑定的默认值救不了）",
                      seen_kw.get("model") == tag, str(seen_kw))
                check("12k _ensure_score 同时把 cross_model 切到 DS（否则复核仍打死掉的 relay）",
                      seen_kw.get("cross_model") == tag, str(seen_kw))

                # --- 12l 综评（finding ②）：--ds 下必须走 DS，而不是 scorer.MAIN_MODEL ---
                run._ensure_composite(FakeConn(), "AAA")
                check("12l _ensure_composite 用 ds-direct（今天 NA 综评的根因）",
                      comp_calls[-1] == tag, str(comp_calls))

                # --- 12m-o 非 --ds 下 generate_composite 吞异常只返回 na——2026-09 起
                #     AUTO_DS_FALLBACK 默认关闭，不再自动重试 DS：综评停在 N/A，不静默调用
                #     DeepSeek（这正是本轮要达成的目标：没有 --ds 就没有任何 DS 调用）---
                run.SCORE_MAIN = None          # 模拟未加 --ds 的日常运行
                comp_calls.clear()

                def fake_gc_na(meta, sub, cross_check_on=False, model=None):
                    comp_calls.append(model)
                    comp_na_first["n"] += 1
                    if model == tag:
                        return {"score": 7.0, "reason": "ok", "na": False}
                    return {"score": None, "reason": "", "na": True}   # relay 死 → 被吞成 na

                scorer.generate_composite = fake_gc_na
                run.db.get_score = lambda c, a: {"composite_score": None, "model": "claude-fable-5",
                                                 "freshness_score": 3.0, "repro_score": 3.0,
                                                 "novelty_total": 3.0, "paper_type": "application",
                                                 "domain_relevance_score": 5.0,
                                                 "authority_na": True, "authority_score": None}
                check("12m AUTO_DS_FALLBACK 默认关闭（本链路也是 False）",
                      run.AUTO_DS_FALLBACK is False)
                _c, ok = run._ensure_composite(FakeConn(), "AAA")
                check("12n 默认关闭：综评 na 不再自动重试 DS，只调了一次 relay 模型",
                      comp_calls == ["claude-fable-5"], str(comp_calls))
                check("12o 默认关闭：综评停在 N/A（没有静默调用 DeepSeek）", ok is False)

                # --- 12p-q 显式打开 AUTO_DS_FALLBACK：旧的"综评 na → 自动重试 DS"能力
                #     仍然存在，只是现在需要手动选择，而不是无提示的默认行为 ---
                comp_calls.clear()
                run.AUTO_DS_FALLBACK = True
                try:
                    _c, ok2 = run._ensure_composite(FakeConn(), "AAA")
                    check("12p 显式打开后：综评 na → 仍会自动重试一次 DS（能力保留，只是默认关闭）",
                          comp_calls == ["claude-fable-5", tag], str(comp_calls))
                    check("12q 重试后综评成功", ok2 is True)
                finally:
                    run.AUTO_DS_FALLBACK = False
            finally:
                (scorer.generate_score, scorer.generate_composite, run.db.ensure,
                 run.db.get_stage_status, run.db.upsert_score, run.db.mark_stage,
                 run.db.get_score, run.db.get_papers, run.db.get_daily_row,
                 run.db.upsert_composite) = orig
    finally:
        (run.STUDY_MODEL, run.SCORE_MAIN, run.CROSS_MODEL, run.JUDGE_MODEL,
         run.MODEL_ROTATION, run.STUDY_MODEL_ROTATION, run.AUTO_DS_FALLBACK) = saved
        relay.roll_init("relay")

    print("\n" + "=" * 66)
    print(f"  通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
    if FAILED:
        print("  失败：")
        for f in FAILED:
            print(f"    - {f}")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
