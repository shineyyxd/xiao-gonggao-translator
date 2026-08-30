# -*- coding: utf-8 -*-
"""Orchestrator：确定性状态机 + 场景路由 + 全链路审计（不用 LLM）。

角色分工：Orchestrator/Memory 是确定性代码；LLM 只用于 Writer（生成）和
Checker（校对）；Fetcher/Editor/Compliance/Publisher 均为确定性规则。

审计"四留痕"：
  1. 输入快照：产出/<date>/raw_announcements.json（可重放复现）
  2. 输出原文：人话稿 JSON / 大字版 HTML / 音频
  3. 判定链路：产出/<date>/audit_<run_id>.jsonl（每个 Agent 每步一行）
  4. 调用元数据：sqlite agent_calls 表（含 Writer/Checker 每次 LLM 调用与重试）

场景路由（写死的路径）：
  FetchFailed        → 缓存降级（fetcher 内）→ 仍失败：停刊+告警，退出码 2
  漏斗后 0 条选题    → EMPTY_DAY 简版路径（无音频），退出码 0
  单条打回超 2 次    → 跳过该条，其余照发
  全部条失败         → 停刊+告警，退出码 2
  Publisher 推送失败 → 重试 3 次 → 落盘 待人工推送.txt

Memory 铁律：Memory 的输出只进 Editor（选什么）和 Checker（查什么），
绝不进 Writer 的上下文（说什么只由当日公告原文决定）。
"""
import json
import logging
import time
import uuid
from pathlib import Path

import config
import data_layer
import fetcher
import funnel
import precheck
import render
from compliance import should_drop
from error_log import ErrorLog, render_daily_report
from generator import Generator, clean_title, validate_contract
from llm_client import LLMClient
from memory import Memory
from reviewer import Reviewer

log = logging.getLogger("orchestrator")

MAX_RETRIES = 2  # 单条打回重试 ≤2 次（即每条最多生成 3 版）

EXIT_OK = 0
EXIT_STOPPED = 2


def _err_type(detail_type: str) -> str:
    """'A数字错' → 'A'；归到 A/B/C/D/其他。"""
    head = (detail_type or "")[:1]
    return head if head in "ABCD" else "其他"


class AuditTrail:
    """审计留痕：JSONL 判定链路 + sqlite agent_calls 调用元数据，双写。"""

    def __init__(self, out_dir: Path, run_id: str, elog: ErrorLog):
        self.out_dir = Path(out_dir)
        self.run_id = run_id
        self.elog = elog
        self.path = self.out_dir / f"audit_{run_id}.jsonl"

    def record(self, agent: str, item_id="", input_summary="", output_summary="",
               conclusion="", elapsed_ms=0, retries=0, usage=None, **extra):
        row = {"run_id": self.run_id, "agent": agent, "item_id": str(item_id or ""),
               "input": input_summary, "output": output_summary,
               "conclusion": conclusion, "elapsed_ms": int(elapsed_ms),
               "retries": int(retries), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        row.update(extra)
        if usage:  # LLM 调用的 token 用量（writer/checker 真实调用时带上）
            row["usage"] = usage
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        usage = usage or {}
        self.elog.log_agent_call(self.run_id, agent, item_id, input_summary,
                                 output_summary, conclusion, elapsed_ms, retries,
                                 prompt_tokens=usage.get("prompt_tokens"),
                                 completion_tokens=usage.get("completion_tokens"),
                                 reasoning_tokens=usage.get("reasoning_tokens"))


def build_output_item(idx: int, ann: dict, draft: dict) -> dict:
    """组装最终产出条目：格式对齐 人话稿_v2_0826.json，链接由管线注入。"""
    return {
        "id": idx,
        "company": ann.get("secu_abbr"),
        "title": clean_title(ann),
        "date": ann.get("info_publ_date"),
        "link": ann.get("announcement_link"),  # 管线注入，不经过任何 LLM 上下文
        "line1": draft["line1"],
        "line2": draft["line2"],
        "line3": draft["line3"],
        # 附加元数据（未打分公告允许缺省）
        "secu_code": ann.get("secu_code"),
        "announcement_id": ann.get("id"),
        "score": ann.get("score"),
        "sector": ann.get("sector"),
        "event_type": ann.get("event_type"),
    }


def process_item(ann: dict, generator: Generator, reviewer: Reviewer,
                 elog: ErrorLog, run_date: str, audit: AuditTrail = None,
                 checker_context: str = None) -> dict:
    """单条选题的 生成→契约→预检→校对 循环（打回重试 ≤2 次）。

    链接全程不进任何 LLM 上下文；Memory 只通过 checker_context 进 Checker。
    """
    company = ann.get("secu_abbr") or ""
    item_id = ann.get("id")

    def trace(agent, **kw):
        if audit:
            audit.record(agent, item_id=item_id, **kw)

    feedback, draft = None, None
    pre = {"checkpoints": 0, "passed": 0, "errors": [], "watchlist": []}

    for attempt in range(1, MAX_RETRIES + 2):  # 1 + 2 次重试
        # ---- Writer 生成 ----
        t0 = time.perf_counter()
        try:
            draft = generator.generate(ann, feedback)
        except Exception as e:
            trace("writer", input_summary=clean_title(ann), output_summary=str(e),
                  conclusion="FAILED", retries=attempt - 1,
                  elapsed_ms=(time.perf_counter() - t0) * 1000)
            log.warning("[%s] 第%d版生成异常：%s", company, attempt, e)
            continue
        elapsed = (time.perf_counter() - t0) * 1000
        writer_usage = getattr(generator, "last_usage", None)  # mock 时为 None

        # ---- 输出契约校验（不过 = CONTRACT_FAIL，视同一次失败重试）----
        problems = validate_contract(draft)
        if problems:
            trace("writer", input_summary=clean_title(ann),
                  output_summary="；".join(problems), conclusion="CONTRACT_FAIL",
                  retries=attempt - 1, elapsed_ms=elapsed, usage=writer_usage)
            elog.log_error(run_date, item_id, company, "generate", "其他",
                           "输出契约不符：" + "；".join(problems))
            feedback = [{"类型": "其他", "位置": "", "问题": p} for p in problems]
            log.warning("[%s] 第%d版契约打回：%s", company, attempt, problems)
            continue
        trace("writer", input_summary=clean_title(ann),
              output_summary=(draft.get("line1") or "")[:60], conclusion="OK",
              retries=attempt - 1, elapsed_ms=elapsed, usage=writer_usage)

        # ---- 确定性预检（不过直接打回，不浪费 LLM 校对调用）----
        pre = precheck.check(draft, ann)
        trace("compliance", input_summary=clean_title(ann),
              output_summary=f"{pre['passed']}/{pre['checkpoints']} 核对点，"
                             f"无锚点分句 {len(pre['watchlist'])} 个",
              conclusion="PASS" if pre["ok"] else "FAIL", retries=attempt - 1)
        if not pre["ok"]:
            for e in pre["errors"]:
                elog.log_error(run_date, item_id, company, "precheck",
                               _err_type(e.get("类型")), f"{e.get('位置')}: {e.get('问题')}")
            feedback = pre["errors"]
            log.warning("[%s] 第%d版预检打回：%d 处", company, attempt, len(pre["errors"]))
            continue

        # ---- Checker 校对（Memory 提示只进这里）----
        t0 = time.perf_counter()
        try:
            verdict = reviewer.review(ann, draft, extra_context=checker_context)
        except Exception as e:
            trace("checker", input_summary=clean_title(ann), output_summary=str(e),
                  conclusion="FAILED", retries=attempt - 1,
                  elapsed_ms=(time.perf_counter() - t0) * 1000)
            log.warning("[%s] 第%d版校对异常：%s", company, attempt, e)
            continue
        conclusion = "PASS" if verdict["passed"] else (
            "REJECT" if verdict.get("parse_ok", True) else "PARSE_FAIL")
        trace("checker", input_summary=clean_title(ann),
              output_summary=json.dumps(verdict["errors"], ensure_ascii=False)[:200],
              conclusion=conclusion, retries=attempt - 1,
              elapsed_ms=(time.perf_counter() - t0) * 1000,
              usage=getattr(reviewer, "last_usage", None))
        if verdict["passed"]:
            log.info("[%s] 第%d版通过", company, attempt)
            return {"ok": True, "attempts": attempt, "draft": draft, "precheck": pre}
        for e in verdict["errors"]:
            elog.log_error(run_date, item_id, company, "review",
                           _err_type(e.get("类型")), f"{e.get('位置')}: {e.get('问题')}")
        feedback = verdict["errors"]
        log.warning("[%s] 第%d版校对打回：%d 处", company, attempt, len(verdict["errors"]))

    # 重试耗尽仍不过：宁缺毋滥，标记失败并跳过
    elog.log_error(run_date, item_id, company, "generate", "其他",
                   f"重试 {MAX_RETRIES} 次仍未通过，本条跳过")
    log.error("[%s] 重试耗尽，本条跳过", company)
    return {"ok": False, "attempts": MAX_RETRIES + 1, "draft": draft, "precheck": pre}


class Orchestrator:
    """确定性编排器：按写死的场景路由驱动 8 个角色。"""

    def __init__(self, run_date: str, source: str = "sample", mock_llm: bool = False,
                 no_tts: bool = False, do_push: bool = False, window_days: int = 5,
                 db_path: Path = None, ignore_memory: bool = False,
                 pdf_summary: bool = None):
        self.run_date = run_date
        self.source = source
        self.mock_llm = mock_llm
        self.no_tts = no_tts
        self.do_push = do_push
        self.window_days = window_days
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.ignore_memory = ignore_memory
        # PDF 摘要开关：默认 live 开、sample 关（CLI --pdf-summary/--no-pdf-summary 覆盖）
        self.pdf_summary = (source == "live") if pdf_summary is None else pdf_summary
        self.integrity = None  # live 抓取后的数据完整性对账结论（assess_integrity）
        self.run_id = f"{run_date}-{uuid.uuid4().hex[:8]}"
        self.out_dir = config.OUTPUT_ROOT / run_date
        self.prompt_gen_v, self.prompt_rev_v = config.get_prompt_versions()

    # ---- 内部工具 ----
    def _meta(self, status, stop_reason=None):
        gen_cfg, rev_cfg = config.get_gen_config(), config.get_rev_config()
        return {"run_id": self.run_id, "status": status, "source": self.source,
                "prompt_version_gen": self.prompt_gen_v,
                "prompt_version_rev": self.prompt_rev_v, "stop_reason": stop_reason,
                "gen_model": gen_cfg["model"], "rev_model": rev_cfg["model"],
                "prices": config.get_prices()}

    def _write_report(self, elog, items_status, run_row, status, stop_reason=None):
        report = render_daily_report(
            self.run_date, items_status, elog.errors_of(self.run_date), run_row,
            elog.history(), meta=self._meta(status, stop_reason),
            usage=elog.usage_of(self.run_id), integrity=self.integrity)
        path = self.out_dir / "日报.md"
        path.write_text(report, encoding="utf-8")
        log.info("已写 %s", path)

    def _assess_fetch_integrity(self, fetch_meta: dict, elog: ErrorLog,
                                audit: AuditTrail):
        """数据完整性对账：结论存 self.integrity（进日报）；WARNING 记 error_log。"""
        if not fetch_meta:
            return
        integ = fetcher.assess_integrity(fetch_meta)
        self.integrity = integ
        pct = f"{integ['coverage'] * 100:.1f}%" if integ.get("coverage") is not None else "N/A"
        reach = f"，分页可及 {integ['reachable']} 条" if integ.get("reachable") else ""
        summary = (f"接口声称 {integ.get('claimed')} 条{reach}，"
                   f"实际抓取 {integ.get('fetched')} 条（唯一 {integ.get('unique')} 条），"
                   f"覆盖率 {pct}")
        if integ.get("note"):
            summary += f"；{integ['note']}"
        audit.record("fetcher", input_summary="数据完整性对账",
                     output_summary=summary, conclusion=integ["status"])
        if integ["status"] == "WARNING":
            elog.log_error(self.run_date, "", "", "fetcher", "完整性缺口", summary)

    # ---- 停刊路径 ----
    def _stop(self, reason: str, elog: ErrorLog, audit: AuditTrail,
              items_status=None, run_row=None) -> dict:
        log.error("当日停刊：%s", reason)
        audit.record("editor", output_summary=reason, conclusion="STOPPED")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / f"大字版_{self.run_date}.html").write_text(
            render.render_simple(self.run_date, "今日停刊", reason), encoding="utf-8")
        run_row = run_row or {"selected_count": 0, "generated_count": 0,
                              "checkpoints_total": 0, "checkpoints_passed": 0,
                              "accuracy": 1.0}
        accuracy = elog.save_run(
            self.run_date, run_row["selected_count"], run_row["generated_count"],
            run_row["checkpoints_total"], run_row["checkpoints_passed"],
            run_id=self.run_id, status="STOPPED", source=self.source,
            prompt_version_gen=self.prompt_gen_v, prompt_version_rev=self.prompt_rev_v,
            stop_reason=reason)
        run_row["accuracy"] = accuracy
        self._write_report(elog, items_status or [], run_row, "STOPPED", reason)
        import push
        pushed = push.send_alert(f"{self.run_date} 停刊：{reason}",
                                 fallback_path=self.out_dir / "停刊告警.txt")
        audit.record("publisher", input_summary="停刊告警",
                     output_summary=reason, conclusion="ALERT_SENT" if pushed else "ALERT_FILED")
        return {"exit_code": EXIT_STOPPED, "status": "STOPPED", "reason": reason,
                "run_id": self.run_id, "items": [], "picks": []}

    # ---- 主流程 ----
    def run(self) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        elog = ErrorLog(self.db_path)
        memory = Memory(self.db_path)
        audit = AuditTrail(self.out_dir, self.run_id, elog)
        log.info("run_id=%s source=%s prompt版本 gen=%s rev=%s",
                 self.run_id, self.source, self.prompt_gen_v, self.prompt_rev_v)
        try:
            return self._run(elog, memory, audit)
        finally:
            elog.close()
            memory.close()

    def _run(self, elog: ErrorLog, memory: Memory, audit: AuditTrail) -> dict:
        # ---- Fetcher：取数 + 输入快照 ----
        t0 = time.perf_counter()
        fetch_meta = {}
        try:
            if self.source == "live":
                raw = fetcher.fetch_live(self.run_date, tracer=audit, meta_out=fetch_meta)
            else:
                raw = data_layer.load_announcements()
                audit.record("fetcher", input_summary=f"本地样本（source=sample）",
                             output_summary=f"{len(raw)} 条", conclusion="OK",
                             elapsed_ms=(time.perf_counter() - t0) * 1000)
        except fetcher.FetchFailed as e:
            return self._stop(f"数据抓取失败：{e}", elog, audit)
        self._assess_fetch_integrity(fetch_meta, elog, audit)
        snapshot = self.out_dir / "raw_announcements.json"
        snapshot.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        audit.record("fetcher", output_summary=f"输入快照 {snapshot.name}（{len(raw)} 条）",
                     conclusion="SNAPSHOT")
        data_layer.load_flash_news()
        data_layer.load_hot_news()
        data_layer.market_background()  # 可选层，没装 akshare 自动跳过

        # ---- Editor：漏斗 + Memory 跨天去重 ----
        windowed = data_layer.filter_by_window(data_layer.dedupe(raw),
                                               self.run_date, self.window_days)
        dropped = sum(1 for a in windowed if should_drop(a))
        audit.record("compliance", input_summary=f"窗口内 {len(windowed)} 条",
                     output_summary=f"荐股话术丢弃 {dropped} 条", conclusion="OK")
        reported = set() if self.ignore_memory else memory.recent_reported(self.run_date)
        audit.record("memory", input_summary="近7天已报道清单",
                     output_summary=f"{len(reported)} 个 (公司,事件类型)"
                                    + ("（--ignore-memory 已停用）" if self.ignore_memory else ""),
                     conclusion="OK")
        picks = funnel.select(windowed, reported=reported or None)
        audit.record("editor", input_summary=f"窗口内 {len(windowed)} 条，跨天去重库 {len(reported)} 个",
                     output_summary="、".join(p["secu_abbr"] for p in picks) or "（无）",
                     conclusion=f"选题 {len(picks)} 条")
        log.info("选题 %d 条：%s", len(picks), "、".join(p["secu_abbr"] for p in picks))

        # ---- 场景路由：0 条选题 → EMPTY_DAY ----
        if not picks:
            audit.record("editor", output_summary="今日无重要公告", conclusion="EMPTY_DAY")
            (self.out_dir / f"大字版_{self.run_date}.html").write_text(
                render.render_simple(self.run_date, "今日无重要公告",
                                     "今天没有需要您留意的上市公司公告。"), encoding="utf-8")
            accuracy = elog.save_run(self.run_date, 0, 0, 0, 0,
                                     run_id=self.run_id, status="EMPTY_DAY", source=self.source,
                                     prompt_version_gen=self.prompt_gen_v,
                                     prompt_version_rev=self.prompt_rev_v)
            self._write_report(elog, [], {"selected_count": 0, "generated_count": 0,
                                          "checkpoints_total": 0, "checkpoints_passed": 0,
                                          "accuracy": accuracy}, "EMPTY_DAY")
            return {"exit_code": EXIT_OK, "status": "EMPTY_DAY", "run_id": self.run_id,
                    "items": [], "picks": [], "out_dir": self.out_dir}

        # ---- PDF 摘要：给入选公告补 PDF 原文摘录（只抓入选的几条，控制量）----
        if self.pdf_summary:
            import pdf_summary
            stats = pdf_summary.fill_summaries(picks, tracer=audit)
            log.info("PDF 摘要：%s", stats)
        else:
            log.info("PDF 摘要关闭（--no-pdf-summary 或 sample 默认）")

        # ---- Writer/Checker（Memory 提示只进 Checker，绝不进 Writer）----
        gen_cfg, rev_cfg = config.get_gen_config(), config.get_rev_config()
        mock = self.mock_llm or not gen_cfg["api_key"]
        if mock:
            log.info("mock 模式（--mock-llm 或未配置 YFZB_GEN_API_KEY）")
        gen_client = None if mock else LLMClient(**gen_cfg)
        rev_client = None if (mock or not rev_cfg["api_key"]) else LLMClient(**rev_cfg)
        generator = Generator(client=gen_client, mock=mock)
        reviewer = Reviewer(client=rev_client, mock=mock or rev_client is None)

        checker_context = None
        if not mock:
            stats = memory.error_type_stats()
            audit.record("memory", input_summary="历史高频错误分布",
                         output_summary=json.dumps(stats, ensure_ascii=False), conclusion="OK")
            checker_context = f"历史高频错误类型分布：{json.dumps(stats, ensure_ascii=False)}，请重点核查。"

        items, items_status = [], []
        ck_total = ck_passed = 0
        for ann in picks:
            result = process_item(ann, generator, reviewer, elog, self.run_date,
                                  audit=audit, checker_context=checker_context)
            pre = result["precheck"]
            ck_total += pre["checkpoints"]
            ck_passed += pre["passed"]
            status = f"通过（第{result['attempts']}版）" if result["ok"] else "失败（已跳过）"
            items_status.append({
                "company": ann.get("secu_abbr"), "title": clean_title(ann),
                "sector": ann["sector"], "event_type": ann["event_type"],
                "score": ann["score"], "status": status,
                "attempts": result["attempts"], "link": ann.get("announcement_link"),
            })
            if result["ok"]:
                items.append(build_output_item(len(items) + 1, ann, result["draft"]))

        # ---- 场景路由：全部条失败 → 停刊 ----
        if not items:
            return self._stop("全部选题生成/校对失败，宁可停刊不可错发", elog, audit,
                              items_status=items_status,
                              run_row={"selected_count": len(picks), "generated_count": 0,
                                       "checkpoints_total": ck_total,
                                       "checkpoints_passed": ck_passed, "accuracy": 0.0})

        # ---- 产出物 ----
        json_path = self.out_dir / f"人话稿_{self.run_date}.json"
        json_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        html_path = self.out_dir / f"大字版_{self.run_date}.html"
        html_path.write_text(render.render_html(self.run_date, items), encoding="utf-8")
        log.info("已写 %s / %s", json_path, html_path)

        audio_paths, tts_failures = [], []
        if not self.no_tts:
            import tts
            t0 = time.perf_counter()
            audio_paths, tts_failures = tts.synthesize(items, self.out_dir / "audio")
            audit.record("publisher", input_summary=f"TTS {len(items)} 条",
                         output_summary=f"成功 {len(audio_paths)}，失败 {len(tts_failures)}",
                         conclusion="OK" if not tts_failures else "DEGRADED",
                         elapsed_ms=(time.perf_counter() - t0) * 1000)
        else:
            log.info("--no-tts，跳过音频合成")

        # ---- Memory 记录本期实际发出的条目（失败跳过的不算"已报道"）----
        published = [p for p, s in zip(picks, items_status) if s["status"].startswith("通过")]
        memory.record_items(self.run_date, published)
        audit.record("memory", input_summary="写入 reported_items",
                     output_summary=f"{len(published)} 条", conclusion="OK")

        # ---- runs + 日报 ----
        accuracy = elog.save_run(self.run_date, len(picks), len(items), ck_total, ck_passed,
                                 run_id=self.run_id, status="OK", source=self.source,
                                 prompt_version_gen=self.prompt_gen_v,
                                 prompt_version_rev=self.prompt_rev_v)
        self._write_report(elog, items_status,
                           {"selected_count": len(picks), "generated_count": len(items),
                            "checkpoints_total": ck_total, "checkpoints_passed": ck_passed,
                            "accuracy": accuracy}, "OK")

        # ---- Publisher：推送（可选）----
        push_result = None
        if self.do_push:
            import push
            content = push.build_markdown(self.run_date, items)
            push_result = push.push_wecom(
                content, fallback_path=self.out_dir / "待人工推送.txt")
            audit.record("publisher", input_summary=f"企业微信推送 {len(items)} 条",
                         output_summary=push_result.get("error", "") or "推送成功",
                         conclusion="OK" if push_result["ok"] else (
                             "SKIPPED" if push_result["skipped"] else "FALLBACK_FILED"),
                         retries=max(push_result["attempts"] - 1, 0))

        return {"exit_code": EXIT_OK, "status": "OK", "run_id": self.run_id,
                "mock": mock, "picks": picks, "items": items, "items_status": items_status,
                "accuracy": accuracy, "checkpoints_total": ck_total,
                "checkpoints_passed": ck_passed, "out_dir": self.out_dir,
                "json_path": json_path, "html_path": html_path,
                "report_path": self.out_dir / "日报.md", "audio_paths": audio_paths,
                "push_result": push_result}
