# -*- coding: utf-8 -*-
"""Fetcher 分页停止规则 + 数据完整性对账单测（全部 mock 接口响应，不联网）。"""
import fetcher
from error_log import render_daily_report


def _page(ids, total=None, totalpages=None, has_more=True):
    return {"announcements": [{"announcementId": i} for i in ids],
            "totalAnnouncement": total, "totalpages": totalpages,
            "hasMore": has_more}


class TestFetchColumnStopping:
    def test_抓满totalpages即停(self, monkeypatch):
        pages = {1: _page(range(1, 31), total=60, totalpages=2),
                 2: _page(range(31, 61), total=60, totalpages=2)}
        monkeypatch.setattr(fetcher, "_query_page", lambda c, d, p, page_size=30: pages[p])
        monkeypatch.setattr(fetcher, "POLITE_DELAY", 0)
        out, meta = fetcher._fetch_column("sse", "2026-08-28")
        assert len(out) == 60
        assert meta == {"claimed_total": 60, "total_pages": 2, "pages": 2, "looped": False}

    def test_totalpages超出平台页数上限时按100页停(self, monkeypatch):
        """totalpages=150 但平台 pageNum>100 重绕：抓到第 100 页即停，不请求绕圈页。"""
        def fake_query(c, d, p, page_size=30):
            assert p <= 100, "不应请求第 100 页之后的绕圈页"
            return _page(range(p * 1000, p * 1000 + 30), total=4500, totalpages=150)
        monkeypatch.setattr(fetcher, "_query_page", fake_query)
        monkeypatch.setattr(fetcher, "POLITE_DELAY", 0)
        out, meta = fetcher._fetch_column("sse", "2026-08-28")
        assert meta["pages"] == 100 and not meta["looped"] and len(out) == 3000

    def test_中途短页不再提前停车(self, monkeypatch):
        """旧逻辑 len(batch)<pageSize 即停，曾在 ~1/3 处提前结束；短页后继续抓满。"""
        pages = {1: _page(range(1, 31), total=65, totalpages=3),
                 2: _page(range(31, 36), total=65, totalpages=3),   # 短页
                 3: _page(range(36, 66), total=65, totalpages=3)}
        monkeypatch.setattr(fetcher, "_query_page", lambda c, d, p, page_size=30: pages[p])
        monkeypatch.setattr(fetcher, "POLITE_DELAY", 0)
        out, meta = fetcher._fetch_column("sse", "2026-08-28")
        assert len(out) == 65 and meta["pages"] == 3 and not meta["looped"]

    def test_绕圈连续重复页保险停车(self, monkeypatch):
        """无 totalpages 时：连续 2 页全是已见 id → looped=True 停车。"""
        first = _page(range(1, 31), total=None, totalpages=None)
        dup = _page(range(1, 31), total=None, totalpages=None)  # 与第 1 页完全相同
        calls = []
        def fake_query(c, d, p, page_size=30):
            calls.append(p)
            return first if p == 1 else dup
        monkeypatch.setattr(fetcher, "_query_page", fake_query)
        monkeypatch.setattr(fetcher, "POLITE_DELAY", 0)
        out, meta = fetcher._fetch_column("sse", "2026-08-28")
        assert meta["looped"] and calls == [1, 2, 3]  # 第2页重复( streak1 )，第3页再重复才停
        assert len({a["announcementId"] for a in out}) == 30


class TestAssessIntegrity:
    def test_覆盖率达标OK(self):
        r = fetcher.assess_integrity({"claimed_total": 100, "cross_total": 100,
                                      "fetched": 95, "unique": 95, "pages": 4})
        assert r["status"] == "OK" and abs(r["coverage"] - 0.95) < 1e-9

    def test_超出分页可及上限时按可及范围算覆盖率(self):
        """claimed 6538 > 可及 3000：抓满 3000 即 100%，note 如实说明超出部分。"""
        r = fetcher.assess_integrity({"claimed_total": 6538, "cross_total": 6538,
                                      "fetched": 3030, "unique": 3000, "pages": 100})
        assert r["status"] == "OK" and r["reachable"] == 3000
        assert abs(r["coverage"] - 1.0) < 1e-9
        assert "分页可及上限" in r["note"] and "3538" in r["note"]

    def test_覆盖率不足WARNING(self):
        r = fetcher.assess_integrity({"claimed_total": 6538, "cross_total": 6538,
                                      "fetched": 3030, "unique": 2390, "pages": 101,
                                      "looped": True})
        assert r["status"] == "WARNING" and r["coverage"] < 0.90
        assert "完整性缺口" in r["note"]

    def test_接口无总数UNVERIFIABLE(self):
        r = fetcher.assess_integrity({"claimed_total": None, "cross_total": None,
                                      "fetched": 3000, "unique": 3000})
        assert r["status"] == "UNVERIFIABLE" and r["coverage"] is None
        assert "无独立总数可对账" in r["note"]

    def test_两路总数不一致不给结论(self):
        r = fetcher.assess_integrity({"claimed_total": 6538, "cross_total": 6200,
                                      "fetched": 6500, "unique": 6500})
        assert r["status"] == "UNVERIFIABLE" and "不可信" in r["note"]

    def test_缓存降级无对账(self):
        r = fetcher.assess_integrity({"degraded": True, "fetched": 100, "unique": 100})
        assert r["status"] == "UNVERIFIABLE" and "缓存降级" in r["note"]


class TestIntegrityReportLine:
    def _render(self, integrity):
        return render_daily_report(
            "2026-08-28", [], [],
            {"selected_count": 0, "generated_count": 0, "checkpoints_total": 0,
             "checkpoints_passed": 0, "accuracy": 1.0}, [],
            meta={"run_id": "r1", "status": "OK", "source": "live"},
            integrity=integrity)

    def test_日报有完整性行(self):
        report = self._render({"claimed": 6538, "fetched": 6510, "unique": 6510,
                               "coverage": 6510 / 6538, "status": "OK", "note": ""})
        assert "数据完整性" in report and "6538" in report and "99.6%" in report
        assert "WARNING" not in report

    def test_日报WARNING标红(self):
        report = self._render({"claimed": 6538, "fetched": 3030, "unique": 2390,
                               "coverage": 2390 / 6538, "status": "WARNING",
                               "note": "覆盖率低于 90%，存在完整性缺口"})
        assert "WARNING" in report and "完整性缺口" in report

    def test_无integrity不渲染该节(self):
        assert "数据完整性" not in self._render(None)
