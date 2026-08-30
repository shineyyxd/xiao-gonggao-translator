# -*- coding: utf-8 -*-
"""错误日志表（sqlite）+ 调用留痕（agent_calls）+ 准确率日报（markdown）。

表结构：
  error_log：run_date, item_id, company, stage(filter/generate/review/precheck),
             error_type(A/B/C/D/其他), detail, created_at
  runs：     run_date(主键), selected_count, generated_count, checkpoints_total,
             checkpoints_passed, accuracy, created_at,
             + run_id, status(OK/EMPTY_DAY/STOPPED), source(sample/live),
               prompt_version_gen, prompt_version_rev, stop_reason（升级迁移）
  agent_calls：run_id, agent(fetcher/editor/writer/checker/compliance/memory/publisher),
             item_id, input_summary, input_hash, output_summary, conclusion,
             elapsed_ms, retries, created_at
             —— "四留痕"之调用元数据：每次 LLM 调用（含重试）都落表。

日报输出到 产出/<日期>/日报.md。
"""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH, DISCLAIMER

_SCHEMA = """
CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    item_id TEXT,
    company TEXT,
    stage TEXT NOT NULL,
    error_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_date TEXT PRIMARY KEY,
    selected_count INTEGER NOT NULL,
    generated_count INTEGER NOT NULL,
    checkpoints_total INTEGER NOT NULL,
    checkpoints_passed INTEGER NOT NULL,
    accuracy REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    item_id TEXT,
    input_summary TEXT,
    input_hash TEXT,
    output_summary TEXT,
    conclusion TEXT,
    elapsed_ms INTEGER,
    retries INTEGER,
    created_at TEXT NOT NULL
);
"""

# runs 表升级列：(列名, 定义)——老库用 ALTER TABLE 迁移
_RUNS_NEW_COLUMNS = [
    ("run_id", "TEXT"),
    ("status", "TEXT DEFAULT 'OK'"),
    ("source", "TEXT DEFAULT 'sample'"),
    ("prompt_version_gen", "TEXT"),
    ("prompt_version_rev", "TEXT"),
    ("stop_reason", "TEXT"),
]

# agent_calls 表升级列：per-call token 用量（token 计量，向后兼容）
_AGENT_CALLS_NEW_COLUMNS = [
    ("prompt_tokens", "INTEGER"),
    ("completion_tokens", "INTEGER"),
    ("reasoning_tokens", "INTEGER"),
]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


class ErrorLog:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """老库补新列（幂等）。"""
        for table, new_cols in (("runs", _RUNS_NEW_COLUMNS),
                                ("agent_calls", _AGENT_CALLS_NEW_COLUMNS)):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in new_cols:
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ---- error_log ----
    def log_error(self, run_date: str, item_id, company: str, stage: str,
                  error_type: str, detail: str):
        self.conn.execute(
            "INSERT INTO error_log(run_date,item_id,company,stage,error_type,detail,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_date, str(item_id or ""), company or "", stage, error_type, detail or "", _now()),
        )
        self.conn.commit()

    def errors_of(self, run_date: str) -> list:
        cur = self.conn.execute(
            "SELECT item_id,company,stage,error_type,detail,created_at FROM error_log"
            " WHERE run_date=? ORDER BY id", (run_date,))
        return cur.fetchall()

    def error_type_stats(self) -> dict:
        """历史错误类型分布（A/B/C/D/其他 各多少）——Memory 给 Checker 的上下文。"""
        cur = self.conn.execute(
            "SELECT error_type, COUNT(*) FROM error_log GROUP BY error_type")
        return {t: c for t, c in cur.fetchall()}

    # ---- runs ----
    def save_run(self, run_date: str, selected_count: int, generated_count: int,
                 checkpoints_total: int, checkpoints_passed: int,
                 run_id: str = None, status: str = "OK", source: str = "sample",
                 prompt_version_gen: str = None, prompt_version_rev: str = None,
                 stop_reason: str = None) -> float:
        accuracy = (checkpoints_passed / checkpoints_total) if checkpoints_total else 1.0
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(run_date,selected_count,generated_count,"
            "checkpoints_total,checkpoints_passed,accuracy,created_at,"
            "run_id,status,source,prompt_version_gen,prompt_version_rev,stop_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_date, selected_count, generated_count, checkpoints_total,
             checkpoints_passed, round(accuracy, 4), _now(),
             run_id, status, source, prompt_version_gen, prompt_version_rev, stop_reason),
        )
        self.conn.commit()
        return accuracy

    def history(self) -> list:
        """全部日期的 runs 记录（准确率曲线数据）。"""
        cur = self.conn.execute(
            "SELECT run_date,selected_count,generated_count,checkpoints_total,"
            "checkpoints_passed,accuracy FROM runs ORDER BY run_date")
        return cur.fetchall()

    # ---- agent_calls（四留痕之调用元数据 + token 用量）----
    def log_agent_call(self, run_id: str, agent: str, item_id="",
                       input_summary: str = "", output_summary: str = "",
                       conclusion: str = "", elapsed_ms: int = 0, retries: int = 0,
                       prompt_tokens=None, completion_tokens=None, reasoning_tokens=None):
        self.conn.execute(
            "INSERT INTO agent_calls(run_id,agent,item_id,input_summary,input_hash,"
            "output_summary,conclusion,elapsed_ms,retries,created_at,"
            "prompt_tokens,completion_tokens,reasoning_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, agent, str(item_id or ""), input_summary or "", _hash(input_summary),
             output_summary or "", conclusion or "", int(elapsed_ms), int(retries), _now(),
             prompt_tokens, completion_tokens, reasoning_tokens),
        )
        self.conn.commit()

    def agent_calls_of(self, run_id: str) -> list:
        cur = self.conn.execute(
            "SELECT agent,item_id,input_summary,output_summary,conclusion,elapsed_ms,retries"
            " FROM agent_calls WHERE run_id=? ORDER BY id", (run_id,))
        return cur.fetchall()

    def usage_of(self, run_id: str) -> list:
        """当日 API 用量：按角色（writer/checker）聚合真实 LLM 调用的 token 数。

        只统计带 token 记录的行（真实调用；mock 行不计）。返回
        [{agent, calls, prompt_tokens, completion_tokens, reasoning_tokens}]。
        """
        cur = self.conn.execute(
            "SELECT agent, COUNT(prompt_tokens),"
            " COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),"
            " COALESCE(SUM(reasoning_tokens),0)"
            " FROM agent_calls WHERE run_id=? AND agent IN ('writer','checker')"
            " AND prompt_tokens IS NOT NULL GROUP BY agent", (run_id,))
        return [{"agent": a, "calls": c, "prompt_tokens": p,
                 "completion_tokens": o, "reasoning_tokens": r}
                for a, c, p, o, r in cur.fetchall()]

    def close(self):
        self.conn.close()


def render_daily_report(run_date: str, items_status: list, errors: list,
                        run_row: dict, history: list, meta: dict = None,
                        usage: list = None, integrity: dict = None) -> str:
    """生成当日准确率日报（markdown）。

    items_status: [{company,title,status,attempts,score,sector,event_type,link}]
    errors:       error_log 行 (item_id,company,stage,error_type,detail,created_at)
    run_row:      {selected_count,generated_count,checkpoints_total,checkpoints_passed,accuracy}
    history:      runs 表全部行
    meta:         {run_id,status,source,prompt_version_gen,prompt_version_rev,stop_reason,
                   gen_model,rev_model,prices}
    usage:        usage_of() 的聚合行 [{agent,calls,prompt_tokens,completion_tokens,reasoning_tokens}]
    integrity:    fetcher.assess_integrity() 的对账结论（sample 模式为 None，不渲染该节）
    """
    meta = meta or {}
    status = meta.get("status", "OK")
    status_text = {"OK": "正常刊发", "EMPTY_DAY": "今日无重要公告（简版）",
                   "STOPPED": "停刊"}.get(status, status)
    lines = [
        f"# 小公告翻译官（银发向）· 准确率日报（{run_date}）",
        "",
        f"> {DISCLAIMER}",
        "",
        "## 一、当日概览",
        "",
        f"- 状态：**{status_text}**",
        f"- run_id：`{meta.get('run_id', '')}`（数据源：{meta.get('source', '')}）",
        f"- prompt 版本：生成 `{meta.get('prompt_version_gen', '')}` / 校对 `{meta.get('prompt_version_rev', '')}`",
        f"- 选题数：{run_row['selected_count']}",
        f"- 生成成功数：{run_row['generated_count']}",
        f"- 事实核对点：{run_row['checkpoints_passed']}/{run_row['checkpoints_total']} 通过",
        f"- 事实准确率：**{run_row['accuracy'] * 100:.1f}%**",
    ]
    # 数据完整性对账（live 抓取时由 orchestrator 传入；覆盖率 <90% 标 WARNING）
    if integrity:
        integ = integrity
        pct = f"{integ['coverage'] * 100:.1f}%" if integ.get("coverage") is not None else "N/A"
        mark = " ⚠️ **WARNING**" if integ.get("status") == "WARNING" else ""
        reach = f"，分页可及 {integ['reachable']} 条" if integ.get("reachable") else ""
        lines.append(
            f"- 数据完整性：接口声称 {integ.get('claimed')} 条{reach}，"
            f"实际抓取 {integ.get('fetched')} 条（唯一 {integ.get('unique')} 条），"
            f"覆盖率 {pct}{mark}")
        if integ.get("note"):
            lines.append(f"  - {integ['note']}")
    if meta.get("stop_reason"):
        lines.append(f"- 停刊/异常原因：{meta['stop_reason']}")
    lines += [
        "",
        "## 二、选题清单与状态",
        "",
        "| # | 公司 | 板块 | 事件类型 | 分数 | 状态 | 生成次数 | 标题 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, it in enumerate(items_status, 1):
        lines.append(
            f"| {i} | {it['company']} | {it['sector']} | {it['event_type']} "
            f"| {it['score']} | {it['status']} | {it['attempts']} | {it['title']} |"
        )
    lines += ["", "## 三、错误明细", ""]
    if errors:
        lines += ["| 公司 | 阶段 | 错误类型 | 详情 |", "|---|---|---|---|"]
        for _item_id, company, stage, error_type, detail, _ts in errors:
            detail = (detail or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {company} | {stage} | {error_type} | {detail} |")
    else:
        lines.append("当日无错误记录。")
    lines += ["", "## 四、历史准确率曲线", "",
              "| 日期 | 选题数 | 生成成功 | 核对点通过 | 准确率 |", "|---|---|---|---|---|"]
    for d, sel, gen, total, passed, acc in history:
        lines.append(f"| {d} | {sel} | {gen} | {passed}/{total} | {acc * 100:.1f}% |")

    # ---- 当日 API 用量（真实 LLM 调用；mock 模式全为 0）----
    usage = usage or []
    prices = (meta.get("prices") or {})
    lines += ["", "## 五、当日 API 用量", "",
              "| 角色 | 模型 | 调用次数 | prompt tokens | completion tokens | 其中 reasoning |",
              "|---|---|---|---|---|---|"]
    # 模型名取当前配置（跨期改模型后以当日报表生成时的配置为准，日报有 prompt 版本可查）
    model_of = {"writer": meta.get("gen_model", ""), "checker": meta.get("rev_model", "")}
    name_of = {"writer": "Writer（生成）", "checker": "Checker（校对）"}
    total_cost = 0.0
    cost_shown = False
    for agent in ("writer", "checker"):
        row = next((u for u in usage if u["agent"] == agent), None)
        row = row or {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "reasoning_tokens": 0}
        lines.append(
            f"| {name_of[agent]} | {model_of[agent]} | {row['calls']} "
            f"| {row['prompt_tokens']} | {row['completion_tokens']} | {row['reasoning_tokens']} |")
        pin = prices.get(f"{agent}_input")
        pout = prices.get(f"{agent}_output")
        if pin and pout:
            # reasoning tokens 含在 completion 内，按输出单价计，不重复计费
            total_cost += row["prompt_tokens"] / 1e6 * pin + row["completion_tokens"] / 1e6 * pout
            cost_shown = True
    if cost_shown:
        lines += ["", f"估算成本：**{total_cost:.4f} 元**（单价来自 .env 配置，"
                      "reasoning 含在 completion 内按输出价计）"]
    else:
        lines += ["", "（未配置单价，仅显示 token 数；在 .env 填 "
                      "YFZB_PRICE_*_PER_MTOK 后显示估算成本）"]
    lines.append("")
    return "\n".join(lines)
