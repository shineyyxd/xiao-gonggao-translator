# -*- coding: utf-8 -*-
"""确定性预检单测：数字原值核对 + 链接注入不经过 LLM。"""
import data_layer
import funnel
import generator
import precheck
from main import build_output_item

RUN_DATE = "2026-08-26"


def _sample():
    return data_layer.dedupe(data_layer.load_announcements())


def _picks():
    return funnel.select(data_layer.filter_by_window(_sample(), RUN_DATE))


def _fake_ann():
    return {
        "secu_abbr": "测试银行", "secu_code": "600000",
        "info_title": "测试银行:测试银行2026年中期利润分配方案公告",
        "info_publ_date": "2026-08-26", "info_tag": "",
        "info_summary": "测试银行拟每股派发现金红利0.10元（含税），合计2.22亿元，占归母净利润的18.29%。",
        "info_event_txt": "",
        "announcement_link": "http://static.sse.com.cn/test/600000_TEST.pdf",
        "id": 1,
    }


class TestNumberCheck:
    def test_原值数字通过(self):
        ann = _fake_ann()
        draft = {"line1": "测试银行每股派1毛钱。",
                 "line2": "一共派出2.22亿元，占归母净利润的18.29%。",
                 "line3": "《测试银行2026年中期利润分配方案公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert result["ok"], result["errors"]
        assert result["checkpoints"] == result["passed"]

    def test_编造数字打回(self):
        ann = _fake_ann()
        draft = {"line1": "测试银行每股派2毛钱。",  # 原文是0.10元/1毛，没有2毛
                 "line2": "约占净利润的19%。",        # 原文是18.29%
                 "line3": "《测试银行2026年中期利润分配方案公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert not result["ok"]
        types = {e["类型"] for e in result["errors"]}
        assert "A数字错" in types

    def test_精度改写打回(self):
        """'1.68%'被写成'约1.7%'：1.7 在原文找不到，必须打回。"""
        ann = _fake_ann()
        ann["info_summary"] = "本期债券票面利率1.68%，发行规模500亿元。"
        draft = {"line1": "测试银行发行500亿元债券。",
                 "line2": "票面利率约1.7%。",
                 "line3": "《公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert not result["ok"]
        assert any(e["位置"] == "1.7" for e in result["errors"])

    def test_禁词打回(self):
        ann = _fake_ann()
        draft = {"line1": "测试银行每股派1毛钱。",
                 "line2": "这是利好，值得关注。",  # C 类
                 "line3": "《公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert not result["ok"]
        assert "C倾向性话术" in {e["类型"] for e in result["errors"]}

    def test_到手算例与换算派生数放行(self):
        """规范允许的派生数过数字闸：1000股算例、每股0.1元（0.10/10 无关、
        100=1000×0.10）、万股换算（如原文 22000 万股 → 2.2 亿股不行，必须是10的倍率）。"""
        ann = _fake_ann()  # 摘要含 0.10元 / 2.22亿元 / 18.29%
        draft = {"line1": "测试银行每股派0.1元（原文：每股派发现金红利0.10元）。",
                 "line2": "你持有1000股，就能拿到100元（税前）。",
                 "line3": "《测试银行2026年中期利润分配方案公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert result["ok"], result["errors"]

    def test_万股换算放行(self):
        ann = _fake_ann()
        ann["info_summary"] = "回购注销140000股限制性股票，每股14.23元。"
        draft = {"line1": "测试银行回购注销14万股。",
                 "line2": "每股按14.23元买回。",
                 "line3": "《公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert result["ok"], result["errors"]

    def test_非10倍率乱换算仍打回(self):
        """每10股派1元被错算成每股0.3元：0.3 与 1.00 不是 10 的倍率，必须打回。"""
        ann = _fake_ann()
        ann["info_summary"] = "每10股派发现金红利1.00元（含税）。"
        draft = {"line1": "测试银行每股派0.3元。",
                 "line2": "你持有1000股，就能拿到300元（税前）。",
                 "line3": "《公告》，2026年8月26日发布。"}
        result = precheck.check(draft, ann)
        assert not result["ok"]
        assert "A数字错" in {e["类型"] for e in result["errors"]}

    def test_golden样例全部通过预检(self):
        """mock 数据源（人话稿_v2）6 条在真实公告上必须全部通过预检。"""
        golden = generator.load_golden()
        picks = _picks()
        gen = generator.Generator(mock=True, golden=golden)
        for ann in picks:
            draft = gen.generate(ann)
            result = precheck.check(draft, ann)
            assert result["ok"], f"{ann['secu_abbr']} 预检失败：{result['errors']}"


class TestLinkInjection:
    def test_链接不进生成prompt(self):
        for ann in _picks():
            msg = generator.build_user_message(ann)
            assert "http" not in msg
            assert ann["announcement_link"] not in msg

    def test_链接由管线注入(self):
        ann = _picks()[0]
        draft = {"line1": "a", "line2": "b", "line3": "c"}
        item = build_output_item(1, ann, draft)
        assert item["link"] == ann["announcement_link"]
        assert item["link"].startswith("http")

    def test_事件字段解析容错(self):
        # 正常解析
        ann = {"info_event_txt": "[{'事件类型': '股份回购', '关键信息': {'回购数量': '38658 股'}}]股份回购"}
        assert generator.parse_event_fields(ann)["关键信息"]["回购数量"] == "38658 股"
        # 坏串不炸
        assert generator.parse_event_fields({"info_event_txt": "这不是字面量"}) == {}
        assert generator.parse_event_fields({"info_event_txt": ""}) == {}
