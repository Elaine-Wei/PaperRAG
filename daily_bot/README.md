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
`RELAY_MODEL`（默认 `claude-fable-5`）。

若未提供 key，导读步骤会失败并以 arXiv 原始摘要兜底（HTML 顶部会显示提示条），
不影响整体流程跑通。

## 输出

`output/` 下每篇论文一个 HTML，文件名形如 `YYYY-MM-DD_<arxiv_id>.html`。
样式来自 `academic-html-skill/unpacked/.../full-css.css`，运行时内联进 HTML。

## 已知环境问题

- **系统 Python（Homebrew 3.14）的 `pyexpat` 损坏**：`xml.etree.ElementTree` 无法解析
  XML（链接到不匹配的系统 `libexpat`）。`run.py` 已内置正则兜底解析器，因此本模块不受影响。
  但 `crawler/`（用 `requests` + ElementTree）在同一 Python 下可能同样报错，日后需要
  给它加同样的正则兜底，或改用修好 expat 的 Python（如 `brew reinstall python@3.14`
  或 pyenv 构建）。
