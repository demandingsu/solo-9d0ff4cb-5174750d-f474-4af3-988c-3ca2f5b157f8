"""引擎层：公告链还原与有效性判定。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.engine import reconstruct
from app.models import Announcement, NoticeKind

TZ = "+08:00"


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def make(
    notice_id: str,
    kind: NoticeKind,
    published: str,
    *,
    route: str | None = "12 路",
    start: str | None = None,
    end: str | None = None,
    refs: list[str] | None = None,
    url: str | None = None,
) -> Announcement:
    return Announcement(
        notice_id=notice_id,
        route=route,
        kind=kind,
        suspend_start=parse_dt(start) if start else None,
        suspend_end=parse_dt(end) if end else None,
        published_at=parse_dt(published),
        references=refs or [],
        url=url or f"http://x/notices/{notice_id}.html",
    )


Q = parse_dt(f"2026-11-11T10:00:00{TZ}")


def test_plain_active_suspension():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-12T22:00:00{TZ}")
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A1"]


def test_correction_chain_replaces_interval():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-07T09:00:00{TZ}",
             start=f"2026-11-09T06:00:00{TZ}", end=f"2026-11-12T20:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T15:00:00{TZ}",
             start=f"2026-11-10T00:00:00{TZ}", end=f"2026-11-15T22:00:00{TZ}",
             refs=["A1"]),
        make("A3", NoticeKind.CORRECTION, f"2026-11-10T08:00:00{TZ}",
             start=f"2026-11-10T00:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}",
             refs=["A2"]),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    a = res.active[0]
    assert a.source_chain == ["A1", "A2", "A3"]
    assert a.suspend_end == parse_dt(f"2026-11-20T22:00:00{TZ}")
    assert a.published_at == parse_dt(f"2026-11-10T08:00:00{TZ}")
    assert a.sources == [f"http://x/notices/{i}.html" for i in ("A1", "A2", "A3")]


def test_cancellation_kills_whole_chain():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T08:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-21T22:00:00{TZ}",
             refs=["A1"]),
        make("A3", NoticeKind.CANCELLATION, f"2026-11-09T10:00:00{TZ}",
             route=None, refs=["A2"]),
    ]
    res = reconstruct(anns, Q)
    assert res.active == []
    assert len(res.inactive) == 1
    assert res.inactive[0].reason == "cancelled"
    assert res.inactive[0].chain == ["A1", "A2", "A3"]


def test_future_correction_is_invisible():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-09T09:00:00{TZ}",
             start=f"2026-11-11T05:00:00{TZ}", end=f"2026-11-13T21:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-12T09:00:00{TZ}",
             start=f"2026-11-11T05:00:00{TZ}", end=f"2026-11-14T21:00:00{TZ}",
             refs=["A1"]),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A1"]
    assert res.active[0].suspend_end == parse_dt(f"2026-11-13T21:00:00{TZ}")


def test_independent_notices_same_route_judged_separately():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-05T09:00:00{TZ}",
             start=f"2026-11-05T06:00:00{TZ}", end=f"2026-11-06T20:00:00{TZ}"),
        make("A2", NoticeKind.SUSPENSION, f"2026-11-09T09:00:00{TZ}",
             start=f"2026-11-11T05:00:00{TZ}", end=f"2026-11-13T21:00:00{TZ}"),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A2"]
    assert {i.reason for i in res.inactive} == {"expired"}
    assert res.inactive[0].root_id == "A1"


def test_unmatched_reference_reported_but_chain_still_evaluated():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-10T09:00:00{TZ}",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-13T20:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-10T18:00:00{TZ}",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-16T20:00:00{TZ}",
             refs=["A1", "GHOST"]),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].suspend_end == parse_dt(f"2026-11-16T20:00:00{TZ}")
    assert len(res.unmatched) == 1
    assert res.unmatched[0].notice_id == "A2"
    assert res.unmatched[0].missing_reference == "GHOST"


def test_not_yet_started_and_whole_chain_future():
    future_chain = make("F1", NoticeKind.SUSPENSION, f"2026-11-14T09:00:00{TZ}",
                        route="50 路",
                        start=f"2026-11-15T06:00:00{TZ}",
                        end=f"2026-11-18T20:00:00{TZ}")
    upcoming = make("U1", NoticeKind.SUSPENSION, f"2026-11-09T08:00:00{TZ}",
                    route="5 路",
                    start=f"2026-11-12T06:00:00{TZ}",
                    end=f"2026-11-14T20:00:00{TZ}")
    res = reconstruct([future_chain, upcoming], Q)
    reasons = {i.root_id: i.reason for i in res.inactive}
    assert reasons == {"F1": "not_yet_effective", "U1": "not_yet_effective"}


def test_boundary_end_exclusive_start_inclusive():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-09T09:00:00{TZ}",
             start=f"2026-11-11T10:00:00{TZ}", end=f"2026-11-12T10:00:00{TZ}"),
    ]
    # 起点 == 查询时刻：有效
    assert len(reconstruct(anns, parse_dt(f"2026-11-11T10:00:00{TZ}")).active) == 1
    # 终点 == 查询时刻：已失效（半开区间 [start, end)）
    res = reconstruct(anns, parse_dt(f"2026-11-12T10:00:00{TZ}"))
    assert res.active == []
    assert res.inactive[0].reason == "expired"


def test_dangling_correction_is_its_own_root():
    # 引用全部悬空的更正公告自身作为根，仍需可被解释
    anns = [
        make("A9", NoticeKind.CORRECTION, f"2026-11-10T18:00:00{TZ}",
             refs=["GHOST"]),
    ]
    res = reconstruct(anns, Q)
    assert res.active == []
    assert res.inactive[0].reason == "no_interval"
    assert len(res.unmatched) == 1


def test_timezone_comparison_uses_instant():
    # 同一时刻不同偏移表达，判定结果应一致
    anns = [
        make("A1", NoticeKind.SUSPENSION, "2026-11-08T09:00:00+08:00",
             start="2026-11-11T08:00:00+08:00", end="2026-11-11T12:00:00+08:00"),
    ]
    q_utc = parse_dt("2026-11-11T03:00:00+00:00")  # == 11:00 +08:00
    res = reconstruct(anns, q_utc)
    assert len(res.active) == 1


def test_naive_datetime_rejected_by_model():
    with pytest.raises(Exception):
        Announcement(
            notice_id="X", route="r", kind=NoticeKind.SUSPENSION,
            suspend_start=None, suspend_end=None,
            published_at=parse_dt("2026-11-11T10:00:00"),  # 无时区
            references=[], url="http://x", title=None,
        )


# ---------------------------------------------------------------------------
# 同编号重复详情：一致合并 / 冲突待核实
# ---------------------------------------------------------------------------


def test_identical_duplicates_are_merged_and_all_urls_kept():
    common = dict(
        kind=NoticeKind.SUSPENSION,
        published=f"2026-11-08T09:00:00{TZ}",
        start=f"2026-11-10T06:00:00{TZ}",
        end=f"2026-11-12T22:00:00{TZ}",
    )
    anns = [
        make("A1", url="http://x/notices/A1.html", **common),
        make("A1", url="http://x/notices/A1-dup.html", **common),
    ]
    res = reconstruct(anns, Q)
    assert len(res.active) == 1
    assert res.active[0].source_chain == ["A1"]
    # 两个来源 URL 都保留
    assert res.active[0].sources == [
        "http://x/notices/A1-dup.html",
        "http://x/notices/A1.html",
    ]
    # 内容一致：无冲突、无待核实
    assert res.conflicts == []
    assert res.pending == []


def test_identical_duplicates_result_is_order_independent():
    common = dict(
        kind=NoticeKind.SUSPENSION,
        published=f"2026-11-08T09:00:00{TZ}",
        start=f"2026-11-10T06:00:00{TZ}",
        end=f"2026-11-12T22:00:00{TZ}",
    )
    v1 = make("A1", url="http://x/notices/A1.html", **common)
    v2 = make("A1", url="http://x/notices/A1-dup.html", **common)
    r1 = reconstruct([v1, v2], Q)
    r2 = reconstruct([v2, v1], Q)
    assert r1.active[0].sources == r2.active[0].sources
    assert r1.active[0].suspend_end == r2.active[0].suspend_end


def test_conflicting_duplicate_marks_chain_pending_and_lists_sources():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T15:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-15T22:00:00{TZ}",
             refs=["A1"], url="http://x/notices/A2.html"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T15:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-18T22:00:00{TZ}",
             refs=["A1"], url="http://x/notices/A2-alt.html"),
    ]
    res = reconstruct(anns, Q)

    # 不输出该链的确定停运结论，也不进入失效链
    assert res.active == []
    assert res.inactive == []

    assert len(res.pending) == 1
    p = res.pending[0]
    assert p.root_id == "A1"
    assert p.chain == ["A1", "A2"]
    assert p.conflict_notice_ids == ["A2"]
    assert p.reason == "conflicting_duplicates"
    assert set(p.sources) == {
        "http://x/notices/A1.html",
        "http://x/notices/A2.html",
        "http://x/notices/A2-alt.html",
    }

    assert len(res.conflicts) == 1
    c = res.conflicts[0]
    assert c.notice_id == "A2"
    assert c.conflicting_fields == ["suspend_end"]
    assert {s.url for s in c.sources} == {
        "http://x/notices/A2.html",
        "http://x/notices/A2-alt.html",
    }


def test_conflict_result_does_not_depend_on_crawl_order():
    base = dict(
        kind=NoticeKind.SUSPENSION,
        published=f"2026-11-08T09:00:00{TZ}",
        start=f"2026-11-10T06:00:00{TZ}",
    )
    v1 = make("A1", end=f"2026-11-15T22:00:00{TZ}",
              url="http://x/notices/A1.html", **base)
    v2 = make("A1", end=f"2026-11-18T22:00:00{TZ}",
              url="http://x/notices/A1-alt.html", **base)
    r1 = reconstruct([v1, v2], Q)
    r2 = reconstruct([v2, v1], Q)
    assert r1.pending == r2.pending
    assert r1.active == r2.active == []
    assert r1.conflicts[0].conflicting_fields == ["suspend_end"]


def test_conflict_on_root_taints_descendant_chain():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-07T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}",
             url="http://x/notices/A1.html"),
        make("A1", NoticeKind.SUSPENSION, f"2026-11-07T09:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-21T22:00:00{TZ}",
             url="http://x/notices/A1-alt.html"),
        make("A2", NoticeKind.CORRECTION, f"2026-11-09T15:00:00{TZ}",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-15T22:00:00{TZ}",
             refs=["A1"]),
    ]
    res = reconstruct(anns, Q)
    assert res.active == []
    assert len(res.pending) == 1
    assert res.pending[0].chain == ["A1", "A2"]
    assert res.pending[0].conflict_notice_ids == ["A1"]


def test_duplicate_versions_pointing_to_different_originals_both_chains_pending():
    anns = [
        make("R1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}", route="70 路",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-13T20:00:00{TZ}"),
        make("R2", NoticeKind.SUSPENSION, f"2026-11-08T09:30:00{TZ}", route="71 路",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-13T20:00:00{TZ}"),
        make("C1", NoticeKind.CORRECTION, f"2026-11-10T12:00:00{TZ}", route="70 路",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-15T20:00:00{TZ}",
             refs=["R1"], url="http://x/notices/C1.html"),
        make("C1", NoticeKind.CORRECTION, f"2026-11-10T12:00:00{TZ}", route="70 路",
             start=f"2026-11-11T06:00:00{TZ}", end=f"2026-11-15T20:00:00{TZ}",
             refs=["R2"], url="http://x/notices/C1-alt.html"),
    ]
    res = reconstruct(anns, Q)

    # 两份详情指向不同原公告：两边关联链都待核实
    assert res.active == []
    chains = {tuple(p.chain) for p in res.pending}
    assert chains == {("R1", "C1"), ("R2", "C1")}
    for p in res.pending:
        assert p.conflict_notice_ids == ["C1"]
        assert {s.url for s in p.conflicts[0].sources} == {
            "http://x/notices/C1.html",
            "http://x/notices/C1-alt.html",
        }

    # 冲突字段包含引用编号
    c = res.conflicts[0]
    assert c.notice_id == "C1"
    assert "references" in c.conflicting_fields


def test_independent_unrelated_notices_still_judged_normally():
    anns = [
        # 冲突链：60 路
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}", route="60 路",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-20T22:00:00{TZ}",
             url="http://x/notices/A1.html"),
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}", route="60 路",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-21T22:00:00{TZ}",
             url="http://x/notices/A1-alt.html"),
        # 无关联的独立公告：12 路照常判定为有效
        make("B1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}", route="12 路",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-12T22:00:00{TZ}"),
        # 无关联的独立公告：18 路照常判定为过期
        make("B2", NoticeKind.SUSPENSION, f"2026-11-03T14:00:00{TZ}", route="18 路",
             start=f"2026-11-05T06:00:00{TZ}", end=f"2026-11-08T20:00:00{TZ}"),
    ]
    res = reconstruct(anns, Q)

    assert len(res.pending) == 1
    assert res.pending[0].chain == ["A1"]

    assert len(res.active) == 1
    assert res.active[0].route == "12 路"

    assert len(res.inactive) == 1
    assert res.inactive[0].root_id == "B2"
    assert res.inactive[0].reason == "expired"


def test_conflicting_duplicate_reports_each_compared_field():
    anns = [
        make("A1", NoticeKind.SUSPENSION, f"2026-11-08T09:00:00{TZ}", route="12 路",
             start=f"2026-11-10T06:00:00{TZ}", end=f"2026-11-12T22:00:00{TZ}",
             url="http://x/notices/A1.html"),
        make("A1", NoticeKind.CORRECTION, f"2026-11-09T09:00:00{TZ}", route="13 路",
             start=f"2026-11-10T07:00:00{TZ}", end=f"2026-11-13T22:00:00{TZ}",
             url="http://x/notices/A1-alt.html"),
    ]
    res = reconstruct(anns, Q)
    c = res.conflicts[0]
    assert set(c.conflicting_fields) == {
        "kind",
        "route",
        "suspend_start",
        "suspend_end",
        "published_at",
    }
    assert len(res.pending) == 1


def test_reference_order_difference_is_not_a_conflict():
    v1 = make("A2", NoticeKind.CORRECTION, f"2026-11-10T18:00:00{TZ}", route=None,
              refs=["A1", "GHOST"], url="http://x/notices/A2.html")
    v2 = make("A2", NoticeKind.CORRECTION, f"2026-11-10T18:00:00{TZ}", route=None,
              refs=["GHOST", "A1"], url="http://x/notices/A2-alt.html")
    res = reconstruct([v1, v2], Q)
    assert res.conflicts == []
    assert res.pending == []
