# -*- coding: utf-8 -*-
"""确定性预检（在 LLM 校对之前先跑，预检不过直接打回，不浪费 LLM 调用）。

机制性消除的两道闸：
  1. 数字原值核对：人话稿里每个数字必须能在公告原文（标题/摘要/事件字段/
     发布日期等事实基准）中找到完全一致的原值 —— 针对 A 类数字错。
     例外：规范允许的换算派生数（股→万股、每10股→每股、1000股到手算例）
     放行，换算正确性由 Checker 验算（见 _is_derived_number）。
  2. 禁词检查：倾向性话术关键词 —— 针对 C 类错误。

返回结构同时给出核对点统计（供准确率日报使用）。
"""
import re

from compliance import find_banned

_NUM_PAT = re.compile(r"\d+(?:\.\d+)?")
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 规范允许的换算派生数（与原文数字成 10 的整数倍关系，≤4 个数量级）：
# 股→万股（140000股→14万股）、每10股→每股（每10股派1元→每股0.1元）、
# 到手算例（1000股×每股派现）。仅放行数字闸，换算正确性仍由 Checker 验算。
_DERIVED_FACTORS = (10, 100, 1000, 10000)

# D 类：关键限定词不得简化。原文含完整术语、稿中只出现简化形式 → 打回。
# （回归用例：v1 曾把"归母净利润"简化为"净利润" ×2）
KEY_QUALIFIER_PAIRS = [
    ("归母净利润", "净利润"),
    ("扣非净利润", "净利润"),
]

# B 类启发式：分句低于该长度不强求锚点
_CLAUSE_MIN_LEN = 6


def normalize(text: str) -> str:
    """归一化：全角数字转半角，去空白与千分位逗号，便于子串比对。"""
    text = (text or "").translate(_FW_DIGITS)
    return re.sub(r"[\s,，]+", "", text)


def extract_numbers(text: str) -> list:
    """提取文本中的全部数字 token（含小数）。"""
    return _NUM_PAT.findall((text or "").translate(_FW_DIGITS))


def fact_base(ann: dict) -> str:
    """事实基准：公告标题 + 摘要 + 事件结构化字段 + 发布日期 + 证券简称/代码。"""
    parts = [
        ann.get("info_title") or "",
        ann.get("info_summary") or "",
        ann.get("info_event_txt") or "",
        ann.get("info_publ_date") or "",
        ann.get("secu_abbr") or "",
        ann.get("secu_code") or "",
    ]
    return normalize("".join(parts))


def draft_text(draft: dict) -> str:
    return (draft.get("line1") or "") + (draft.get("line2") or "") + (draft.get("line3") or "")


def _is_derived_number(num: str, base_norm: str) -> bool:
    """换算派生数白名单：num 与事实基准中某数字成 10 的整数倍关系（≤4 个数量级）。

    覆盖规范允许的三种写法：股→万股、每10股派X元→每股X/10元、
    到手算例（固定 1000 股 × 每股派现）。只是预检放行，是否算对由 Checker 验算。
    """
    try:
        v = float(num)
    except ValueError:
        return False
    for b in extract_numbers(base_norm):
        try:
            bv = float(b)
        except ValueError:
            continue
        if bv == 0:
            continue
        for k in _DERIVED_FACTORS:
            if abs(v - bv * k) < 1e-6 or abs(v * k - bv) < 1e-6:
                return True
    return False


def _check_qualifiers(draft: dict, ann: dict) -> list:
    """D 类：关键限定词简化检测。原文含完整术语而稿中只用简化形式 → 错误。"""
    source = (ann.get("info_title") or "") + (ann.get("info_summary") or "")
    text = draft_text(draft)
    errors = []
    for full, simplified in KEY_QUALIFIER_PAIRS:
        if full in source and simplified in text and full not in text:
            errors.append({
                "类型": "D遗漏关键限定",
                "位置": simplified,
                "问题": f"原文为「{full}」，稿中简化为「{simplified}」，关键限定被省略",
            })
    return errors


def find_unanchored_clauses(draft: dict, ann: dict) -> list:
    """B 类启发式：找出"原文中不存在锚点"的连续长句，进 Checker 重点关注清单。

    line1/line2 按句号/逗号/分号切分后，每个分句需在原文有锚点才算有出处：
    数字锚点（分句里的数字能在事实基准找到）、专名锚点（含公司简称/代码）、
    或连续 4 字子串锚点。无锚点的分句疑似无中生有/额外判断
    （回归用例："监管要求的安全垫工具"、"和普通储户没关系"）。
    这是启发式提示，不直接判打回。
    """
    base = fact_base(ann)
    abbr = (ann.get("secu_abbr") or "").replace("*", "")
    watch = []
    for line_key in ("line1", "line2"):
        for clause in re.split(r"[。，；！？,.;!?]", draft.get(line_key) or ""):
            clause = clause.strip()
            if len(clause) < _CLAUSE_MIN_LEN:
                continue
            if _has_anchor(clause, base, abbr):
                continue
            watch.append(clause)
    return watch


def _has_anchor(clause: str, base: str, abbr: str) -> bool:
    # 专名锚点
    if abbr and abbr in clause:
        return True
    # 数字锚点
    if any(normalize(n) in base for n in extract_numbers(clause)):
        return True
    # 连续 4 字子串锚点
    cn = normalize(clause)
    return any(cn[i:i + 4] in base for i in range(len(cn) - 3))


def check(draft: dict, ann: dict) -> dict:
    """对一版人话稿做预检。

    返回 {ok, errors, checkpoints, passed, watchlist}：
      errors     错误清单 [{类型, 位置, 问题}]（A数字错 / C倾向性话术 / D遗漏关键限定）
      checkpoints 核对点总数（每个数字 1 点 + 禁词检查 1 点 + 每个关键限定 1 点）
      passed     通过的核对点数
      watchlist  无锚点分句清单（启发式，供 Checker 重点关注，不判打回）
    """
    base = fact_base(ann)
    errors, checkpoints, passed = [], 0, 0

    # 1) 数字原值核对：每个数字必须能在事实基准中找到原值（或属规范允许的换算派生数）
    for num in extract_numbers(draft_text(draft)):
        checkpoints += 1
        if normalize(num) in base or _is_derived_number(num, base):
            passed += 1
        else:
            errors.append({
                "类型": "A数字错",
                "位置": num,
                "问题": f"数字「{num}」在公告原文中找不到完全一致的原值",
            })

    # 2) 禁词检查（C 类）
    checkpoints += 1
    banned = find_banned(draft_text(draft))
    if not banned:
        passed += 1
    else:
        for w in banned:
            errors.append({
                "类型": "C倾向性话术",
                "位置": w,
                "问题": f"出现禁词「{w}」",
            })

    # 3) 关键限定词（D 类）
    qual_errors = _check_qualifiers(draft, ann)
    checkpoints += 1
    if not qual_errors:
        passed += 1
    else:
        errors.extend(qual_errors)

    return {"ok": not errors, "errors": errors,
            "checkpoints": checkpoints, "passed": passed,
            "watchlist": find_unanchored_clauses(draft, ann)}
