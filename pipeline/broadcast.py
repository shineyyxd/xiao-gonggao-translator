# -*- coding: utf-8 -*-
"""口播稿生成：文字稿 → 老人能听懂的朗读文本（纯规则转换，不用 LLM）。

口播规范（产品定稿 2026-08-31）：
  绝不念：链接/网址、公告全名长串（出处改念"公告原文见文字版"）、英文/编号
         （唯一例外：*ST 念 "ST"，星号不念）。
  必须改写：
  - 数字口语化但**保精度**：小数逐位读（18.119元→十八点一一九元）；
    百分比读"百分之X"（1.68%→百分之一点六八）；年份逐位读（2026年→二零二六年）；
    千分位逗号去掉后按整值读（884,742千元→八十八万四千七百四十二千元）；
    0.10元/0.1元 → "一毛钱"（与原文严格等价才用，不做任何换算）。
  - 括号改停顿；冒号去掉；"第X条"序号用中文数字。
精度红线：只做"同一个数的读法变化"，绝不换算数值（换算是 Writer 的事，
且 Writer 侧有 precheck/Checker 双闸；这里若读错数，质检在口播稿全文自查）。
"""
import logging
import re

log = logging.getLogger(__name__)

_DIGITS = "零一二三四五六七八九"
_GROUP_UNITS = ("", "万", "亿", "万亿")

# 口播开场白/结尾：全期各只念一次（合规话术不再逐条念）
OPENING_TEMPLATE = ("这里是小公告翻译官。今天是{date_cn}。"
                    "早报只说公告里的事实，不做任何投资建议。")
CLOSING = "今天的内容就到这里。本内容仅为公告信息整理，不构成投资建议。"
ITEM_SOURCE = "公告原文见文字版。"


def _four_to_cn(n: int) -> str:
    """0~9999 → 中文读法（10~19 读'十X'，如 15→十五）。"""
    if n == 0:
        return ""
    if n < 10:
        return _DIGITS[n]
    if n < 20:
        return "十" + (_DIGITS[n % 10] if n % 10 else "")
    s, zero = "", False
    for d, u in zip(((n // 1000) % 10, (n // 100) % 10, (n // 10) % 10, n % 10),
                    ("千", "百", "十", "")):
        if d == 0:
            zero = True
        else:
            if zero and s:
                s += "零"
            zero = False
            s += _DIGITS[d] + u
    return s


def _int_to_cn(n: int) -> str:
    """非负整数 → 中文读法（含万/亿分组与组间补零）。如 884742→八十八万四千七百四十二。"""
    if n == 0:
        return "零"
    groups = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    parts = []
    for gi in range(len(groups) - 1, -1, -1):
        g = groups[gi]
        if g == 0:
            continue
        if parts and g < 1000:  # 低组不满千且前面有非零组 → 中间补零
            parts.append("零")
        parts.append(_four_to_cn(g) + _GROUP_UNITS[gi])
    return re.sub(r"零+$", "", "".join(parts))


def _spoken_number(s: str) -> str:
    """一个数字字符串 → 口语读法。整数按规则读，小数点后面**逐位**读（保精度）。"""
    s = s.replace(",", "")
    if "." in s:
        ip, fp = s.split(".", 1)
        return _int_to_cn(int(ip)) + "点" + "".join(_DIGITS[int(d)] for d in fp)
    return _int_to_cn(int(s))


# 匹配顺序有讲究：年份 > 千分位 > 带百分号/小数的普通数
_NUM_PAT = re.compile(r"\d{4}(?=年)|\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?%?")
_URL_PAT = re.compile(r"https?://\S+")
_LATIN_PAT = re.compile(r"[A-Za-z]+")


def _strip_latin(m: re.Match) -> str:
    w = m.group(0)
    if w == "ST":  # *ST 的唯一例外：念 "ST"（星号已在上游去掉）
        return w
    log.warning("口播稿剔除英文词：%s", w)
    return ""


def _replace_number(m: re.Match) -> str:
    tok = m.group(0)
    if re.fullmatch(r"\d{4}", tok) and m.string[m.end():m.end() + 1] == "年":
        return "".join(_DIGITS[int(d)] for d in tok)  # 年份逐位读
    if tok.endswith("%"):
        return "百分之" + _spoken_number(tok[:-1])
    return _spoken_number(tok)


def to_spoken(text: str) -> str:
    """文字稿 → 口播文本：数字中文化（保精度）、去括号/冒号、*ST 去星、英文清除。"""
    t = text or ""
    t = _URL_PAT.sub("", t)  # 链接整体不念（先于英文清除，避免残留 :// 碎片）
    t = t.replace("*ST", "ST")
    t = re.sub(r"0\.10*元", "一毛钱", t)  # 严格等价的口语说法，须先于通用数字转换
    t = _NUM_PAT.sub(_replace_number, t)
    # 残余英文（除 ST 外）一律不念：整词匹配替换，避免误伤其他词中的字母
    t = _LATIN_PAT.sub(_strip_latin, t)
    t = t.replace("（", "，").replace("）", "").replace("：", "")
    t = re.sub(r"[，。、]{2,}", "，", t)      # 标点叠压收敛
    t = re.sub(r"\s+", " ", t).strip("， ")
    return t


def spoken_date_cn(run_date: str) -> str:
    """2026-08-26 → 二零二六年八月二十六日。"""
    try:
        y, m, d = run_date[:10].split("-")
        year = "".join(_DIGITS[int(c)] for c in y)
        return f"{year}年{_int_to_cn(int(m))}月{_int_to_cn(int(d))}日"
    except (ValueError, IndexError):
        return run_date


def item_script(index: int, item: dict) -> str:
    """单条口播稿：第X条 + line1 + line2（不念出处长名，改念固定句）。"""
    body = to_spoken(f"{item.get('line1', '')} {item.get('line2', '')}")
    return f"第{_int_to_cn(index)}条。{body}。{ITEM_SOURCE}".replace("。。", "。")


def issue_script(run_date: str, items: list) -> str:
    """整期口播稿：开场白 + 各条（条间换行停顿）+ 结尾。"""
    parts = [OPENING_TEMPLATE.format(date_cn=spoken_date_cn(run_date))]
    parts += [item_script(i, it) for i, it in enumerate(items, 1)]
    parts.append(CLOSING)
    return "\n\n".join(parts)
