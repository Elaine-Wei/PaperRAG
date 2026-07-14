# PaperRAG

AI / Agent 方向论文的检索系统(RAG),面向量化研究。

## 项目结构

- `crawler/` —— 第一部分:论文采集与数据增强
  - arXiv API 抓取最新论文,入库 PostgreSQL
  - Semantic Scholar / Hugging Face 补充引用数、点赞数等字段
- (规划中)检索层 —— 第二部分:向量检索 + 混合检索 + 重排

详见各子目录的 README。
