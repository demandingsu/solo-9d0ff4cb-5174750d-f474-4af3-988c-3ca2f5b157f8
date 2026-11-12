"""端到端：FastAPI + 爬虫 + 引擎，对本地示例公告站做完整查询。

前置：示例站点已在 127.0.0.1:8080 提供服务（见 README / demo 脚本）。
"""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

SITE = os.environ.get("TEST_SITE_URL", "http://127.0.0.1:8080/index.html")
Q = "2026-11-11T10:00:00+08:00"

pytestmark = pytest.mark.asyncio


async def test_full_query_workflow():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # lifespan 由 LifespanManager 之外的方式不保证执行，手动触发
        async with app.router.lifespan_context(app):
            r = await ac.post("/query", json={"index_url": SITE, "query_time": Q})
    assert r.status_code == 200, r.text
    data = r.json()

    # 五个列表页全部爬到；21 个同站详情 URL（含同编号多版本），跨站 1 条被跳过
    assert len(data["list_pages_crawled"]) == 5
    assert data["detail_pages_crawled"] == 21
    assert data["announcements_parsed"] == 21
    assert len(data["skipped_offsite_links"]) == 1

    active = data["active_suspensions"]
    chains = {tuple(a["source_chain"]) for a in active}
    # 有效链：001 原始（同编号重复刊出版本内容一致，已合并）；005 原始（012 次日更正不可见）；
    # 003→006→010 两次更正；009→011 更正（带悬空引用）
    assert ("N-2026-001",) in chains
    assert ("N-2026-005",) in chains
    assert ("N-2026-003", "N-2026-006", "N-2026-010") in chains
    assert ("N-2026-009", "N-2026-011") in chains
    # 冲突链不输出确定结论：60/70/71 路均不在有效停运中
    assert not any(a["route"] in {"60 路", "70 路", "71 路"} for a in active)

    # 001 的重复刊出版本内容一致，合并后两个来源 URL 都保留
    chain001 = next(a for a in active if a["source_chain"] == ["N-2026-001"])
    assert sorted(chain001["sources"]) == [
        "http://127.0.0.1:8080/notices/N-2026-001-dup.html",
        "http://127.0.0.1:8080/notices/N-2026-001.html",
    ]

    by_route = {a["route"]: a for a in active}
    # 两次更正后的最终区间
    assert by_route["25 路"]["suspend_end"].startswith("2026-11-20T22:00:00")
    # 同线路两条独立公告分别判断：12 路有两条有效停运
    routes_12 = [a for a in active if a["route"] == "12 路"]
    assert len(routes_12) == 2

    reasons = {(c["root_id"], c["reason"]) for c in data["inactive_chains"]}
    assert ("N-2026-002", "expired") in reasons
    assert ("N-2026-004", "cancelled") in reasons
    assert ("N-2026-008", "not_yet_effective") in reasons
    assert ("N-2026-013", "not_yet_effective") in reasons
    # 冲突链不进入失效链判定
    assert {c["root_id"] for c in data["inactive_chains"]}.isdisjoint(
        {"N-2026-014", "N-2026-017", "N-2026-018"}
    )

    # 待核实链：015 冲突关联 014；016 两个版本分别指向 017/018，两条链都待核实
    pending = data["pending_verification"]
    pending_chains = {tuple(p["chain"]) for p in pending}
    assert pending_chains == {
        ("N-2026-014", "N-2026-015"),
        ("N-2026-017", "N-2026-016"),
        ("N-2026-018", "N-2026-016"),
    }
    p15 = next(p for p in pending if p["conflict_notice_ids"] == ["N-2026-015"])
    assert p15["route"] == "60 路"
    assert set(p15["sources"]) == {
        "http://127.0.0.1:8080/notices/N-2026-014.html",
        "http://127.0.0.1:8080/notices/N-2026-015.html",
        "http://127.0.0.1:8080/notices/N-2026-015-alt.html",
    }

    # 冲突明细：列出冲突来源；001 的一致重复不算冲突
    conflicts = data["duplicate_conflicts"]
    conflict_ids = {c["notice_id"] for c in conflicts}
    assert conflict_ids == {"N-2026-015", "N-2026-016"}
    c15 = next(c for c in conflicts if c["notice_id"] == "N-2026-015")
    assert c15["conflicting_fields"] == ["suspend_end"]
    assert {s["url"] for s in c15["sources"]} == {
        "http://127.0.0.1:8080/notices/N-2026-015.html",
        "http://127.0.0.1:8080/notices/N-2026-015-alt.html",
    }
    c16 = next(c for c in conflicts if c["notice_id"] == "N-2026-016")
    assert c16["conflicting_fields"] == ["route", "references"]

    unmatched = data["unmatched_references"]
    assert len(unmatched) == 1
    assert unmatched[0]["notice_id"] == "N-2026-011"
    assert unmatched[0]["missing_reference"] == "N-2026-999"

    # 来源链接完整
    chain25 = next(a for a in active if a["route"] == "25 路")
    assert all(u.startswith("http://127.0.0.1:8080/notices/") for u in chain25["sources"])


async def test_naive_query_time_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={"index_url": SITE, "query_time": "2026-11-11T10:00:00"},
            )
    assert r.status_code == 422


async def test_bad_index_url_returns_502():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/query",
                json={
                    "index_url": "http://127.0.0.1:9/never.html",
                    "query_time": Q,
                },
            )
    assert r.status_code == 502
