# -*- coding: utf-8 -*-
"""PDF 摘要抓取：对入选公告（漏斗之后、生成之前，只抓入选的几条）下载 PDF，
提取首页（最多前 2 页）文本，句子级抽取关键段落补进 info_summary。

合规边界（重要）：摘要只能是 PDF 原文的**抽取**——整句摘录、不改一字，
不做任何概括/改写/推断（改写是 Writer 的事，Writer 产出有独立的事实红线：
precheck 数字闸 + Checker 校对兜底）。

降级语义：pypdf 未安装 / 下载失败 / 解析失败 / 无可用文本，都只让该条
info_summary 留空（退化为标题级输入），绝不让单条 PDF 失败搞挂整期。
"""
import io
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 20   # 单条下载超时（秒）
DOWNLOAD_RETRIES = 2    # 失败重试 2 次（共 3 次尝试）
RETRY_WAIT = 2          # 重试间隔（秒）
POLITE_DELAY = 1.0      # PDF 下载间隔 ≥1s，礼貌抓取
MAX_PDF_PAGES = 2       # 最多取前 2 页
MAX_SUMMARY_LEN = 500   # 摘要上限（字）
MIN_PAGE1_LEN = 200     # 首页文本短于此长度才补第 2 页
MIN_SENT_LEN = 8        # 短于此长度的句子视为碎片不入选

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

_SENT_SPLIT = re.compile(r"(?<=[。！？；])|\n+")
_HAS_DIGIT = re.compile(r"\d")


def fetch_pdf_text(url: str, timeout: int = DOWNLOAD_TIMEOUT,
                   retries: int = DOWNLOAD_RETRIES) -> str:
    """下载 PDF 并提取前 1~2 页纯文本。重试耗尽仍失败则抛异常（上层兜底降级）。"""
    import pypdf  # 延迟导入：未安装时由 fill_summaries 统一降级
    last = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            reader = pypdf.PdfReader(io.BytesIO(resp.content))
            text = (reader.pages[0].extract_text() or "") if reader.pages else ""
            if len(text) < MIN_PAGE1_LEN and len(reader.pages) > 1:
                text += "\n" + (reader.pages[1].extract_text() or "")
            return text.replace("\x0c", " ").strip()
        except Exception as e:  # 网络失败与解析失败统一重试
            last = e
            log.warning("PDF 获取失败（第%d次）：%s", attempt + 1, e)
            if attempt < retries:
                time.sleep(RETRY_WAIT)
    raise last


def extract_summary(text: str, max_len: int = MAX_SUMMARY_LEN) -> str:
    """句子级抽取（纯摘录，不改写）：含数字的句子优先（同类保持原文顺序），
    其余句子按原文顺序补足，总长 ≤max_len。完全相同的句子去重。"""
    sents, seen = [], set()
    for s in _SENT_SPLIT.split(text or ""):
        s = re.sub(r"\s+", " ", s).strip()  # 只归一化空白，不动文字与数字
        if len(s) < MIN_SENT_LEN or s in seen:
            continue
        seen.add(s)
        sents.append(s)
    digit = [s for s in sents if _HAS_DIGIT.search(s)]
    plain = [s for s in sents if not _HAS_DIGIT.search(s)]
    out, total = [], 0
    for s in digit + plain:
        if total + len(s) > max_len:
            continue  # 超长句跳过，排后面的短句仍可入选
        out.append(s)
        total += len(s)
    return "".join(out)


def fill_summaries(announcements: list, tracer=None) -> dict:
    """给 info_summary 为空的入选公告补 PDF 原文摘要（就地写入该字段）。

    tracer: 审计记录器，每条记 agent=pdf_summary（输入=链接，输出=摘要字数，
    结论 OK/FAIL）。返回统计 {尝试, 成功, 失败, 已有摘要跳过}。
    """
    targets = [a for a in announcements if not (a.get("info_summary") or "").strip()]
    stats = {"尝试": 0, "成功": 0, "失败": 0,
             "已有摘要跳过": len(announcements) - len(targets)}
    if not targets:
        return stats
    try:
        import pypdf  # noqa: F401
    except ImportError:
        log.warning("pypdf 未安装，PDF 摘要整体跳过（pip install pypdf 后可用）")
        if tracer:
            tracer.record("pdf_summary", output_summary="pypdf 未安装",
                          conclusion="SKIPPED")
        return stats
    for i, ann in enumerate(targets):
        if i:
            time.sleep(POLITE_DELAY)
        stats["尝试"] += 1
        url = ann.get("announcement_link") or ""
        try:
            summary = extract_summary(fetch_pdf_text(url))
            if not summary:
                raise ValueError("PDF 无可用文本")
            ann["info_summary"] = summary
            stats["成功"] += 1
            if tracer:
                tracer.record("pdf_summary", item_id=ann.get("id"),
                              input_summary=url,
                              output_summary=f"摘要 {len(summary)} 字",
                              conclusion="OK")
        except Exception as e:  # 单条失败只影响该条
            stats["失败"] += 1
            log.warning("[%s] PDF 摘要失败（info_summary 留空降级）：%s",
                        ann.get("secu_abbr"), e)
            if tracer:
                tracer.record("pdf_summary", item_id=ann.get("id"),
                              input_summary=url, output_summary=str(e)[:120],
                              conclusion="FAIL", retries=DOWNLOAD_RETRIES)
    return stats
