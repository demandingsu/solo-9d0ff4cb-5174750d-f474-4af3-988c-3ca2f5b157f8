"""解析器：分页链接、详情链接与详情字段。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.parser import (
    is_same_site,
    parse_detail,
    parse_detail_links,
    parse_next_page,
)

HTML_DIR = Path(__file__).parent.parent / "site" / "html"


def test_next_page_and_detail_links_index():
    html = (HTML_DIR / "index.html").read_text(encoding="utf-8")
    nxt = parse_next_page(html, "http://site:8080/index.html")
    assert nxt == "http://site:8080/page2.html"

    links = parse_detail_links(html, "http://site:8080/index.html")
    assert len(links) == 5
    assert links[0][1] == "N-2026-001"
    assert links[0][0] == "http://site:8080/notices/N-2026-001.html"


def test_last_page_has_no_next():
    html = (HTML_DIR / "page5.html").read_text(encoding="utf-8")
    assert parse_next_page(html, "http://site:8080/page5.html") is None


def test_offsite_link_absolute_and_detected():
    html = (HTML_DIR / "page5.html").read_text(encoding="utf-8")
    links = parse_detail_links(html, "http://site:8080/page5.html")
    offsite = [u for u, _, _ in links if "external.example.org" in u]
    assert offsite == ["http://external.example.org/notice/N-EXT-001.html"]
    assert not is_same_site(offsite[0], "http://site:8080/index.html")
    assert is_same_site("http://site:8080/notices/x.html", "http://site:8080/index.html")


def test_parse_suspension_detail():
    html = (HTML_DIR / "notices" / "N-2026-001.html").read_text(encoding="utf-8")
    a = parse_detail(html, "http://site:8080/notices/N-2026-001.html")
    assert a.notice_id == "N-2026-001"
    assert a.route == "12 路"
    assert a.kind.value == "suspension"
    assert a.suspend_start.isoformat() == "2026-11-10T06:00:00+08:00"
    assert a.suspend_end.isoformat() == "2026-11-12T22:00:00+08:00"
    assert a.published_at.utcoffset() is not None


def test_parse_correction_with_chained_reference():
    html = (HTML_DIR / "notices" / "N-2026-010.html").read_text(encoding="utf-8")
    a = parse_detail(html, "http://site:8080/notices/N-2026-010.html")
    assert a.kind.value == "correction"
    assert a.references == ["N-2026-006"]


def test_parse_cancellation_detail():
    html = (HTML_DIR / "notices" / "N-2026-007.html").read_text(encoding="utf-8")
    a = parse_detail(html, "http://site:8080/notices/N-2026-007.html")
    assert a.kind.value == "cancellation"
    assert a.suspend_start is None
    assert a.references == ["N-2026-004"]


def test_parse_multi_reference_detail():
    html = (HTML_DIR / "notices" / "N-2026-011.html").read_text(encoding="utf-8")
    a = parse_detail(html, "http://site:8080/notices/N-2026-011.html")
    assert a.references == ["N-2026-009", "N-2026-999"]


def test_parse_conflicting_duplicate_versions():
    # 同编号 N-2026-015 的两个版本仅停运截止时间不同
    base = "http://site:8080/notices/"
    a1 = parse_detail(
        (HTML_DIR / "notices" / "N-2026-015.html").read_text(encoding="utf-8"),
        base + "N-2026-015.html",
    )
    a2 = parse_detail(
        (HTML_DIR / "notices" / "N-2026-015-alt.html").read_text(encoding="utf-8"),
        base + "N-2026-015-alt.html",
    )
    assert a1.notice_id == a2.notice_id == "N-2026-015"
    assert a1.kind == a2.kind
    assert a1.route == a2.route == "60 路"
    assert a1.suspend_start == a2.suspend_start
    assert a1.suspend_end != a2.suspend_end
    assert a1.references == a2.references == ["N-2026-014"]


def test_parse_duplicate_versions_pointing_to_different_originals():
    # 同编号 N-2026-016 的两个版本引用不同的原公告
    base = "http://site:8080/notices/"
    a1 = parse_detail(
        (HTML_DIR / "notices" / "N-2026-016.html").read_text(encoding="utf-8"),
        base + "N-2026-016.html",
    )
    a2 = parse_detail(
        (HTML_DIR / "notices" / "N-2026-016-alt.html").read_text(encoding="utf-8"),
        base + "N-2026-016-alt.html",
    )
    assert a1.notice_id == a2.notice_id == "N-2026-016"
    assert a1.references == ["N-2026-017"]
    assert a2.references == ["N-2026-018"]


def test_parse_identical_duplicate_detail():
    # N-2026-001 与其重复刊出版本内容完全一致、仅 URL 不同
    base = "http://site:8080/notices/"
    a1 = parse_detail(
        (HTML_DIR / "notices" / "N-2026-001.html").read_text(encoding="utf-8"),
        base + "N-2026-001.html",
    )
    a2 = parse_detail(
        (HTML_DIR / "notices" / "N-2026-001-dup.html").read_text(encoding="utf-8"),
        base + "N-2026-001-dup.html",
    )
    assert a1.notice_id == a2.notice_id
    assert a1.kind == a2.kind
    assert a1.route == a2.route
    assert a1.suspend_start == a2.suspend_start
    assert a1.suspend_end == a2.suspend_end
    assert a1.published_at == a2.published_at
    assert a1.references == a2.references == []
    assert a1.url != a2.url


def test_missing_id_raises():
    bad = "<html><body><table class='notice-fields'></table></body></html>"
    with pytest.raises(ValueError):
        parse_detail(bad, "http://site/x.html")
