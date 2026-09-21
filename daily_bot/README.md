# daily_bot —— 每日论文导读（v0）

最小可运行闭环：抓取 arXiv 最新论文 → 挑 2 篇 → relay LLM 生成中文导读 → 套用
`academic-html-skill` 的风格生成自包含 HTML → 落地 `output/`。

暂不涉及调度 / 数据库 / 推送 / 复杂筛选。仅用 Python 标准库，无需 pip 安装。

## 运行

```bash
python daily_bot/run.py
```

## 配置

LLM 通过 OpenAI 兼容 relay 访问。运行前需要提供 `RELAY_API_KEY`，两种方式（任选）：

1. **`.env` 文件（推荐）**：把 `.env.example` 复制为 `daily_bot/.env` 并填入 key。
   `run.py` 启动时会自动读取 `daily_bot/.env`（`.env` 已在 `.gitignore` 中，不会提交）。
2. **Shell 环境变量**：`export RELAY_API_KEY=...`。shell 中已导出的值优先于 `.env`。

可选变量（均有默认值）：`RELAY_BASE_URL`（默认 `https://a6.a6api.com/v1`）、
`RELAY_MODEL`（默认 `gpt-5.6-sol`）。

若未提供 key，导读步骤会失败并以 arXiv 原始摘要兜底（HTML 顶部会显示提示条），
不影响整体流程跑通。

## 配置驱动主题榜单

`run_topic.py` 保留现有的 selfevo legacy 配置；新增主题放在
`daily_bot/topics_config.json`，不需要修改 Python 常量。每个主题配置包含
`label`、`description`、`bootstrap_json`、`prefilter_terms`、五维 `weights`，以及
`top_n`、`study_top`、`shortlist_top`。权重字段为 `freshness`、
`reproducibility`（也接受 `repro`）、`novelty`、`domain_relevance`、`authority`，
合计必须为 1。

`bootstrap_json` 指向 `topic_bootstrap.py` 生成的 JSON。加载器会从其中的
`verified_anchors` 自动生成 seeds/anchors，并从 `keyword_nets` 展平关键词查询。
配置主题使用代码确定性的加权 composite；缺失的 authority 等维度会被排除，剩余
权重重新归一化。已有 selfevo 与 factor A/B 榜单继续使用原有路径。

推荐的激活顺序：

```bash
python daily_bot/topic_bootstrap.py --topic "主题名" \
  --describe "主题范围描述" --output daily_bot/output/topic_bootstrap --html-review
编辑 daily_bot/topics_config.json，加入该 bootstrap_json 和主题参数
python daily_bot/run_topic.py --topic topic-key --dry-run
python daily_bot/run_topic.py --topic topic-key --score
python daily_bot/run_topic.py --topic topic-key --study
```

`daily_bot/board_registry.json` 是纯 bookkeeping 索引，记录 legacy selfevo、factor-A、
factor-B 以及后续配置主题的名称、类型、创建日期和描述；它不参与任何榜单运行。

## 输出

`output/` 下每篇论文一个 HTML，文件名形如 `YYYY-MM-DD_<arxiv_id>.html`。
样式来自 `academic-html-skill/unpacked/.../full-css.css`，运行时内联进 HTML。

## 已知环境问题

- **系统 Python（Homebrew 3.14）的 `pyexpat` 损坏**：`xml.etree.ElementTree` 无法解析
  XML（链接到不匹配的系统 `libexpat`）。`run.py` 已内置正则兜底解析器，因此本模块不受影响。
  但 `crawler/`（用 `requests` + ElementTree）在同一 Python 下可能同样报错，日后需要
  给它加同样的正则兜底，或改用修好 expat 的 Python（如 `brew reinstall python@3.14`
  或 pyenv 构建）。
