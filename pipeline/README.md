# 小公告翻译官（银发向）· 工程化管线（pipeline/）

把 A 股上市公司公告自动筛选、改写成 60-75 岁退休老人能听懂的大白话早报，
每天开盘前产出音频 + 大字文字稿。**只讲事实，不做任何投资建议。宁可停刊，不可错发。**

架构：**多 Agent + 确定性编排 + 全链路审计**。
Orchestrator 和 Memory 是确定性代码（不用 LLM）；LLM 只用于 Writer（生成）
和 Checker（校对）；事实只能来自当日公告原文；链接不进任何 LLM 上下文。

## 架构与角色分工

```
07:29 scheduler 触发
  ↓ Fetcher    巨潮实时抓取（live）/ 本地样本（sample），输入快照落盘 + 完整性对账
  ↓ Editor     三层漏斗 + Memory 跨天去重
  ↓ PdfSummary 入选公告抓 PDF 提取原文摘录补 info_summary（live；失败留空降级）
  ↓ Writer     公告 → 人话稿（LLM，链接不进上下文；输出契约校验）
  ↓ Compliance 确定性预检：数字原值 / 禁词 / 关键限定 / 无锚点分句（不过直接打回）
  ↓ Checker    对照原文核查（独立 LLM；Memory 错误分布只进这里）
  ↓ Publisher  大字版 HTML + TTS 音频 + 企业微信推送（失败重试→落盘兜底）
  ↓ Memory     本期已报道条目落库，供下期去重
全程 Orchestrator 状态机驱动，audit JSONL + sqlite agent_calls 双写留痕
```

| 角色 | 实现 | 是否 LLM | 职责 |
|---|---|---|---|
| Orchestrator | orchestrator.py | 否 | 确定性状态机、场景路由、审计 |
| Fetcher | fetcher.py | 否 | 巨潮抓取、重试、缓存降级、输入快照 |
| Editor | funnel.py | 否 | 板块分类/重要性打分/多样性控制 |
| Memory | memory.py | 否 | 跨天去重、历史错误分布、已报道库 |
| Writer | generator.py | **是** | 三段式人话稿生成（契约受控） |
| Compliance | compliance.py + precheck.py | 否 | 禁词、数字原值、关键限定、无锚点分句 |
| Checker | reviewer.py | **是**（独立配置） | 通过/打回 + 错误清单 |
| Publisher | render.py + tts.py + push.py | 否 | HTML/音频/推送/告警兜底 |

## 快速开始

```bash
python3 -m venv pipeline/.venv
pipeline/.venv/bin/pip install -r pipeline/requirements.txt
pipeline/.venv/bin/python -m pytest pipeline/tests -v          # 含回归集
cd pipeline && .venv/bin/python main.py --date 2026-08-26 --mock-llm --source sample
```

## 数据源：live / sample

- `--source sample`：本地 `研发数据/` 样本（回归、重放、开发用）。
- `--source live`：巨潮资讯网实时抓取（生产用；scheduler 默认走 live）。
  实测：column=sse/szse 返回相同的全市场合并数据（两路互为冗余，主路失败才走备路）；
  pageSize 服务端上限 30，分页抓取；**巨潮没有官方摘要**，`info_summary` 由
  **PDF 摘要抓取**补齐（见下节）。
- **PDF 摘要**（`pdf_summary.py`，live 默认开、sample 默认关，`--pdf-summary/--no-pdf-summary`）：
  漏斗之后、生成之前，只对入选的 6~8 条下载 PDF（间隔 ≥1s、超时 20s、重试 2 次），
  提取前 1~2 页文本做**句子级纯抽取**（含数字的句子优先，≤500 字）写入 `info_summary`。
  合规边界：摘要只能是 PDF 原文的整句摘录，不改一字——改写是 Writer 的事。
  单条失败/pypdf 未安装 → 该条留空降级为标题级输入，不阻断整期；逐条记
  agent_calls（agent=pdf_summary，输出=摘要字数，OK/FAIL）。
- 抓取降级链：主路（3 次指数退避重试）→ 备路（同重试）→ `pipeline/data/cache/`
  最近成功快照（可能是旧日期的，窗口过滤后选题偏少属预期降级）→ 全失败抛
  `FetchFailed` 走停刊路径。

## 数据完整性对账（live）

- 口径（2026-08-25/26/28 三日实测）：主路首页 `totalAnnouncement` 为声称总数
  （如 08-28 的 6538，当日自洽：`totalpages`217 ≈ 6538/30）；备路 pageSize=1
  轻量查询同字段互验——注意两路是**同一份合并数据**，互验只能确认确定性，
  不是独立来源。
- **平台分页硬上限**：pageNum ≤ 100（pageSize=30 时可及 3000 条），第 101 页起
  重绕第 1 页内容、hasMore 恒 true。声称总数超出 3000 的部分**该接口拿不到**
  （超出的是更旧的公告；对"当日早报"场景，接口按时间倒序返回，可及范围即最新
  3000 条，覆盖当日新增足够）——日报如实注明，不装作抓全了。
- 覆盖率 = 实际唯一 announcementId 数 / min（声称总数， 3000 可及上限）。
  **<90% → 日报"数据完整性"行标 ⚠️WARNING，并记 error_log（stage=fetcher，
  error_type=完整性缺口）**；接口不给总数或两路不一致 → 日报如实写
  UNVERIFIABLE 及原因，不编造覆盖率。
- 抓取停止规则：抓到 min(totalpages, 100) 即停（不请求绕圈页），"连续 2 页无新 id"
  作保险；中途短页/单页重复不停车（旧逻辑"整页无新 id 即停"曾在 ~101 页提前
  停车，0827 缓存唯一 2390 条；且"短页即停"会被中途短页误伤，均已修正）。

## 板块分类（细分赛道）

判定优先级：银行（带"银行账户/银行贷款"等误判排除，最高）→ 公用事业 → 宏观
→ 证券/保险（**仅匹配简称**，公告标题里的"XX证券 关于…"是中介机构语境）
→ 细分赛道（匹配简称+标题，按表序先匹配先赢）→ 其他。

- 银行/公用事业/宏观：匹配 简称+标题+摘要（历史行为）。
- 细分赛道：白酒、半导体、光模块（光通信）、光伏、新能源（锂电/储能/风电）、
  航天（卫星/火箭）、医药、有色金属（含黄金）、汽车、地产
  ——**只匹配简称+标题**，不用摘要（避免摘要里业务内容词误伤，且与 live 模式
  没有摘要的行为一致）。
- 加成规则：**新赛道一律不加成**，银行 +2 保留（产品决策）；所有赛道与"其他"
  共用 每板块≤3 / 每事件类型≤2 / 最低分≥4 的多样性控制。
- 期货不在点名赛道内（永安期货等落"其他"，保持 0826 期回归口径）。

**已知局限（诚实标注）**：live 模式没有摘要，板块分类只能靠简称+标题；公司名
不含行业词时仍会落入"其他"（如 中际旭创 是光模块、福莱特 是光伏玻璃，名字
都不带）——宁漏不误。已实测确认巨潮公告查询接口与证券列表接口均**无行业字段**
可用（公告对象 23 个字段逐个核对，columnId/announcementType 是公告分类而非
公司行业），故无法从数据源补强。其余刻意不收的词见 funnel.py 注释
（如"光"字太宽泛、"电池"不等于锂电、"金银/珠宝"不等于有色采选、"生物"是宽词
会命中兽药/生物识别类公司——可接受）。

## 场景路由表（状态机，写死的路径）

| 场景 | 路径 | 退出码 |
|---|---|---|
| FetchFailed（含缓存降级失败） | 当日停刊：runs.status=STOPPED，日报写明原因，webhook 告警（未配置则落盘 `停刊告警.txt`） | 2 |
| 漏斗后 0 条选题 | EMPTY_DAY 简版：简版大字版+日报，**不发音频** | 0 |
| 单条打回超 2 次 | 跳过该条记入错误日志，其余照发（宁缺毋滥） | 0 |
| 全部条失败 | 停刊+告警（宁可停刊不可错发） | 2 |
| 推送失败 | 重试 3 次（5s/15s/60s）→ 落盘 `产出/<date>/待人工推送.txt` | 0 |
| PDF 摘要失败（单条） | 该条 info_summary 留空降级为标题级输入，记 agent_calls（FAIL） | 0 |
| 完整性覆盖率 <90% | 日报"数据完整性"行标 ⚠️WARNING + error_log（完整性缺口） | 0 |
| TTS 失败 | 不阻断发布，记 agent_calls（conclusion=DEGRADED） | 0 |

## 审计"四留痕"

1. **输入快照**：`产出/<date>/raw_announcements.json`（原始公告原样落盘，可重放复现）
2. **输出原文**：`人话稿_<date>.json` / `大字版_<date>.html` / `audio/*.mp3`
3. **判定链路**：`产出/<date>/audit_<run_id>.jsonl`（每个 Agent 每步一行：输入/输出摘要、结论、耗时、重试）
4. **调用元数据**：sqlite `agent_calls` 表（run_id, agent, item_id, 输入摘要+hash, 输出摘要, 结论, 耗时, 重试次数）——Writer/Checker 每次 LLM 调用（含重试）都落表

另：`runs` 表含 status（OK/EMPTY_DAY/STOPPED）、source、**prompt_version**
（`prompts/生成Agent.md`、`校对Agent.md` 内容 sha256 前 8 位）——改 prompt 后可追溯每期用的哪版。

## Memory 铁律

**Memory 的输出绝不进 Writer 的上下文。** 它只影响"选什么"（Editor 跨天去重）
和"查什么"（Checker 重点核查历史高频错误类型），不影响"说什么"
（Writer 的事实只能来自当日公告原文）。有单测 `test_Memory不进Writer上下文` 守着。

- 跨天去重：近 7 天（严格早于当日，保证同日重放幂等）同 (公司, 事件类型)
  已报道的剔除；**重大风险类豁免**——退市风险提示需要连续提醒（公司自己都发
  第六次、第七次），漏报风险大于重复打扰。
- 已报道库只记录**实际发出**的条目（生成失败被跳过的不算"已报道"）。

## 重放注意事项

- 重放 0826：`main.py --date 2026-08-26 --mock-llm --source sample`
  必须仍产出与测试报告一致的 6 条选题（有测试 `test_0826重放一致且同日幂等`）。
- 跨天去重严格早于当日，同日重放天然幂等；若要重放"当天已有记录"的历史日期，
  先清库（`sqlite3 pipeline/data/error_log.db "DELETE FROM reported_items"`）
  或加 `--ignore-memory`，测试则用独立临时库。

## 回归集（改 prompt / 改规则必跑）

`tests/test_regression.py` + `tests/regression_cases.json`：第一期测试报告 v1 的
8 处真实错误做成永久回归用例——4 处链接错误（断言链接由管线注入且与源一致、
不进 Writer 上下文）、2 处"归母净利润→净利润"（断言 precheck 关键限定检查能
发现简化）、2 处无中生有/额外判断（断言"无锚点连续长句"启发式能标记进 Checker
重点关注清单）。3 个漏斗规则缺陷的专测在 `tests/test_funnel.py`。

## Writer/Checker 输出契约

- Writer 输出必须严格 `{"line1","line2","line3"}`、line1 ≤50 字（铁律 40，宽限 50）、
  无禁词；line1/line2/line3 不得自带合规话术（"不构成投资建议"由管线在期首/期尾
  统一注入，k3 会自行加这句话，契约机制性拦截）；格式不符 = CONTRACT_FAIL，
  视同一次失败重试，不进 precheck/校对。
- 预检数字闸放行规范允许的换算派生数（股→万股、每10股→每股、1000股到手算例，
  与原文数字成 10 的整数倍关系即放行），换算正确性由 Checker 验算；
  Checker 提示词内含对应的"白名单"节（词表固定说法/到手算例/安心句/公司介绍），
  防止把规范允许的改写法误判为 B 类无中生有。
- Checker 输出必须能解析出"结论：通过/打回"；解析失败按打回处理（宁可错杀），
  记 PARSE_FAIL。
- 打回重试 ≤2 次，仍不过跳过该条（记入错误日志与 agent_calls）。

## 模型按角色配型（当前配置 2026-08-28）

**Writer 与 Checker 是两个独立子 Agent**：各自持有独立 LLMClient 实例、独立模型、
独立 harness 约束（契约校验/解析兜底/重试），互不共享上下文。

当前生产配置（`.env`）：

- **Writer（生成）= kimi-k2.6**：写作型模型，轻推理改写任务；
- **Checker（校对）= kimi-k3 + `reasoning_effort=max`**：挑错是重推理任务，
  用最强思考档保准确率——effort 是 Harness 对思考链的显式预算约束。
- Kimi 平台实测约束（llm_client 已吸收）：
  - kimi-k2.6 / kimi-k3 **只接受 temperature=1**，其他值 400；
    温度做成可配（`YFZB_GEN/REV_TEMPERATURE`），不配则不传该字段。
  - 免费档**组织级 RPM=3**：客户端内置全局限速（默认间隔 21s，
    `YFZB_MIN_CALL_INTERVAL` 可调）+ 429 自动等待重试 ≤5 次。
  - 推理模型 `max_tokens` 必须给足（默认 8000），否则思考耗尽 content 为空；
    `finish_reason=length` 显式报错交由上层重试。

历史实测（DeepSeek v4，2026-08-27）：两个档位都是推理模型，同一琐碎提示词
flash 推理占比 67.7%、pro 84.8%——轻任务用轻模型、重任务用重模型的配型原则
即由此确立；因 DeepSeek 余额耗尽（402），2026-08-28 切换至 Kimi。

## token 用量计量与成本

- 每次真实 LLM 调用的 usage（prompt/completion/reasoning tokens）随审计链路落入
  `agent_calls` 表（老库自动 ALTER TABLE 迁移，向后兼容）；mock 调用不计入。
- 日报新增"当日 API 用量"节：Writer/Checker 分模型的调用次数与三类 token 合计。
- **成本估算不内置价格**：在 `.env` 填四个单价（元/百万 token，去 DeepSeek 平台
  后台查实时价格后填，不要凭记忆编）后日报才显示估算成本：
  `YFZB_PRICE_GEN_INPUT_PER_MTOK` / `YFZB_PRICE_GEN_OUTPUT_PER_MTOK` /
  `YFZB_PRICE_REV_INPUT_PER_MTOK` / `YFZB_PRICE_REV_OUTPUT_PER_MTOK`。
  reasoning tokens 含在 completion 内，按输出价计，不重复计费。

## CLI

```bash
python main.py --date 2026-08-27 --source live              # 生产：实时抓取
python main.py --date 2026-08-26 --source sample --mock-llm # 样本重放
python main.py --date 2026-08-27 --source live --no-tts     # 跳过音频
python main.py --date 2026-08-27 --source live --push       # 触发微信推送
python main.py --date 2026-08-27 --source live --no-pdf-summary  # 关闭 PDF 摘要抓取
python main.py --date 2026-08-26 --source sample --ignore-memory  # 重放时跳过跨天去重
python scheduler.py                    # 常驻，每天 07:29 触发（默认 --source live）
```

退出码：0 = 正常/EMPTY_DAY；2 = 停刊（STOPPED）。无 API key 时自动落 mock
（`--mock-llm` 可强制）。数据窗口：`--window-days 5`（当日及前 4 个自然日，
不取晚于当日的数据；样本实际覆盖 2026-08-25~08-26）。

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `YFZB_GEN_API_KEY` / `YFZB_GEN_BASE_URL` / `YFZB_GEN_MODEL` | 生成模型 | 空 key 自动 mock / `https://api.moonshot.cn/v1` / `kimi-k2-0905-preview` |
| `YFZB_REV_API_KEY` / `YFZB_REV_BASE_URL` / `YFZB_REV_MODEL` | 校对模型（独立配置，避免"自己批自己作业"） | 缺省回退 GEN |
| `YFZB_GEN_EFFORT` / `YFZB_REV_EFFORT` | 思考档位约束（kimi-k3 支持 low/high/max） | 空=不传 |
| `YFZB_GEN_TEMPERATURE` / `YFZB_REV_TEMPERATURE` | 温度（kimi-k2.6/k3 只接受 1，必须显式配） | 空=不传该字段 |
| `YFZB_MIN_CALL_INTERVAL` | 全局限速间隔秒数（Kimi 免费档组织级 RPM=3） | 21 |
| `YFZB_WECOM_WEBHOOK` | 企业微信群机器人 webhook（推送+停刊告警共用） | 空则跳过/仅落盘 |
| `YFZB_PRICE_GEN/REV_INPUT/OUTPUT_PER_MTOK` | API 单价（元/百万 token，用于日报成本估算） | 空=只显示 token 数 |

密钥放 `pipeline/.env`（本地文件，勿外发；config.py 启动时自动加载，已 export 的
环境变量优先）。

## 目录结构

```
pipeline/
├── orchestrator.py      ← 确定性状态机 + 场景路由 + 审计（核心）
├── fetcher.py           ← 巨潮实时抓取（重试/缓存降级/绕圈检测/完整性对账/FetchFailed）
├── pdf_summary.py       ← 入选公告 PDF 原文摘录补摘要（句子级纯抽取，不改写）
├── memory.py            ← Memory Agent（确定性：跨天去重/错误分布/已报道库）
├── funnel.py            ← Editor：三层筛选漏斗
├── generator.py         ← Writer（链接不进上下文；输出契约）
├── reviewer.py          ← Checker（独立模型配置）
├── precheck.py          ← Compliance：数字原值/禁词/关键限定/无锚点分句
├── compliance.py        ← 禁词表与荐股丢弃
├── error_log.py         ← sqlite（error_log/runs/agent_calls）+ 日报
├── render.py            ← 大字版 HTML（正文≥20px，查证链接 14px 淡色标注"查证用（可忽略）"）+ 简版页
├── tts.py / push.py     ← Publisher：音频、推送、告警、兜底落盘
├── data_layer.py        ← 样本加载/快讯/热点/akshare 可选
├── llm_client.py        ← OpenAI 兼容客户端
├── config.py            ← 路径、环境变量、prompt 版本 hash
├── scheduler.py         ← 每天 07:29 触发
├── main.py              ← 薄入口（CLI → Orchestrator）
├── data/                ← error_log.db + cache/（抓取快照）
└── tests/               ← pytest（含 regression_cases.json 永久回归集）
```
