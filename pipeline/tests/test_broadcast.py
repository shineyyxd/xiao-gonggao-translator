# -*- coding: utf-8 -*-
"""口播稿（broadcast.py）单测：数字中文化保精度 + 绝不念清单 + 开场结尾结构。"""
import broadcast


class TestNumberSpoken:
    def test_小数逐位读保精度(self):
        assert "十八点一一九元" in broadcast.to_spoken("按18.119元回购")
        assert "二点四九九元" in broadcast.to_spoken("每10股派2.499元")
        assert "一点八零" in broadcast.to_spoken("利率1.80%")
        assert "零点二四九九" in broadcast.to_spoken("每股0.2499元")

    def test_百分比读法(self):
        assert broadcast.to_spoken("利率1.68%") == "利率百分之一点六八"
        assert "百分之十八点二九" in broadcast.to_spoken("占18.29%")

    def test_一毛钱等价说法(self):
        assert "一毛钱" in broadcast.to_spoken("每股派0.10元（含税）")
        assert "一毛钱" in broadcast.to_spoken("每股0.1元")
        # 0.22 不是 0.1，不得念毛，逐位读
        assert "零点二二元" in broadcast.to_spoken("每股0.22元")

    def test_整数与大数(self):
        assert "一千股" in broadcast.to_spoken("你持有1000股")
        assert "十四万股" in broadcast.to_spoken("回购注销14万股")
        assert "五十六点二五万股" in broadcast.to_spoken("共56.25万股")
        assert "五百亿元" in broadcast.to_spoken("发行500亿元")
        assert "三百五十亿元" in broadcast.to_spoken("一笔350亿元")

    def test_千分位大数(self):
        assert "八十八万四千七百四十二千元" in broadcast.to_spoken("合计拟派现884,742千元")

    def test_年份日期逐位读(self):
        assert "二零二六年八月二十八日" in broadcast.to_spoken("预计2026年8月28日完成")
        assert "二零二五年度" in broadcast.to_spoken("已获2025年度股东会授权")
        assert broadcast.spoken_date_cn("2026-08-26") == "二零二六年八月二十六日"


class TestNeverSpeak:
    def test_星号不念(self):
        out = broadcast.to_spoken("*ST萃华第六次提醒")
        assert "*" not in out and "ST萃华" in out

    def test_英文剔除但保留ST(self):
        out = broadcast.to_spoken("TLAC债券和A股安排")
        assert "TLAC" not in out and "A股" not in out

    def test_括号冒号变停顿(self):
        out = broadcast.to_spoken("每股派0.10元（含税），（原文：每10股派1元）")
        assert "（" not in out and "）" not in out and "：" not in out
        assert "一毛钱" in out and "每十股派一元" in out

    def test_口播稿全文无链接无英文无星号(self):
        items = [{"line1": "*ST萃华（做珠宝的）第六次提醒：股票可能退市。",
                  "line2": "总市值连续14个交易日低于5亿元。http://example.com/x.pdf"}]
        script = broadcast.issue_script("2026-08-26", items)
        assert "http" not in script and "*" not in script
        for w in __import__("re").findall(r"[A-Za-z]+", script):
            assert w == "ST"


class TestIssueStructure:
    ITEMS = [{"company": "苏农银行", "line1": "苏农银行要分钱：每股派0.10元（含税）。",
              "line2": "你持有1000股，就能拿到100元（税前）。"}]

    def test_开场白含日期且合规话术只一次(self):
        script = broadcast.issue_script("2026-08-26", self.ITEMS)
        assert script.startswith("这里是小公告翻译官。今天是二零二六年八月二十六日。")
        assert "早报只说公告里的事实，不做任何投资建议。" in script  # 开场白
        assert script.count("不构成投资建议") == 1  # 固定话术只在结尾念一次
        assert script.endswith(broadcast.CLOSING)

    def test_条目序号与出处替代(self):
        script = broadcast.issue_script("2026-08-26", self.ITEMS)
        assert "第一条。" in script and "公告原文见文字版。" in script
        assert "一千股" in script and "一百元" in script and "一毛钱" in script

    def test_单条口播稿结构(self):
        s = broadcast.item_script(3, {"line1": "a", "line2": "b"})
        assert s.startswith("第三条。") and s.endswith("公告原文见文字版。")
