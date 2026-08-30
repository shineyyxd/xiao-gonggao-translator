# -*- coding: utf-8 -*-
"""合规禁词过滤单测。"""
import pytest

from compliance import BANNED_WORDS, find_banned, is_clean, should_drop
from config import DISCLAIMER


class TestFindBanned:
    @pytest.mark.parametrize("word", BANNED_WORDS)
    def test_每个禁词都能命中(self, word):
        assert word in find_banned(f"这支股票{word}，大家怎么看")

    def test_干净文本通过(self):
        assert is_clean("公司每股派发现金红利0.10元（含税）。")

    def test_固定合规话术不算违规(self):
        # “不构成投资建议”里的“建议”二字不应触发禁词
        assert is_clean(DISCLAIMER)
        assert is_clean(f"每股派1毛钱。{DISCLAIMER}")

    def test_多禁词同时命中(self):
        found = find_banned("这是利好，可以抄底买入")
        assert {"利好", "抄底", "买入"} <= set(found)


class TestFunnelDrop:
    def _ann(self, title, summary=""):
        return {"info_title": title, "info_summary": summary}

    def test_荐股话术丢弃(self):
        assert should_drop(self._ann("某公司深度报告：买入评级，目标价30元"))
        assert should_drop(self._ann("某公司点评", "我们强烈推荐，建议买入"))
        assert should_drop(self._ann("某公司快评：抄底机会来了"))

    def test_正常公告不丢弃(self):
        assert not should_drop(self._ann(
            "苏农银行2026年中期利润分配方案公告",
            "每股派发现金红利0.10元（含税）。"))
