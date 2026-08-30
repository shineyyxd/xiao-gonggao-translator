# -*- coding: utf-8 -*-
"""数据层：公告（核心）+ 快讯（噪音层）+ 热点新闻。

akshare 行情背景为可选导入：没装或拉取失败就跳过，不影响主流程。
"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from config import DATA_DIR

log = logging.getLogger(__name__)


def load_announcements(data_dir: Path = DATA_DIR) -> list:
    """加载公告样本（list[dict]，字段见 README）。"""
    path = Path(data_dir) / "公告样本_0822-0826.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_flash_news(data_dir: Path = DATA_DIR) -> list:
    """加载股票快讯（噪音层，本期只做接入验证，不进生成流程）。"""
    path = Path(data_dir) / "股票快讯_0825-0826.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_hot_news(data_dir: Path = DATA_DIR) -> str:
    """加载市场热点原始文本。"""
    path = Path(data_dir) / "热点新闻_0824-0826.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def dedupe(announcements: list) -> list:
    """按 (证券代码, 标题, 链接) 去重，保留首次出现（保持原始顺序）。"""
    seen, out = set(), []
    for a in announcements:
        key = (a.get("secu_code"), a.get("info_title"), a.get("announcement_link"))
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def filter_by_window(announcements: list, run_date: str, window_days: int = 5) -> list:
    """按日期窗口过滤：取不晚于 run_date、且不早于 run_date-(window_days-1) 的公告。

    早报覆盖"最近几天"的公告而不是只取当日——首日测试的 6 条选题即来自
    08-25/08-26 两个日期。样本实际只含 08-25 ~ 08-26 的数据。
    """
    end = date.fromisoformat(run_date)
    start = end - timedelta(days=window_days - 1)
    out = []
    for a in announcements:
        try:
            d = date.fromisoformat((a.get("info_publ_date") or "")[:10])
        except ValueError:
            continue
        if start <= d <= end:
            out.append(a)
    return out


def market_background():
    """可选：akshare 行情背景。未安装或失败时返回 None 并记录日志。"""
    try:
        import akshare as ak  # noqa: F401
    except ImportError:
        log.info("akshare 未安装，跳过行情背景（可选层）")
        return None
    try:
        df = ak.stock_zh_index_daily(symbol="sh000001")
        return df.tail(1).to_dict("records")
    except Exception as e:  # 网络/接口异常均不阻断主流程
        log.warning("行情背景获取失败，跳过：%s", e)
        return None
