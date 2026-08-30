# -*- coding: utf-8 -*-
"""确定性合规过滤：机制性消除 C 类错误（倾向性话术），不依赖模型能力。

两处用途：
  1. 漏斗层 should_drop()：公告标题/摘要本身带荐股话术 → 整条丢弃；
  2. 产出层 find_banned()：检查生成的人话稿是否含禁词（precheck 与校对都会用）。
"""
from config import DISCLAIMER

# 人话稿禁词表（出现任一即不合规）
BANNED_WORDS = [
    "建议", "值得关注", "利好", "利空", "可以考虑",
    "买入", "卖出", "目标价", "强烈推荐", "推荐买入",
    "抄底", "加仓", "减仓",
]

# 漏斗层丢弃词：公告原文含荐股类话术 → 整篇不选
FUNNEL_DROP_WORDS = [
    "买入评级", "目标价", "强烈推荐", "推荐买入", "建议买入", "抄底",
]


def _strip_disclaimer(text: str) -> str:
    """固定话术'不构成投资建议'本身含'建议'二字，检查前先剔除。"""
    return (text or "").replace(DISCLAIMER, "")


def find_banned(text: str) -> list:
    """返回文本中命中的禁词列表（已剔除固定合规话术）。"""
    body = _strip_disclaimer(text)
    return [w for w in BANNED_WORDS if w in body]


def is_clean(text: str) -> bool:
    return not find_banned(text)


def should_drop(announcement: dict) -> bool:
    """漏斗层合规过滤：标题或摘要含荐股话术则丢弃。"""
    blob = (announcement.get("info_title") or "") + (announcement.get("info_summary") or "")
    return any(w in blob for w in FUNNEL_DROP_WORDS)
