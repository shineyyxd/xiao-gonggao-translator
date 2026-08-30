# -*- coding: utf-8 -*-
"""三层漏斗规则单测（覆盖第一期测试报告中的 3 个已修复缺陷）。"""
import pytest

import data_layer
import funnel
from funnel import classify_sector, merge_same_theme, score_announcement, score_event, select

RUN_DATE = "2026-08-26"
EXPECTED_COMPANIES = {"苏农银行", "瑞丰银行", "*ST萃华", "建设银行", "福莱特", "鼎际得"}


@pytest.fixture(scope="module")
def sample():
    return data_layer.dedupe(data_layer.load_announcements())


@pytest.fixture(scope="module")
def picks(sample):
    windowed = data_layer.filter_by_window(sample, RUN_DATE)
    return select(windowed)


def _find(sample, abbr, title_kw=None):
    for a in sample:
        if a["secu_abbr"] == abbr and (title_kw is None or title_kw in a["info_title"]):
            return a
    raise AssertionError(f"样本中未找到 {abbr} {title_kw}")


# ---------- 打分规则基本盘 ----------

class TestScoreEvent:
    def test_分红派息10分(self):
        assert score_event("苏农银行2026年中期利润分配方案公告") == (10, "分红派息")

    def test_业绩标题级9分(self):
        assert score_event("某公司2026年半年度业绩预告") == (9, "业绩")
        assert score_event("某公司2026年半年度业绩快报") == (9, "业绩")

    def test_重大风险9分(self):
        assert score_event("关于公司股票存在可能因市值被终止上市的第六次风险提示公告") == (9, "重大风险")

    def test_回购注销6分(self):
        assert score_event("关于回购注销部分限制性股票的公告") == (6, "回购注销")

    def test_发债完成4分(self):
        assert score_event("关于2026年二级资本债券（第一期）发行完毕的公告") == (4, "发债完成")

    def test_人事变动4分(self):
        assert score_event("关于董事长离任的公告") == (4, "人事变动")

    def test_程序件1分(self):
        assert score_event("律师事务所关于某公司激励计划之法律意见书") == (1, "程序件")
        assert score_event("华泰联合证券关于某公司向特定对象发行股票之发行保荐书") == (1, "程序件")


class TestSector:
    def test_银行板块(self, sample):
        assert classify_sector(_find(sample, "苏农银行", "利润分配")) == "银行"
        assert classify_sector(_find(sample, "瑞丰银行", "利润分配")) == "银行"

    def test_银行加分(self, sample):
        # 分红 10 + 银行 2 = 12
        assert score_announcement(_find(sample, "苏农银行", "利润分配"))["score"] == 12
        # 发债 4 + 银行 2 = 6
        assert score_announcement(_find(sample, "建设银行", "发行完毕"))["score"] == 6

    def test_银行误命中排除(self, sample):
        # *ST萃华 摘要含"银行账户被冻结"，不能错划进银行板块
        ann = _find(sample, "*ST萃华", "第六次")
        assert "银行账户" in ann["info_summary"]
        assert classify_sector(ann) == "其他"

    def test_公用事业(self):
        assert classify_sector({"secu_abbr": "江南水务", "info_title": "江南水务2026年半年度利润分配方案公告",
                                "info_summary": ""}) == "公用事业"


# ---------- 缺陷1：事件标签误命中（业绩类信号只认标题级） ----------

class TestDefect1TagMisfire:
    def test_tag含业绩预告但标题是保荐书_判程序件(self, sample):
        """中广核技定增保荐书：info_tag 含'业绩预告'，仍应判程序件 1 分而非业绩 9 分。"""
        ann = _find(sample, "中广核技", "发行保荐书")
        assert "业绩预告" in (ann["info_tag"] or "")
        scored = score_announcement(ann)
        assert scored["event_type"] == "程序件"
        assert scored["score"] == 1

    def test_打分不看info_tag与event_txt(self):
        """合成样本：tag/event_txt 都写'业绩预告'，标题是保荐书 → 1 分。"""
        ann = {
            "secu_abbr": "测试股份", "secu_code": "000000",
            "info_title": "测试股份:某证券关于测试股份定向增发之保荐书",
            "info_tag": "定向增发,业绩预告",
            "info_event_txt": "[{'事件类型': '业绩预告', '关键信息': {}}]业绩预告",
            "info_summary": "",
        }
        scored = score_announcement(ann)
        assert scored["score"] == 1
        assert scored["event_type"] == "程序件"


# ---------- 缺陷2：同公司同主题合并 ----------

class TestDefect2MergeSameTheme:
    def test_萃华两次风险提示只留一条(self, sample):
        cuis = [a for a in sample if a["secu_abbr"] == "*ST萃华"]
        assert len(cuis) >= 2  # 第六次 + 第七次风险提示
        scored = [score_announcement(a) for a in cuis]
        merged = merge_same_theme(scored)
        assert len(merged) == 1
        # 同分取日期最新：留下 08-26 的第六次
        assert "第六次" in merged[0]["info_title"]
        assert merged[0]["info_publ_date"] == "2026-08-26"

    def test_合并键是公司加事件类型(self):
        base = {"secu_abbr": "X", "secu_code": "000001", "info_summary": ""}
        a = score_announcement({**base, "info_title": "X:关于回购注销部分股票的公告",
                                "info_publ_date": "2026-08-25"})
        b = score_announcement({**base, "info_title": "X:2026年中期利润分配方案公告",
                                "info_publ_date": "2026-08-26"})
        merged = merge_same_theme([a, b])
        assert len(merged) == 2  # 不同事件类型不合并


# ---------- 缺陷3：最低分门槛（禁止低分填充） ----------

class TestDefect3MinScore:
    def test_粤电力DFI零分(self, sample):
        """粤电力A 的 DFI 注册申请是 0 分公告（当年低分填充的反面教材）。"""
        ann = _find(sample, "粤电力A")
        assert score_announcement(ann)["score"] < 4

    def test_配额没凑满也不选低分(self):
        low = {
            "secu_abbr": "低分公司", "secu_code": "000002",
            "info_title": "低分公司:关于召开2026年第一次临时股东大会的通知",
            "info_summary": "会议通知", "info_tag": "", "info_event_txt": "",
            "info_publ_date": "2026-08-26", "announcement_link": "http://x",
        }
        # 程序件 1 分，即使板块/事件配额全空也不得入选
        assert select([low]) == []


# ---------- 多样性控制与真实样本集成 ----------

class TestSelection:
    def test_真实样本选出报告同款6条(self, picks):
        companies = {p["secu_abbr"] for p in picks}
        assert companies == EXPECTED_COMPANIES

    def test_选题元数据(self, picks):
        by = {p["secu_abbr"]: p for p in picks}
        assert by["苏农银行"]["score"] == 12 and by["苏农银行"]["sector"] == "银行"
        assert by["瑞丰银行"]["score"] == 12
        assert by["*ST萃华"]["score"] == 9 and by["*ST萃华"]["event_type"] == "重大风险"
        assert by["建设银行"]["score"] == 6 and by["建设银行"]["event_type"] == "发债完成"
        assert by["福莱特"]["event_type"] == "回购注销"
        assert by["鼎际得"]["event_type"] == "回购注销"

    def test_板块与事件配额(self, picks):
        from collections import Counter
        sec = Counter(p["sector"] for p in picks)
        ev = Counter(p["event_type"] for p in picks)
        assert all(v <= funnel.MAX_PER_SECTOR for v in sec.values())
        assert all(v <= funnel.MAX_PER_EVENT for v in ev.values())

    def test_全部达到最低分(self, picks):
        assert all(p["score"] >= funnel.MIN_SCORE for p in picks)


# ---------- 细分行业赛道分类 ----------

def _mk(abbr, title, summary=""):
    return {"secu_abbr": abbr, "info_title": title, "info_summary": summary}


class TestNewSectors:
    """每赛道至少 1 正例 + 关键误伤负例（负例即注释里点名的误命中案例）。"""

    @pytest.mark.parametrize("abbr,title,expect", [
        ("贵州茅台", "贵州茅台2026年中期利润分配方案公告", "白酒"),
        ("五粮液", "五粮液关于2026年半年度报告的公告", "白酒"),
        ("中芯国际", "中芯国际关于晶圆产能建设进展的公告", "半导体"),
        ("长电科技", "长电科技关于封测基地投产的公告", "半导体"),
        ("新易盛", "新易盛关于回购注销部分限制性股票的公告", "光模块"),  # 简称主体词命中
        ("光迅科技", "光迅科技关于光通信器件项目的公告", "光模块"),
        ("隆基绿能", "隆基绿能关于硅片价格调整的公告", "光伏"),
        ("阳光电源", "阳光电源关于光伏逆变器扩产的公告", "光伏"),
        ("宁德时代", "宁德时代关于锂电池扩产的公告", "新能源"),
        ("金风科技", "金风科技关于海上风电项目中标的公告", "新能源"),
        ("航天电子", "航天电子关于卫星配套产品的公告", "航天"),
        ("中国卫通", "中国卫通关于火箭发射安排的公告", "航天"),
        ("科前生物", "科前生物关于回购注销公司2025年员工持股计划的公告", "医药"),
        ("恒瑞医药", "恒瑞医药关于创新药获批的公告", "医药"),
        ("中信证券", "中信证券关于自营业务情况的公告", "证券"),
        ("新华保险", "新华保险关于保费收入的公告", "保险"),
        ("中国平安", "中国平安关于回购股份进展的公告", "保险"),
        ("湖南黄金", "湖南黄金关于披露重组草案暨股票复牌的公告", "有色金属"),
        ("江西铜业", "江西铜业关于铜矿资源的公告", "有色金属"),
        ("比亚迪", "比亚迪关于新能源汽车产销快报的公告", "汽车"),
        ("万科A", "万科A关于新增土地储备的公告", "地产"),
        ("保利发展", "保利发展关于置业项目开盘的公告", "地产"),
    ])
    def test_赛道正例(self, abbr, title, expect):
        assert classify_sector(_mk(abbr, title)) == expect

    @pytest.mark.parametrize("abbr,title", [
        ("骆驼股份", "骆驼股份关于蓄电池回收处理的公告"),      # "电池"≠"锂电"，铅酸电池不入新能源
        ("锦江酒店", "锦江酒店关于加盟店发展的公告"),          # "酒店"≠"酒业"（单字"酒"不收）
        ("酒钢宏兴", "酒钢宏兴关于钢材产量的公告"),            # 酒钢是钢铁，"酒"单字不收
        ("中国国航", "中国国航关于购买飞机的公告"),            # 航空≠航天
        ("三安光电", "三安光电关于LED外延片的公告"),           # "光电"≠光模块（它是LED/半导体）
        ("永安期货", "永安期货股份有限公司关于董事长离任的公告"),  # 期货不在点名赛道，落"其他"
    ])
    def test_误伤负例(self, abbr, title):
        assert classify_sector(_mk(abbr, title)) == "其他"

    def test_证券中介语境不误伤(self, sample):
        """中广核技定增保荐书标题含'华泰联合证券'，但它是核技术公司不是券商。"""
        ann = _find(sample, "中广核技", "发行保荐书")
        assert "证券" in ann["info_title"]
        assert classify_sector(ann) == "其他"

    def test_珠宝零售不误入有色(self, sample):
        """*ST萃华 全称'萃华金银珠宝'，是珠宝零售商不是有色采选（不收'金银/珠宝'）。"""
        ann = _find(sample, "*ST萃华", "第六次")
        assert "金银珠宝" in ann["info_summary"]
        assert classify_sector(ann) == "其他"

    def test_银行优先于保险(self):
        # "平安"是保险赛道词，但平安银行必须先被银行赛道截获
        assert classify_sector(_mk("平安银行", "平安银行关于发行完毕的公告")) == "银行"

    def test_新赛道不加成(self):
        # 银行 +2 是产品决策；新赛道（如有色金属）不享受加成
        bank = score_announcement(_mk("苏农银行", "苏农银行2026年中期利润分配方案公告"))
        gold = score_announcement(_mk("湖南黄金", "湖南黄金2026年中期利润分配方案公告"))
        assert bank["score"] == 12 and gold["score"] == 10
