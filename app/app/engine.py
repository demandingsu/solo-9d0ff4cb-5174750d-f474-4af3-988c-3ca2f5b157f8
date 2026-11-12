"""公告链还原与停运有效性判定。

规则（对应业务约定）：

* 每条公告按编号唯一标识；更正/撤销公告通过“引用编号”指向上一条公告，
  更正也可以引用另一条更正，从而形成链。
* 链的顺序由引用关系确定，同一节点存在多个后继时按发布时间排序。
* 更正公告携带的停运起止（及线路）替换其之前链上的停运信息；
  链上最后一条非撤销公告的信息即当前生效信息。
* 撤销公告使它所在的整条链失效。
* 查询时刻之后才发布的公告在该时刻尚不可见，不参与判定。
* 同一起始公告若出现分叉后继，每个分支独立成链、独立判定；
  不同根公告（同线路的独立公告）天然分别判断。

同编号重复详情（同站分页偶尔刊出多份同编号详情页）：

* 按编号对抓取结果分组，逐字段核对
  ``kind / route / suspend_start / suspend_end / published_at / references``。
* 所有字段一致的重复页视为同一公告合并，所有来源 URL 均保留在结论的
  ``sources`` 中；判定不依赖分页抓取顺序。
* 任一字段不一致即为冲突：该编号及其通过更正/撤销引用关联到的整条公告链
  （两个版本各自引用的原公告两边都要覆盖）标记为“待核实”，列出冲突来源，
  且不输出这些链的确定停运结论（既不进入有效停运，也不进入失效链判定）。
* 与冲突公告无引用关联的独立公告照常判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    ActiveSuspension,
    Announcement,
    ConflictSource,
    InactiveChain,
    NoticeConflict,
    NoticeKind,
    PendingVerification,
    UnmatchedReference,
)

# 参与一致性核对的字段及展示顺序
COMPARE_FIELDS: tuple[str, ...] = (
    "kind",
    "route",
    "suspend_start",
    "suspend_end",
    "published_at",
    "references",
)


@dataclass
class _Node:
    announcement: Announcement
    children: list[str] = field(default_factory=list)  # 引用本公告的后继编号
    urls: list[str] = field(default_factory=list)      # 该编号全部版本的详情页 URL


@dataclass
class ReconstructionResult:
    active: list[ActiveSuspension]
    inactive: list[InactiveChain]
    unmatched: list[UnmatchedReference]
    pending: list[PendingVerification]
    conflicts: list[NoticeConflict]
    # 编号 -> 代表公告（便于测试/诊断）
    by_id: dict[str, Announcement]


def _sort_key(published_at: datetime) -> datetime:
    return published_at


def _fingerprint(a: Announcement) -> tuple:
    """用于同编号一致性核对的字段指纹。

    引用编号按集合比较，引用书写顺序不同但指向集合相同视为一致；
    其余字段直接比较（带时区时间按瞬间比较）。
    """
    return (
        a.kind,
        a.route,
        a.suspend_start,
        a.suspend_end,
        a.published_at,
        frozenset(a.references),
    )


def _variant(a: Announcement) -> ConflictSource:
    return ConflictSource(
        url=a.url,
        kind=a.kind,
        route=a.route,
        suspend_start=a.suspend_start,
        suspend_end=a.suspend_end,
        published_at=a.published_at,
        references=list(a.references),
    )


def reconstruct(
    announcements: list[Announcement], query_time: datetime
) -> ReconstructionResult:
    """根据引用关系与发布时间还原公告链并判定有效性。"""
    # 0) 按编号分组：同编号的多份详情逐版本核对，结果与抓取顺序无关
    groups: dict[str, list[Announcement]] = {}
    for a in announcements:
        groups.setdefault(a.notice_id, []).append(a)

    by_id: dict[str, Announcement] = {}
    conflict_ids: set[str] = set()
    conflicts: list[NoticeConflict] = []
    all_urls: dict[str, list[str]] = {}

    for notice_id in sorted(groups):
        variants = sorted(groups[notice_id], key=lambda x: x.url)
        all_urls[notice_id] = [v.url for v in variants]
        fingerprint = _fingerprint(variants[0])
        differing: set[str] = set()
        for v in variants[1:]:
            if _fingerprint(v) == fingerprint:
                continue
            for idx, field_name in enumerate(COMPARE_FIELDS):
                if _fingerprint(v)[idx] != fingerprint[idx]:
                    differing.add(field_name)
        if differing:
            conflict_ids.add(notice_id)
            conflicts.append(
                NoticeConflict(
                    notice_id=notice_id,
                    conflicting_fields=[f for f in COMPARE_FIELDS if f in differing],
                    sources=[_variant(v) for v in variants],
                )
            )
        # 代表版本取 URL 排序后的第一条，保证调换分页次序结论不变
        by_id[notice_id] = variants[0]

    # 1) 无法匹配的引用：被引用编号在整站公告中不存在。
    #    同编号多版本的引用取并集后只报告一次。
    unmatched: list[UnmatchedReference] = []
    seen_unmatched: set[tuple[str, str]] = set()
    for notice_id in sorted(by_id):
        refs = {r for v in groups[notice_id] for r in v.references}
        for ref in sorted(refs):
            if ref not in by_id and (notice_id, ref) not in seen_unmatched:
                seen_unmatched.add((notice_id, ref))
                unmatched.append(
                    UnmatchedReference(
                        notice_id=notice_id,
                        missing_reference=ref,
                        url=by_id[notice_id].url,
                        kind=by_id[notice_id].kind,
                    )
                )

    # 2) 构建后继索引（只连存在的边）。同编号多版本的引用取并集，
    #    保证任一版本指向的更正/撤销关联链都会被纳入待核实范围。
    nodes: dict[str, _Node] = {
        nid: _Node(a, urls=all_urls[nid]) for nid, a in by_id.items()
    }
    for notice_id, a in by_id.items():
        refs = {r for v in groups[notice_id] for r in v.references}
        for ref in refs:
            if ref in nodes and notice_id not in nodes[ref].children:
                nodes[ref].children.append(notice_id)
    for node in nodes.values():
        node.children.sort(key=lambda cid: _sort_key(by_id[cid].published_at))

    visible = {nid for nid, a in by_id.items() if a.published_at <= query_time}

    # 3) 根 = 不引用任何“查询时刻已发布且存在”的公告的节点（链的出边起点）。
    #    悬空引用（编号不存在）与指向未来公告的引用不阻止其成为根。
    def has_visible_existing_ref(notice_id: str) -> bool:
        refs = {r for v in groups[notice_id] for r in v.references}
        return any(
            ref in by_id and by_id[ref].published_at <= query_time for ref in refs
        )

    roots = [nid for nid in by_id if not has_visible_existing_ref(nid)]
    roots.sort(key=lambda nid: (by_id[nid].published_at, nid))

    active: list[ActiveSuspension] = []
    inactive: list[InactiveChain] = []
    pending: list[PendingVerification] = []
    conflict_by_id = {c.notice_id: c for c in conflicts}

    def chain_urls(chain_ids: list[str]) -> list[str]:
        urls: list[str] = []
        for nid in chain_ids:
            for u in nodes[nid].urls:
                if u not in urls:
                    urls.append(u)
        return urls

    def mark_pending(path: list[str]) -> None:
        tainted = [nid for nid in path if nid in conflict_ids]
        chain_ann = [by_id[i] for i in path]
        route = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > query_time:
                continue
            if a.route:
                route = a.route
        pending.append(
            PendingVerification(
                root_id=path[0],
                route=route,
                chain=list(path),
                sources=chain_urls(path),
                conflict_notice_ids=tainted,
                conflicts=[conflict_by_id[nid] for nid in tainted],
            )
        )

    def walk(path: list[str], seen: set[str]) -> None:
        tail = path[-1]
        # 查询时刻之后才发布的后继公告当时尚不可见，不进入链
        kids = [c for c in nodes[tail].children if c not in seen and c in visible]
        if not kids:
            # 路径上任何编号存在同编号冲突详情：整条链待核实，
            # 不输出确定的有效/失效结论
            if any(nid in conflict_ids for nid in path):
                mark_pending(path)
            else:
                evaluate_chain(path)
            return
        for kid in kids:  # 已按发布时间排序
            walk(path + [kid], seen | {kid})

    def evaluate_chain(chain_ids: list[str]) -> None:
        chain_ann = [by_id[i] for i in chain_ids]
        sources = chain_urls(chain_ids)
        root = chain_ann[0]

        # 整条链在查询时刻都还未发布
        if not any(i in visible for i in chain_ids):
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=root.route,
                    reason="not_yet_effective",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=max(a.published_at for a in chain_ann),
                )
            )
            return

        # 撤销 -> 整条链失效（只统计查询时刻已发布的撤销）
        cancels = [
            a
            for a in chain_ann
            if a.kind is NoticeKind.CANCELLATION and a.published_at <= query_time
        ]
        if cancels:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=_current_route(chain_ann),
                    reason="cancelled",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=cancels[-1].published_at,
                )
            )
            return

        # 按发布时间依次应用更正，后者替换前者的停运信息
        route, start, end, latest_pub = _current_interval(chain_ann, query_time)
        if start is None or end is None:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=route,
                    reason="no_interval",
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_pub,
                )
            )
            return

        if start > query_time:
            reason = "not_yet_effective"
        elif end <= query_time:
            # 半开区间 [start, end)：查询时刻恰为结束时刻时已失效
            reason = "expired"
        else:
            reason = None

        if reason:
            inactive.append(
                InactiveChain(
                    root_id=root.notice_id,
                    route=route,
                    reason=reason,
                    chain=chain_ids,
                    sources=sources,
                    latest_published_at=latest_pub,
                )
            )
        else:
            active.append(
                ActiveSuspension(
                    route=route or "(未知线路)",
                    suspend_start=start,
                    suspend_end=end,
                    source_chain=chain_ids,
                    sources=sources,
                    published_at=latest_pub,
                )
            )

    def _current_route(chain_ann: list[Announcement]) -> str | None:
        route = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > query_time:
                continue
            if a.route:
                route = a.route
        return route

    def _current_interval(
        chain_ann: list[Announcement], now: datetime
    ) -> tuple[str | None, datetime | None, datetime | None, datetime | None]:
        route = None
        start = end = None
        latest_pub = None
        for a in sorted(chain_ann, key=lambda x: x.published_at):
            if a.published_at > now:
                continue
            latest_pub = a.published_at
            if a.route:
                route = a.route
            if a.suspend_start is not None:
                start = a.suspend_start
            if a.suspend_end is not None:
                end = a.suspend_end
        return route, start, end, latest_pub

    for root_id in roots:
        walk([root_id], {root_id})

    active.sort(key=lambda x: (x.route, x.suspend_start))
    inactive.sort(key=lambda x: (x.root_id, tuple(x.chain)))
    unmatched.sort(key=lambda x: (x.notice_id, x.missing_reference))
    pending.sort(key=lambda x: (x.root_id, tuple(x.chain)))
    conflicts.sort(key=lambda x: x.notice_id)

    return ReconstructionResult(
        active=active,
        inactive=inactive,
        unmatched=unmatched,
        pending=pending,
        conflicts=conflicts,
        by_id=by_id,
    )
