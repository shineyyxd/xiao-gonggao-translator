# -*- coding: utf-8 -*-
"""实时数据抓取层（Fetcher）：巨潮资讯网公告查询。

接口实测结论（2026-08-27 联调，2026-08-28 复核）：
  - POST http://www.cninfo.com.cn/new/hisAnnouncement/query
  - form: pageNum/pageSize(服务端上限30)/column/tabName=fulltext/seDate=YYYY-MM-DD~YYYY-MM-DD
  - column=sse 与 column=szse 返回**相同的全市场合并数据**（pageColumn 字段区分
    SHZB/SHKCB/SZZB/SZCY），两路互为冗余：主路失败才走备路，不做双份抓取。
  - 返回 announcements[] 关键字段：secCode/secName/announcementId/announcementTitle/
    announcementTime(毫秒时间戳)/adjunctUrl/pageColumn。
  - 巨潮**没有官方摘要**：info_summary 置空字符串；入选公告的摘要由
    pdf_summary.py 抓 PDF 原文摘录补齐（见该模块的合规边界注释）。
  - 分页"绕圈"实测（2026-08-25/26/28 三日复核）：平台分页硬上限 pageNum ≤ 100
    （pageSize=30 时可及 3000 条），第 101 页起重绕第 1 页内容，hasMore 恒 true；
    totalpages/totalAnnouncement 按真实总量自报（如 6538/217 页），超出可及范围。
    停止规则：抓到 min(totalpages, PAGE_CAP=100) 即停；"连续 2 页无新 id"作保险。
    旧逻辑"整页无新 id 即停"曾在 ~101 页提前停车（0827 缓存唯一 2390 条），
    且"len(batch)<pageSize 即停"会被中途短页误伤，均已修正。

降级链：主路(3次指数退避重试) → 备路(同重试) → pipeline/data/cache/ 最近成功快照
→ 全部失败抛 FetchFailed，交给 Orchestrator 走停刊路径。
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CNINFO_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
    "X-Requested-With": "XMLHttpRequest",
}
PAGE_SIZE = 30          # 服务端硬上限（实测 pageSize=100 也只返回 30）
PAGE_CAP = 100          # 平台分页硬上限：pageNum>100 即从第 1 页重绕（2026-08-25/26/28 三日实测）
MAX_PAGES = PAGE_CAP    # 安全上限 = 平台实际上限
POLITE_DELAY = 0.15     # 分页间隔，礼貌抓取
COLUMNS = ("sse", "szse")  # 主路 + 冗余备路
RETRY_BACKOFF = (2, 4, 8)  # 指数退避秒数，共 3 次重试

CACHE_DIR = Path(__file__).resolve().parent / "data" / "cache"
_CN_TZ = timezone(timedelta(hours=8))


class FetchFailed(Exception):
    """所有抓取路径与缓存降级均失败。Orchestrator 捕获后走停刊路径。"""


def _query_page(column: str, day: str, page: int, page_size: int = PAGE_SIZE) -> dict:
    """请求单页，失败抛 requests 异常（由上层重试）。"""
    resp = requests.post(CNINFO_URL, headers=_HEADERS, timeout=20, data={
        "pageNum": page, "pageSize": page_size, "column": column,
        "tabName": "fulltext", "plate": "", "stock": "", "searchkey": "",
        "secid": "", "category": "", "trade": "",
        "seDate": f"{day}~{day}", "sortName": "", "sortType": "",
        "isHLtitle": "true",
    })
    resp.raise_for_status()
    return resp.json()


def _fetch_column(column: str, day: str) -> tuple:
    """抓取一路的全部分页，返回 (公告列表, meta)。

    meta = {claimed_total, total_pages, pages, looped}，供完整性对账使用。
    停止规则（2026-08-25/26/28 三日实测修正）：
      - 抓到 min(服务器自报 totalpages, PAGE_CAP=100) 即停——pageNum>100 平台
        重绕第 1 页，totalpages 自报的是真实总量对应的页数，超出部分不可及；
      - "连续 2 页无新 id"作为保险（无 totalpages 时的绕圈兜底）；
      - 中途偶有短页/单页重复不再立即停车（旧逻辑曾因此在 ~1/3 处提前结束）。
    """
    out, seen_ids, page, dup_streak = [], set(), 1, 0
    claimed = total_pages = None
    looped = False
    while page <= MAX_PAGES:
        data = _query_page(column, day, page)
        if claimed is None:
            t = data.get("totalAnnouncement")
            claimed = t if isinstance(t, int) and t >= 0 else None
            tp = data.get("totalpages")
            total_pages = tp if isinstance(tp, int) and tp > 0 else None
        batch = data.get("announcements") or []
        new = [a for a in batch if a.get("announcementId") not in seen_ids]
        out.extend(batch)
        seen_ids.update(a.get("announcementId") for a in batch)
        log.info("巨潮 %s 第%d页：%d 条（累计 %d，唯一 %d）",
                 column, page, len(batch), len(out), len(seen_ids))
        if total_pages and page >= min(total_pages, PAGE_CAP):
            break
        dup_streak = dup_streak + 1 if not new else 0
        if dup_streak >= 2:  # 绕圈保险：连续两页全是已见 id
            looped = True
            log.warning("巨潮 %s 连续重复页，提前停止于第%d页", column, page)
            break
        if not batch:  # 空页：没有更多可抓
            break
        if not data.get("hasMore") and not total_pages:
            break
        page += 1
        time.sleep(POLITE_DELAY)
    meta = {"claimed_total": claimed, "total_pages": total_pages,
            "pages": page, "looped": looped}
    return out, meta


def query_claimed_total(column: str, day: str):
    """轻量对账查询：pageSize=1 查一次拿 totalAnnouncement。失败返回 None，不抛。"""
    try:
        data = _query_page(column, day, 1, page_size=1)
        t = data.get("totalAnnouncement")
        return t if isinstance(t, int) and t >= 0 else None
    except Exception as e:
        log.warning("对账轻量查询失败（%s %s）：%s", column, day, e)
        return None


COVERAGE_MIN = 0.90  # 覆盖率低于此值判完整性缺口（日报 WARNING + error_log）


def assess_integrity(meta: dict) -> dict:
    """数据完整性对账：接口声称总数 vs 实际抓到的唯一 announcementId 数。

    口径（2026-08-25/26/28 三日实测）：claimed = 主路首页 totalAnnouncement；
    cross = 备路 pageSize=1 轻量查询的 totalAnnouncement——两路实测为同一份
    全市场合并数据，互验只能确认确定性，不是独立来源。claimed 是真实总量
    （当日自洽，如 6538 ≈ 217 页 × 30），但平台分页硬上限 pageNum ≤ 100
    （可及 3000 条），超出部分该接口拿不到。故覆盖率按**可及范围**计算：
    coverage = 唯一 id 数 / min(claimed, 3000)；<90% 判 WARNING（抓取层缺口）。
    接口不给总数或两路不一致 → UNVERIFIABLE，如实说明，不编造覆盖率。
    """
    claimed = meta.get("claimed_total")
    cross = meta.get("cross_total")
    unique = meta.get("unique") or 0
    out = {"claimed": claimed, "cross": cross, "fetched": meta.get("fetched"),
           "unique": unique, "pages": meta.get("pages"),
           "total_pages": meta.get("total_pages"), "looped": meta.get("looped", False),
           "reachable": None, "coverage": None, "status": "UNVERIFIABLE", "note": ""}
    if meta.get("degraded"):
        out["note"] = "缓存降级数据（非当日实时抓取），无独立总数可对账"
        return out
    if claimed is None:
        out["note"] = "接口未返回总数字段，无独立总数可对账（已做绕圈检测+页数检查）"
        return out
    if cross is not None and cross != claimed:
        out["note"] = (f"两路总数不一致（主路 {claimed} / 备路 {cross}），"
                       "totalAnnouncement 口径不可信，不给覆盖率结论")
        return out
    reachable = min(claimed, PAGE_CAP * PAGE_SIZE)
    out["reachable"] = reachable
    out["coverage"] = (unique / reachable) if reachable else 0.0
    notes = []
    if claimed > reachable:
        notes.append(f"接口声称 {claimed} 条，超出分页可及上限"
                     f"（{PAGE_CAP} 页/{PAGE_CAP * PAGE_SIZE} 条）{claimed - reachable} 条；"
                     "超出部分该接口无法获取（平台分页上限，非抓取失败），覆盖率按可及范围计算")
    if out["coverage"] >= COVERAGE_MIN:
        out["status"] = "OK"
    else:
        out["status"] = "WARNING"
        notes.append(f"可及范围覆盖率低于 {COVERAGE_MIN * 100:.0f}%，存在完整性缺口")
    out["note"] = "；".join(notes)
    return out


def _with_retries(fn, retries: int = len(RETRY_BACKOFF)):
    """指数退避重试包装。返回结果或抛最后一次异常。"""
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except (requests.RequestException, ValueError) as e:
            last = e
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            log.warning("抓取失败（第%d次）：%s，%ds 后重试", attempt + 1, e, wait)
            time.sleep(wait)
    raise last


def map_announcement(raw: dict) -> dict:
    """巨潮字段 → 现有样本结构。"""
    sec_name = raw.get("secName") or ""
    title = raw.get("announcementTitle") or ""
    page_column = raw.get("pageColumn") or ""
    market = "上海证券交易所" if page_column.startswith("SH") else "深圳证券交易所"
    try:
        publ_date = datetime.fromtimestamp(
            (raw.get("announcementTime") or 0) / 1000, tz=_CN_TZ).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        publ_date = ""
    ann_id = raw.get("announcementId") or ""
    return {
        "secu_abbr": sec_name,
        "secu_code": raw.get("secCode") or "",
        "secu_market": market,
        "info_title": f"{sec_name}:{title}" if sec_name else title,
        "info_publ_date": publ_date,
        "info_tag": "",
        "listed_sector": page_column,
        "info_event_txt": "",
        "info_summary": "",  # 巨潮无官方摘要，置空（README 有取舍说明）
        "announcement_link": "http://static.cninfo.com.cn/" + (raw.get("adjunctUrl") or ""),
        "id": int(ann_id) if str(ann_id).isdigit() else abs(hash(ann_id)) % (10 ** 12),
    }


def _save_cache(day: str, mapped: list) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"announcements_{day.replace('-', '')}.json"
    path.write_text(json.dumps(mapped, ensure_ascii=False), encoding="utf-8")
    return path


def _latest_cache() -> list:
    """最近一次成功快照（可能不是当日的——数据陈旧会导致窗口过滤后选题偏少，
    这是可接受的降级语义，README 有说明）。"""
    if not CACHE_DIR.exists():
        return []
    files = sorted(CACHE_DIR.glob("announcements_*.json"))
    if not files:
        return []
    latest = files[-1]
    log.warning("使用缓存快照降级：%s", latest.name)
    return json.loads(latest.read_text(encoding="utf-8"))


def fetch_live(day: str, tracer=None, meta_out: dict = None) -> list:
    """抓取某日全市场公告，返回映射后的样本结构列表。

    tracer: 可选的审计记录器（有 record 方法），记录每路成败。
    meta_out: 可选 dict，回填完整性对账元数据（claimed_total/cross_total/
              fetched/unique/pages/looped 等），由 assess_integrity 出结论。
    全部路径失败且没有缓存时抛 FetchFailed。
    """
    errors = []
    for idx, column in enumerate(COLUMNS):
        try:
            raw, meta = _with_retries(lambda: _fetch_column(column, day))
            mapped = [map_announcement(r) for r in raw]
            unique = len({a["id"] for a in mapped})
            _save_cache(day, mapped)
            # 对账：备路 pageSize=1 轻量查询拿总数互验（两路为同一合并数据，验确定性）
            cross = query_claimed_total(COLUMNS[1 - idx], day)
            if tracer:
                tracer.record("fetcher", input_summary=f"cninfo column={column} date={day}",
                              output_summary=f"{len(mapped)} 条（唯一 {unique}，"
                                             f"接口声称 {meta.get('claimed_total')}）",
                              conclusion="OK")
            if meta_out is not None:
                meta_out.update(meta)
                meta_out.update({"column": column, "cross_total": cross,
                                 "fetched": len(mapped), "unique": unique})
            return mapped
        except Exception as e:  # 主路失败再走备路
            errors.append(f"{column}: {e}")
            log.error("巨潮 %s 路抓取失败：%s", column, e)
            if tracer:
                tracer.record("fetcher", input_summary=f"cninfo column={column} date={day}",
                              output_summary="", conclusion="FAILED", retries=len(RETRY_BACKOFF))
    cached = _latest_cache()
    if cached:
        if tracer:
            tracer.record("fetcher", input_summary=f"cache fallback date={day}",
                          output_summary=f"{len(cached)} 条（缓存）", conclusion="DEGRADED")
        if meta_out is not None:
            meta_out.update({"degraded": True, "fetched": len(cached),
                             "unique": len({a.get("id") for a in cached})})
        return cached
    raise FetchFailed("巨潮两路抓取均失败且无缓存：" + "；".join(errors))
