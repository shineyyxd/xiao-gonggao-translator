# -*- coding: utf-8 -*-
"""Memory Agent（确定性代码，不是 LLM）。

铁律：**Memory 的输出绝不进 Writer 的上下文**——它只影响"选什么、查什么"，
不影响"说什么"。Writer 的事实只能来自当日公告原文。

两个能力：
  1. 跨天去重：给 Editor（漏斗）提供"近 7 天已报道 (secu_code, event_type) 清单"，
     同公司同事件类型已上过早报的剔除；**重大风险类除外**——退市风险提示这类
     公告需要连续提醒（公司自己都在发第六次、第七次），漏报风险大于重复打扰。
  2. 历史高频错误分布：从 error_log 统计 A/B/C/D 各多少，附加到 Checker 的
     上下文，提示它重点核查。
"""
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reported_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    secu_code TEXT NOT NULL,
    company TEXT,
    event_type TEXT NOT NULL,
    title TEXT
);
"""

# 需要连续提醒、豁免跨天去重的事件类型
DEDUP_EXEMPT = {"重大风险"}


class Memory:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def recent_reported(self, run_date: str, days: int = 7) -> set:
        """近 N 天（严格早于 run_date，不含当日，保证同日重放幂等）已报道的
        (secu_code, event_type) 集合。"""
        end = date.fromisoformat(run_date)
        start = (end - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "SELECT DISTINCT secu_code, event_type FROM reported_items"
            " WHERE run_date >= ? AND run_date < ?", (start, run_date))
        return {(code, ev) for code, ev in cur.fetchall()}

    def cross_day_dedup(self, picks: list, run_date: str, days: int = 7) -> tuple:
        """跨天去重。返回 (保留列表, 剔除列表)。重大风险类豁免（见模块注释）。"""
        reported = self.recent_reported(run_date, days)
        kept, removed = [], []
        for p in picks:
            key = (p.get("secu_code"), p.get("event_type"))
            if key in reported and p.get("event_type") not in DEDUP_EXEMPT:
                removed.append(p)
            else:
                kept.append(p)
        return kept, removed

    def error_type_stats(self) -> dict:
        """历史高频错误类型分布（从 error_log 统计），给 Checker 做重点核查提示。"""
        cur = self.conn.execute(
            "SELECT error_type, COUNT(*) FROM error_log GROUP BY error_type")
        return {t: c for t, c in cur.fetchall()}

    def record_items(self, run_date: str, picks: list):
        """每期结束后把本期选题写入 reported_items。"""
        self.conn.executemany(
            "INSERT INTO reported_items(run_date,secu_code,company,event_type,title)"
            " VALUES (?,?,?,?,?)",
            [(run_date, p.get("secu_code") or "", p.get("secu_abbr") or "",
              p.get("event_type") or "", p.get("info_title") or "") for p in picks],
        )
        self.conn.commit()

    def clear(self):
        self.conn.execute("DELETE FROM reported_items")
        self.conn.commit()

    def close(self):
        self.conn.close()
