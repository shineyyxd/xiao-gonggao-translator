# 小公告翻译官（银发向）

把 A 股上市公司公告翻译成退休老人能看懂的大白话早报：每天开盘前自动筛选重要公告，
改写成"什么事 / 会怎样 / 原文出处"三段大白话，产出大字版 HTML。**只讲事实，不做任何
投资建议；宁可停刊，不可错发。**

## 架构骨架

```
07:29 scheduler 触发
  ↓ Fetcher    巨潮实时抓取（live）/ 本地样本（sample），输入快照落盘 + 完整性对账
  ↓ Editor     三层漏斗（14 赛道 + 重要性打分 + 多样性控制）+ Memory 跨天去重
  ↓ PdfSummary 入选公告抓 PDF 提取原文摘录补摘要（纯抽取，不改写）
  ↓ Writer     公告 → 三段式人话稿（LLM；链接不进上下文；输出契约校验）
  ↓ Compliance 确定性预检：数字原值 / 禁词 / 关键限定 / 无锚点分句（不过直接打回）
  ↓ Checker    对照原文核查（独立 LLM；Memory 错误分布只进这里）
  ↓ Publisher  大字版 HTML + TTS 音频（暂停中）+ 企业微信推送（可选）
  ↓ Memory     本期已报道条目落库，供下期去重
全程 Orchestrator 状态机驱动，audit JSONL + sqlite agent_calls 双写留痕
```

Orchestrator 和 Memory 是确定性代码；LLM 只用于 Writer（生成）和 Checker（校对），
两个子 Agent 独立模型、独立 harness，互不共享上下文。事实只能来自当日公告原文；
公告链接由管线直接注入，不经过任何 LLM 上下文（机制性消除出处错误）。

## 快速开始

```bash
python3 -m venv pipeline/.venv
pipeline/.venv/bin/pip install -r pipeline/requirements.txt
pipeline/.venv/bin/python -m pytest pipeline/tests -q        # 回归集（含真实错误用例）

# 样本重放（无需 API key，mock 模式确定性出稿）
cd pipeline && .venv/bin/python main.py --date 2026-08-26 --mock-llm --source sample

# 真实 LLM 跑样本：在 pipeline/.env 配置下列环境变量后去掉 --mock-llm
#   YFZB_GEN_API_KEY / YFZB_GEN_MODEL            生成模型
#   YFZB_REV_API_KEY / YFZB_REV_MODEL / YFZB_REV_EFFORT   校对模型（独立配置）
# 生产实时抓取：.venv/bin/python main.py --date <日期> --source live
```

详见 `pipeline/README.md`（场景路由、审计留痕、数据完整性对账口径、配置项全表）。

## 目录导览

- `pipeline/` — 工程管线（编排器/抓取/漏斗/双LLM子Agent/合规/审计/渲染），含测试 130+ 项
- `prompts/` — Writer（生成Agent）与 Checker（校对Agent）的 skill 提示词
- `研发数据/` — 公告样本与已验证的人话稿金标准样例
- `产出/` — 各日期产出（人话稿 JSON / 大字版 HTML / 日报 / 审计 JSONL）与老人试用包
- `项目总方案.md` / `第一期测试报告_2026-08-26.md` — 产品方案与首期测试报告（历史文档）
- `人工待办清单.md` — 待办与迭代清单

## 合规声明

个人学习项目。公告数据来源于巨潮资讯网（www.cninfo.com.cn）公开披露信息。
本项目所有产出仅为公告信息的整理与通俗化转述，**不构成任何投资建议**；
信息以上市公司公告原文为准，使用者据此操作风险自担。
