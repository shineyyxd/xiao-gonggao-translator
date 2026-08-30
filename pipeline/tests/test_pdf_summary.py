# -*- coding: utf-8 -*-
"""PDF 摘要抓取单测：句子级纯抽取（不改写）+ 单条失败降级 + 审计留痕。"""
import sys

import pdf_summary


class _FakeTracer:
    def __init__(self):
        self.rows = []

    def record(self, agent, **kw):
        self.rows.append({"agent": agent, **kw})


def _ann(**kw):
    base = {"id": 1, "secu_abbr": "测试公司", "info_summary": "",
            "announcement_link": "http://static.cninfo.com.cn/finalpage/x.PDF"}
    base.update(kw)
    return base


class TestExtractSummary:
    def test_含数字句子优先且逐字摘自原文(self):
        text = ("本公司董事会全体成员保证公告内容真实准确完整。\n"
                "公司拟向全体股东每10股派发现金红利1.00元（含税）。\n"
                "本次分配方案已经董事会审议通过。")
        out = pdf_summary.extract_summary(text)
        assert out.startswith("公司拟向全体股东每10股派发现金红利1.00元（含税）。")
        # 纯抽取：输出的每个句子都是原文原句
        for s in ("公司拟向全体股东每10股派发现金红利1.00元（含税）。",
                  "本次分配方案已经董事会审议通过。"):
            assert s in text and s in out

    def test_长度上限与超长句跳过(self):
        long_sent = "长" * 600 + "1。"
        text = long_sent + "每股派0.5元。"
        out = pdf_summary.extract_summary(text, max_len=500)
        assert len(out) <= 500
        assert "每股派0.5元。" in out and "长" not in out

    def test_空文本与碎片(self):
        assert pdf_summary.extract_summary("") == ""
        assert pdf_summary.extract_summary("短句。行。") == ""

    def test_重复句去重(self):
        text = "每股派发现金红利0.10元（含税）。\n每股派发现金红利0.10元（含税）。"
        assert pdf_summary.extract_summary(text).count("0.10") == 1


class TestFillSummaries:
    def test_只补空摘要并记OK(self, monkeypatch):
        monkeypatch.setattr(pdf_summary, "POLITE_DELAY", 0)
        monkeypatch.setattr(pdf_summary, "fetch_pdf_text",
                            lambda url: "公司拟每10股派1.00元（含税）。")
        anns = [_ann(id=1), _ann(id=2, info_summary="已有摘要，不动")]
        tracer = _FakeTracer()
        stats = pdf_summary.fill_summaries(anns, tracer=tracer)
        assert stats == {"尝试": 1, "成功": 1, "失败": 0, "已有摘要跳过": 1}
        assert "每10股派1.00元" in anns[0]["info_summary"]
        assert anns[1]["info_summary"] == "已有摘要，不动"
        assert tracer.rows[0]["agent"] == "pdf_summary"
        assert tracer.rows[0]["item_id"] == 1 and tracer.rows[0]["conclusion"] == "OK"

    def test_单条失败留空不阻断(self, monkeypatch):
        monkeypatch.setattr(pdf_summary, "POLITE_DELAY", 0)
        def boom(url):
            raise RuntimeError("下载超时")
        monkeypatch.setattr(pdf_summary, "fetch_pdf_text", boom)
        anns = [_ann(id=7)]
        tracer = _FakeTracer()
        stats = pdf_summary.fill_summaries(anns, tracer=tracer)
        assert stats["失败"] == 1
        assert anns[0]["info_summary"] == ""  # 留空降级
        assert tracer.rows[0]["conclusion"] == "FAIL"

    def test_pypdf未安装整体跳过(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pypdf", None)  # import pypdf → ImportError
        tracer = _FakeTracer()
        stats = pdf_summary.fill_summaries([_ann()], tracer=tracer)
        assert stats["尝试"] == 0
        assert tracer.rows[0]["conclusion"] == "SKIPPED"

    def test_全部已有摘要直接返回(self):
        stats = pdf_summary.fill_summaries([_ann(info_summary="有")])
        assert stats["尝试"] == 0 and stats["已有摘要跳过"] == 1
