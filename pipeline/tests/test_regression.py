# -*- coding: utf-8 -*-
"""永久回归集：第一期测试报告 v1 的 8 处真实错误（见 regression_cases.json）。

  A 类 ×4：链接占位符/截断 → 断言链接由管线注入且与源数据一致，且不进 Writer prompt
  D 类 ×2：归母净利润→净利润 → 断言 precheck 关键限定检查能发现简化
  B 类 ×2：无中生有/额外判断 → 断言确定性层能标记"无锚点连续长句"（进 Checker 重点关注）

**改 prompt / 改规则必跑本文件。**（3 个漏斗规则缺陷的专测在 test_funnel.py）
"""
import json
from pathlib import Path

import pytest

import data_layer
import generator
import precheck
from orchestrator import build_output_item

CASES = json.loads((Path(__file__).parent / "regression_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sample():
    return data_layer.load_announcements()


def _find_by_id(sample, ann_id):
    for a in sample:
        if a["id"] == ann_id:
            return a
    raise AssertionError(f"样本中未找到 announcement id={ann_id}")


class TestLinkInjectionRegression:
    """v1 的 4 处链接错误（A 类）：已由"链接管线注入"机制性消除，永久回归。"""

    @pytest.mark.parametrize("case", CASES["link_cases"], ids=[c["id"] for c in CASES["link_cases"]])
    def test_链接由管线注入且与源一致(self, sample, case):
        ann = _find_by_id(sample, case["announcement_id"])
        assert ann["secu_abbr"] == case["company"]
        item = build_output_item(1, ann, {"line1": "a", "line2": "b", "line3": "c"})
        assert item["link"] == ann["announcement_link"]
        assert item["link"].startswith("http")

    @pytest.mark.parametrize("case", CASES["link_cases"], ids=[c["id"] for c in CASES["link_cases"]])
    def test_链接不进Writer上下文(self, sample, case):
        ann = _find_by_id(sample, case["announcement_id"])
        msg = generator.build_user_message(ann)
        assert "http" not in msg
        assert ann["announcement_link"] not in msg


class TestQualifierRegression:
    """v1 的 2 处"归母净利润→净利润"（D 类）：precheck 关键限定检查必须发现。"""

    @pytest.mark.parametrize("case", CASES["qualifier_cases"], ids=[c["id"] for c in CASES["qualifier_cases"]])
    def test_关键限定简化被打回(self, case):
        result = precheck.check(case["draft"], case["source_ann"])
        assert not result["ok"], f"{case['desc']}：未被检出"
        types = {e["类型"] for e in result["errors"]}
        assert case["expect_error"] in types, f"期望 {case['expect_error']}，实际 {types}"


class TestFabricationRegression:
    """v1 的 2 处无中生有/额外判断（B 类）：确定性层必须能标记无锚点长句。"""

    @pytest.mark.parametrize("case", CASES["fabrication_cases"], ids=[c["id"] for c in CASES["fabrication_cases"]])
    def test_无锚点长句进重点关注清单(self, case):
        watch = precheck.find_unanchored_clauses(case["draft"], case["source_ann"])
        assert any(case["expect_clause"] in c for c in watch), \
            f"{case['desc']}：期望在重点关注清单中标记「{case['expect_clause']}」，实际 {watch}"
