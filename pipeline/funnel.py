# -*- coding: utf-8 -*-
"""三层筛选漏斗（严格按第一期测试报告实现，含 3 个已修复缺陷）。

  ① 板块分类：银行 / 公用事业 / 宏观 / 证券 / 保险 / 细分赛道（白酒/半导体/光模块/
     光伏/新能源/航天/医药/有色金属/汽车/地产）/ 其他
     银行/公用事业/宏观 匹配 secu_abbr + info_title + info_summary；
     新赛道只匹配 简称+标题（与 live 模式行为一致，证券/保险仅简称防中介语境误伤）；
     "银行"需排除"银行账户/银行存款"等误命中语境（否则 *ST萃华 这类
     摘要里带"银行账户被冻结"的公司会被错划进银行板块）。
  ② 重要性打分（只认 info_title 标题级命中 —— 缺陷1的修复）：
     分红派息10 / 业绩9 / 重大风险9 / 回购注销6 / 发债完成4 / 人事变动4 /
     程序件压到1 / 其他0；银行板块 +2。
  ③ 合规过滤 + 多样性控制：
     荐股话术直接丢弃；每板块≤3；每事件类型≤2；
     同公司同主题合并（缺陷2的修复）；最低分门槛 ≥4（缺陷3的修复，禁止低分填充）。

排序：按分数降序的稳定排序（同分保持数据文件原始顺序），再按配额贪心入选。
"""
import re

from compliance import should_drop

# ---- 板块关键词 ----
_BANK_FALSE_POS = re.compile(
    r"银行账户|银行存款|银行承兑|贷款银行|存款银行|开户银行|银行同期|银行业金融机构"
    r"|银行贷款|银行借款|银行授信|银行汇票|银行保函")
_BANK_PAT = re.compile(r"银行|农商行")
_UTIL_PAT = re.compile(r"电力|水务|燃气|核电|公用事业|供水|污水")
_MACRO_PAT = re.compile(r"央行|人民银行|国务院|财政部|发改委|统计局")

# ---- 细分行业赛道 ----
# 已知局限：live 模式没有摘要，新赛道只能靠 简称+标题 分类（已实测确认巨潮公告
# 查询接口与证券列表接口均无行业字段可用）；公司名不含行业词时仍会落入"其他"
# （如 中际旭创 是光模块、福莱特 是光伏玻璃，名字都不带）——宁漏不误。
# 证券/保险只匹配简称：公告标题里的"XX证券/XX保险 关于…"是中介机构语境
# （误命中案例：中广核技定增保荐书标题含"华泰联合证券"，它不是券商）。
# 期货不在点名赛道内（永安期货等落"其他"）——若归入证券赛道会改变 0826 期
# 既定选题（永安期货 4 分人事变动将从"其他"配额中释放），保持回归口径一致。
_SECURITIES_PAT = re.compile(r"证券|券商")
_INSURANCE_PAT = re.compile(r"保险|人寿|人保|太保|平安")  # "平安"依赖银行先判（平安银行→银行）

# 其余赛道匹配 简称+标题（不用 summary：避免摘要里业务内容词误伤，且与 live 模式行为一致）
_TRACK_PATTERNS = [
    # "酒"单字不收（酒店/酒席误伤）；"舍得"不收（"舍不得"是普通词）
    ("白酒", re.compile(r"白酒|酒业|茅台|五粮液|泸州老窖|汾酒|洋河|古井贡|今世缘|水井坊|酒鬼|酱香")),
    # "硅片"让给光伏（光伏硅片用量大）：半导体硅片公司标题无"半导体"时会误入光伏，可接受
    ("半导体", re.compile(r"半导体|芯片|集成电路|晶圆|封测|光刻|存储器|存储芯片")),
    # "光"字太宽泛不收；"光电"不收（三安光电是 LED/半导体）；中际旭创这类名字
    # 不带行业词的落"其他"（已知局限，见上）；"光组件"会被光伏的"组件"截走，词序在此
    ("光模块", re.compile(r"光模块|光通信|光器件|CPO|光迅|新易盛|天孚通信")),
    # "组件"可能误命中电子/汽车零部件的组件语境，公告标题里概率低，接受
    ("光伏", re.compile(r"光伏|太阳能|多晶硅|硅料|硅片|组件|电池片|逆变器")),
    # 用"锂电"不用"电池"（误命中案例：骆驼股份这类铅酸蓄电池公司不是锂电赛道）；
    # 排在"汽车"前——车企公告含"动力电池"时会被拉进本赛道，可接受
    ("新能源", re.compile(r"锂电|动力电池|储能|风电|风能|锂矿|锂业|正极|负极|电解液|隔膜|三元材料|磷酸铁锂|充电桩")),
    # 不收"航空"（民航/军机是另一行业）
    ("航天", re.compile(r"航天|卫星|火箭|宇航|北斗|低轨|空间站")),
    # "生物"是宽词：兽药/生物制品（科前生物）、生物识别、农业生物都会命中，
    # 可接受（医药本就走宽赛道，注释备查）
    ("医药", re.compile(r"医药|药业|制药|生物|医疗|器械|药品|疫苗|中药|创新药|原料药")),
    # 不收"金银/珠宝/矿业"（误命中案例：萃华金银珠宝是珠宝零售商，不是有色采选）；
    # 不收"冶炼"（钢铁是黑色金属）
    ("有色金属", re.compile(r"有色金属|黄金|金矿|铜|铝|锌|铅|镍|锡|稀土|钨|钼")),
    ("汽车", re.compile(r"汽车|车企|整车|客车|重卡|商用车|乘用车|汽配")),
    # "物业"归地产产业链（物管公司多为地产关联），可接受
    ("地产", re.compile(r"地产|房地产|置业|物业|万科|保利发展|招商蛇口|金地|龙湖|碧桂园")),
]

# ---- 事件打分关键词（均只匹配 info_title）----
# 程序件：纯程序性文件，压到 1 分（优先判定并短路）
_PROCEDURAL = ["法律意见书", "保荐书", "问询", "回复", "声明与承诺", "核查意见",
               "股东大会通知", "会议通知", "审计报告", "评估报告", "独立财务顾问",
               "鉴证报告", "招股说明书", "上市公告书", "管理办法", "管理制度",
               "议事规则", "章程"]
_RISK = ["终止上市", "退市风险", "风险提示", "风险警示", "立案调查", "账户冻结"]
_DIVIDEND = ["分红", "利润分配", "派息", "权益分派"]
_PERFORMANCE = ["业绩预告", "业绩快报", "净利润"]  # 业绩类只认标题级（缺陷1）
_BUYBACK = ["回购注销"]
_BOND = ["发行完毕", "发行完成"]
_HR = ["选举", "聘任", "辞职", "任职资格", "董事长", "总经理", "离任"]

SCORE_RULES = {
    "分红派息": 10, "业绩": 9, "重大风险": 9, "回购注销": 6,
    "发债完成": 4, "人事变动": 4, "程序件": 1, "其他": 0,
}

MIN_SCORE = 4           # 最低分门槛（缺陷3：禁止低分填充）
MAX_PER_SECTOR = 3      # 每板块条数上限
MAX_PER_EVENT = 2       # 每事件类型条数上限
BANK_BONUS = 2          # 银行板块加分


def classify_sector(ann: dict) -> str:
    """板块分类：银行 / 公用事业 / 宏观 / 证券 / 保险 / 细分赛道 / 其他。

    判定优先级：银行（带误判排除，最高）→ 公用事业 → 宏观 → 证券/保险（仅简称）
    → 细分赛道（简称+标题，按表序先匹配先赢）→ 其他。
    新赛道一律不加成（银行 +2 是产品决策，保留）。
    """
    blob = (ann.get("secu_abbr") or "") + (ann.get("info_title") or "") + (ann.get("info_summary") or "")
    bank_blob = _BANK_FALSE_POS.sub("", blob)
    if _BANK_PAT.search(bank_blob):
        return "银行"
    if _UTIL_PAT.search(blob):
        return "公用事业"
    if _MACRO_PAT.search(blob):
        return "宏观"
    abbr = ann.get("secu_abbr") or ""
    if _SECURITIES_PAT.search(abbr):
        return "证券"
    if _INSURANCE_PAT.search(abbr):
        return "保险"
    head = abbr + (ann.get("info_title") or "")
    for name, pat in _TRACK_PATTERNS:
        if pat.search(head):
            return name
    return "其他"


def score_event(title: str) -> tuple:
    """按标题打重要性分，返回 (分数, 事件类型)。

    只认 info_title 标题级命中：标题是"定增保荐书"但 tag 含"业绩预告"的，
    应判程序件 1 分而不是业绩 9 分（缺陷1）。
    """
    title = title or ""
    if any(k in title for k in _PROCEDURAL):
        return SCORE_RULES["程序件"], "程序件"
    if any(k in title for k in _RISK):
        return SCORE_RULES["重大风险"], "重大风险"
    if any(k in title for k in _DIVIDEND):
        return SCORE_RULES["分红派息"], "分红派息"
    if any(k in title for k in _PERFORMANCE):
        return SCORE_RULES["业绩"], "业绩"
    if any(k in title for k in _BUYBACK) or ("回购" in title and "注销" in title):
        return SCORE_RULES["回购注销"], "回购注销"
    if any(k in title for k in _BOND):
        return SCORE_RULES["发债完成"], "发债完成"
    if any(k in title for k in _HR):
        return SCORE_RULES["人事变动"], "人事变动"
    return SCORE_RULES["其他"], "其他"


def score_announcement(ann: dict) -> dict:
    """给公告打分并附加 score / sector / event_type 元数据。"""
    score, event_type = score_event(ann.get("info_title"))
    sector = classify_sector(ann)
    if sector == "银行":
        score += BANK_BONUS
    out = dict(ann)
    out.update({"score": score, "sector": sector, "event_type": event_type})
    return out


def merge_same_theme(candidates: list) -> list:
    """同公司同主题合并（缺陷2）：同 secu_code + 同事件类型只留一条。

    保留分数最高的；同分取发布日期最新的；再同则取文件中靠前的。
    例：*ST萃华 第六次、第七次风险提示只留一条。
    """
    best = {}
    order = []
    for c in candidates:
        key = (c.get("secu_code"), c["event_type"])
        if key not in best:
            best[key] = c
            order.append(key)
        else:
            cur = best[key]
            new_is_better = (
                c["score"] > cur["score"]
                or (c["score"] == cur["score"]
                    and (c.get("info_publ_date") or "") > (cur.get("info_publ_date") or ""))
            )
            if new_is_better:
                best[key] = c
    return [best[k] for k in order]


def select(announcements: list,
           min_score: int = MIN_SCORE,
           max_per_sector: int = MAX_PER_SECTOR,
           max_per_event: int = MAX_PER_EVENT,
           reported: set = None) -> list:
    """三层漏斗主入口：打分 → 合规过滤 → 合并 → 跨天去重 → 门槛 → 配额贪心入选。

    reported: Memory 提供的"近 7 天已报道 (secu_code, event_type)"集合（可选），
    在同公司同主题合并之后做跨天去重——命中且非重大风险类的剔除
    （重大风险如退市风险提示需要连续提醒，豁免；业务理由见 memory.py）。
    """
    scored = [score_announcement(a) for a in announcements]
    # 合规过滤：荐股类话术直接丢弃
    scored = [c for c in scored if not should_drop(c)]
    # 同公司同主题合并
    merged = merge_same_theme(scored)
    # 跨天去重（Memory 能力1，重大风险豁免）
    if reported:
        merged = [c for c in merged
                  if (c.get("secu_code"), c["event_type"]) not in reported
                  or c["event_type"] == "重大风险"]
    # 最低分门槛（缺陷3）：即使板块配额没凑满，<min_score 也不选入
    eligible = [c for c in merged if c["score"] >= min_score]
    # 稳定排序：分数降序，同分保持原始顺序
    ranked = sorted(eligible, key=lambda c: -c["score"])
    picks, sector_cnt, event_cnt = [], {}, {}
    for c in ranked:
        if sector_cnt.get(c["sector"], 0) >= max_per_sector:
            continue
        if event_cnt.get(c["event_type"], 0) >= max_per_event:
            continue
        picks.append(c)
        sector_cnt[c["sector"]] = sector_cnt.get(c["sector"], 0) + 1
        event_cnt[c["event_type"]] = event_cnt.get(c["event_type"], 0) + 1
    return picks
