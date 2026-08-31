# -*- coding: utf-8 -*-
"""编排层测试：场景路由（EMPTY_DAY/STOPPED）、Memory 跨天去重、契约校验、
prompt 版本留痕、Publisher 兜底。全部用临时库与临时产出目录，不碰真实数据。"""
import json
import sqlite3

import pytest

import config
import fetcher
import funnel
import push
import tts
from error_log import ErrorLog
from generator import validate_contract
from memory import Memory
from orchestrator import EXIT_OK, EXIT_STOPPED, Orchestrator, process_item
from reviewer import Reviewer

EXPECTED_0826 = {"苏农银行", "瑞丰银行", "*ST萃华", "建设银行", "福莱特", "鼎际得"}


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """临时产出目录 + 临时 sqlite，返回 (out_root, db_path)。"""
    out_root = tmp_path / "产出"
    db_path = tmp_path / "data" / "error_log.db"
    monkeypatch.setattr(config, "OUTPUT_ROOT", out_root)
    return out_root, db_path


def _run(date, sandbox, **kw):
    out_root, db_path = sandbox
    kw.setdefault("mock_llm", True)
    kw.setdefault("no_tts", True)
    orch = Orchestrator(run_date=date, db_path=db_path, **kw)
    return orch.run()


# ---------- 场景路由 ----------

class TestEmptyDay:
    def test_窗口内无公告走简版路径(self, sandbox):
        # 样本只覆盖 08-25~08-26，选个窗口外日期 → 0 条选题
        summary = _run("2026-08-31", sandbox, source="sample")
        assert summary["exit_code"] == EXIT_OK
        assert summary["status"] == "EMPTY_DAY"
        out_root, db_path = sandbox
        assert (out_root / "2026-08-31" / "日报.md").exists()
        assert not (out_root / "2026-08-31" / "audio").exists()  # 简版无音频
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT status FROM runs WHERE run_date='2026-08-31'").fetchone()
        assert row[0] == "EMPTY_DAY"


class TestStopped:
    def test_抓取全败走停刊(self, sandbox, monkeypatch):
        def boom(day, tracer=None, meta_out=None):
            raise fetcher.FetchFailed("测试：两路均失败且无缓存")
        monkeypatch.setattr(fetcher, "fetch_live", boom)
        summary = _run("2026-08-27", sandbox, source="live")
        assert summary["exit_code"] == EXIT_STOPPED
        assert summary["status"] == "STOPPED"
        out_root, db_path = sandbox
        assert (out_root / "2026-08-27" / "停刊告警.txt").exists()
        report = (out_root / "2026-08-27" / "日报.md").read_text(encoding="utf-8")
        assert "停刊" in report and "数据抓取失败" in report
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT status, stop_reason FROM runs WHERE run_date='2026-08-27'").fetchone()
        assert row[0] == "STOPPED" and "数据抓取失败" in row[1]

    def test_全部条失败走停刊(self, sandbox, monkeypatch):
        # Writer 永远契约不符 → 全部条失败 → 停刊
        import generator as gen_mod
        monkeypatch.setattr(gen_mod.Generator, "generate",
                            lambda self, ann, feedback=None: {"line1": "长" * 60, "line2": "b", "line3": "c"})
        summary = _run("2026-08-26", sandbox, source="sample")
        assert summary["exit_code"] == EXIT_STOPPED
        assert summary["status"] == "STOPPED"
        assert "宁可停刊不可错发" in summary["reason"]


# ---------- 重放一致性与 Memory ----------

class TestReplay:
    def test_0826重放一致且同日幂等(self, sandbox):
        s1 = _run("2026-08-26", sandbox, source="sample")
        assert s1["status"] == "OK"
        assert {i["company"] for i in s1["items"]} == EXPECTED_0826
        # 再跑一次：reported_items 已有同日记录，但跨天去重严格早于当日 → 结果不变
        s2 = _run("2026-08-26", sandbox, source="sample")
        assert {i["company"] for i in s2["items"]} == EXPECTED_0826


class TestMemory:
    def test_跨天去重(self, tmp_path):
        mem = Memory(tmp_path / "m.db")
        picks = [{"secu_code": "603323", "secu_abbr": "苏农银行", "event_type": "分红派息",
                  "info_title": "t1"},
                 {"secu_code": "002731", "secu_abbr": "*ST萃华", "event_type": "重大风险",
                  "info_title": "t2"}]
        mem.record_items("2026-08-26", picks)
        # 同日严格排除（重放幂等）
        assert mem.recent_reported("2026-08-26") == set()
        # 次日可见
        reported = mem.recent_reported("2026-08-27")
        assert ("603323", "分红派息") in reported
        # 跨天去重：分红被剔除，重大风险豁免（连续提醒）
        kept, removed = mem.cross_day_dedup(picks, "2026-08-27")
        assert [p["secu_abbr"] for p in kept] == ["*ST萃华"]
        assert [p["secu_abbr"] for p in removed] == ["苏农银行"]
        mem.close()

    def test_漏斗接入reported集合(self):
        anns = [{
            "secu_abbr": "苏农银行", "secu_code": "603323",
            "info_title": "苏农银行:苏农银行2026年中期利润分配方案公告",
            "info_summary": "每股派发现金红利0.10元", "info_tag": "", "info_event_txt": "",
            "info_publ_date": "2026-08-27", "announcement_link": "http://x", "id": 1,
        }]
        assert len(funnel.select(anns)) == 1
        assert funnel.select(anns, reported={("603323", "分红派息")}) == []

    def test_Memory不进Writer上下文(self, sandbox, monkeypatch):
        # 抓 Writer 实际收到的 user message，断言不含 Memory 提示语
        seen = {}
        import generator as gen_mod
        orig = gen_mod.build_user_message
        def spy(ann, feedback=None):
            seen["msg"] = orig(ann, feedback)
            return seen["msg"]
        monkeypatch.setattr(gen_mod, "build_user_message", spy)
        summary = _run("2026-08-26", sandbox, source="sample")
        assert summary["status"] == "OK"
        assert "历史高频错误" not in seen.get("msg", "")
        assert "无锚点" not in seen.get("msg", "")


# ---------- 契约与版本留痕 ----------

class TestContract:
    def test_line1超长判打回(self):
        problems = validate_contract({"line1": "字" * 51, "line2": "b", "line3": "c"})
        assert any("line1 超长" in p for p in problems)

    def test_缺字段与禁词(self):
        assert validate_contract({"line1": "a", "line2": "", "line3": "c"})
        assert any("禁词" in p for p in validate_contract(
            {"line1": "a", "line2": "利好消息", "line3": "c"}))

    def test_正文自带合规话术判打回(self):
        """k3 会自行在稿尾加"不构成投资建议"——三段里出现即契约打回
        （话术由管线在期首/期尾统一注入，每期只留开头结尾各1次）。"""
        for key in ("line1", "line2", "line3"):
            draft = {"line1": "a", "line2": "b", "line3": "c"}
            draft[key] = draft[key] + "。本内容仅为公告信息整理，不构成投资建议"
            problems = validate_contract(draft)
            assert any("合规话术" in p for p in problems), f"{key} 未被拦截"
        # 干净的三段不误伤
        assert validate_contract({"line1": "a", "line2": "b", "line3": "c"}) == []

    def test_契约失败计为一次失败重试(self, tmp_path):
        class BadGen:
            def generate(self, ann, feedback=None):
                return {"line1": "长" * 60, "line2": "b", "line3": "c"}
        elog = ErrorLog(tmp_path / "e.db")
        ann = {"secu_abbr": "X", "id": 1, "info_title": "X:t", "info_summary": "",
               "info_event_txt": "", "info_publ_date": "2026-08-26", "secu_code": "0",
               "announcement_link": "http://x"}
        result = process_item(ann, BadGen(), Reviewer(mock=True), elog, "2026-08-26")
        assert not result["ok"] and result["attempts"] == 3
        errors = elog.errors_of("2026-08-26")
        assert any("输出契约不符" in e[4] for e in errors)
        elog.close()

    def test_prompt版本落表(self, sandbox):
        summary = _run("2026-08-26", sandbox, source="sample")
        assert summary["status"] == "OK"
        _out_root, db_path = sandbox
        conn = sqlite3.connect(str(db_path))
        gen_v, rev_v = config.get_prompt_versions()
        row = conn.execute(
            "SELECT prompt_version_gen, prompt_version_rev, run_id, status, source"
            " FROM runs WHERE run_date='2026-08-26'").fetchone()
        assert row[0] == gen_v and row[1] == rev_v
        assert row[2] == summary["run_id"] and row[3] == "OK" and row[4] == "sample"
        # agent_calls 留痕覆盖各角色（publisher 因 --no-tts 且无 push 缺席属预期）
        agents = {r[0] for r in conn.execute(
            "SELECT agent FROM agent_calls WHERE run_id=?", (summary["run_id"],))}
        assert {"fetcher", "editor", "writer", "checker", "compliance", "memory"} <= agents

    def test_审计jsonl双写(self, sandbox, monkeypatch):
        monkeypatch.setattr(tts, "synthesize", lambda items, out_dir, **kw: ([], []))
        summary = _run("2026-08-26", sandbox, source="sample", no_tts=False)
        out_root, _db = sandbox
        audit_files = list((out_root / "2026-08-26").glob("audit_*.jsonl"))
        assert len(audit_files) == 1
        rows = [json.loads(ln) for ln in audit_files[0].read_text(encoding="utf-8").splitlines()]
        agents = {r["agent"] for r in rows}
        assert {"fetcher", "editor", "writer", "checker", "compliance", "memory", "publisher"} <= agents
        assert all(r["run_id"] == summary["run_id"] for r in rows)


# ---------- Publisher 兜底 ----------

class TestPublisherFallback:
    def test_推送失败重试后落盘(self, tmp_path, monkeypatch):
        monkeypatch.setattr(push, "RETRY_INTERVALS", (0, 0, 0))
        fallback = tmp_path / "待人工推送.txt"
        result = push.push_wecom("测试内容", webhook="http://127.0.0.1:9/unreachable",
                                 fallback_path=fallback)
        assert not result["ok"] and result["attempts"] == 4  # 首发 + 3 次重试
        text = fallback.read_text(encoding="utf-8")
        assert "请人工推送" in text and "测试内容" in text

    def test_未配置webhook优雅跳过(self, monkeypatch):
        monkeypatch.setattr(push, "get_wecom_webhook", lambda: "")
        result = push.push_wecom("x")
        assert result["skipped"] and not result["ok"]


# ---------- token 用量计量 ----------

class _FakeGen:
    """模拟真实 Writer：返回合规草稿并带 last_usage（如真实 client）。"""
    def __init__(self):
        self.last_usage = None

    def generate(self, ann, feedback=None):
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 50,
                           "reasoning_tokens": 30}
        return {"line1": "苏农银行每股派1毛钱。",
                "line2": "每10股派1元，一共派出2.22亿元。",
                "line3": "《公告》，2026年8月26日发布。"}


class _FakeRev:
    def __init__(self):
        self.last_usage = None

    def review(self, ann, draft, extra_context=None):
        self.last_usage = {"prompt_tokens": 200, "completion_tokens": 40,
                           "reasoning_tokens": 25}
        return {"passed": True, "errors": [], "parse_ok": True}


class TestUsageMetering:
    def _ann(self):
        return {"secu_abbr": "苏农银行", "secu_code": "603323", "id": 7,
                "info_title": "苏农银行:苏农银行2026年中期利润分配方案公告",
                "info_summary": "每股派发现金红利0.10元，合计2.22亿元。",
                "info_event_txt": "", "info_publ_date": "2026-08-26",
                "announcement_link": "http://x", "score": 12, "sector": "银行",
                "event_type": "分红派息"}

    def test_writer_checker调用带token落表(self, tmp_path):
        from orchestrator import AuditTrail
        elog = ErrorLog(tmp_path / "e.db")
        audit = AuditTrail(tmp_path, "r1", elog)
        result = process_item(self._ann(), _FakeGen(), _FakeRev(), elog,
                              "2026-08-26", audit=audit)
        assert result["ok"]
        conn = elog.conn
        rows = conn.execute(
            "SELECT agent,prompt_tokens,completion_tokens,reasoning_tokens"
            " FROM agent_calls WHERE run_id='r1' AND agent IN ('writer','checker')"
        ).fetchall()
        assert ("writer", 100, 50, 30) in rows
        assert ("checker", 200, 40, 25) in rows
        # usage_of 聚合
        usage = {u["agent"]: u for u in elog.usage_of("r1")}
        assert usage["writer"]["calls"] == 1 and usage["writer"]["prompt_tokens"] == 100
        elog.close()

    def test_mock调用不计入用量(self, tmp_path):
        from generator import Generator
        elog = ErrorLog(tmp_path / "e.db")
        gen = Generator(mock=True, golden=[])
        assert gen.last_usage is None
        gen.generate(self._ann())
        assert gen.last_usage is None  # mock 不产生 token 用量
        elog.close()

    def test_日报用量节_无价仅token(self, tmp_path):
        from error_log import render_daily_report
        elog = ErrorLog(tmp_path / "e.db")
        elog.log_agent_call("r1", "writer", prompt_tokens=1000, completion_tokens=200,
                            reasoning_tokens=150)
        elog.log_agent_call("r1", "checker", prompt_tokens=3000, completion_tokens=100,
                            reasoning_tokens=80)
        usage = elog.usage_of("r1")
        report = render_daily_report(
            "2026-08-27", [], [],
            {"selected_count": 0, "generated_count": 0, "checkpoints_total": 0,
             "checkpoints_passed": 0, "accuracy": 1.0}, [],
            meta={"run_id": "r1", "status": "OK", "source": "live",
                  "gen_model": "deepseek-v4-flash", "rev_model": "deepseek-v4-pro",
                  "prices": {}},
            usage=usage)
        assert "## 五、当日 API 用量" in report
        assert "deepseek-v4-flash" in report and "1000" in report and "3000" in report
        assert "估算成本：**" not in report  # 无价不显示金额行
        elog.close()

    def test_日报用量节_有价显示成本(self, tmp_path):
        from error_log import render_daily_report
        elog = ErrorLog(tmp_path / "e.db")
        elog.log_agent_call("r1", "writer", prompt_tokens=1_000_000, completion_tokens=500_000,
                            reasoning_tokens=0)
        usage = elog.usage_of("r1")
        report = render_daily_report(
            "2026-08-27", [], [],
            {"selected_count": 0, "generated_count": 0, "checkpoints_total": 0,
             "checkpoints_passed": 0, "accuracy": 1.0}, [],
            meta={"run_id": "r1", "status": "OK", "source": "live",
                  "gen_model": "g", "rev_model": "r",
                  "prices": {"writer_input": 2.0, "writer_output": 8.0}},
            usage=usage)
        # 1M*2元 + 0.5M*8元 = 6元
        assert "估算成本：**6.0000 元**" in report
        elog.close()

    def test_老库迁移加token列(self, tmp_path):
        # 手工建一个旧版 agent_calls（无 token 列），再交给 ErrorLog 迁移
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE agent_calls(
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, agent TEXT,
            item_id TEXT, input_summary TEXT, input_hash TEXT, output_summary TEXT,
            conclusion TEXT, elapsed_ms INTEGER, retries INTEGER, created_at TEXT)""")
        conn.commit(); conn.close()
        elog = ErrorLog(db)  # 应幂等迁移
        cols = {r[1] for r in elog.conn.execute("PRAGMA table_info(agent_calls)")}
        assert {"prompt_tokens", "completion_tokens", "reasoning_tokens"} <= cols
        elog.close()
