# 公交临时停运公告采集 API

调度员出车前，需要从运营方**分页发布**的线路公告中，查出“查询时刻仍有效”的临时停运。
本服务用 FastAPI + BeautifulSoup 抓取同站分页与详情页，按公告编号引用关系与发布时间
还原**更正/撤销公告链**，返回有效停运区间、来源链接及无法匹配的引用。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 公告按编号唯一标识 | `Announcement.notice_id` |
| 更正可引用另一条更正 | 引用图上从根（无有效出边引用的公告）沿后继遍历，支持任意深度 |
| 链顺序 | 引用关系定链，同一节点多个后继按发布时间排序 |
| 更正替换原停运信息 | 按发布时间依次应用，链上最后一条可见公告的起止/线路为当前生效信息 |
| 撤销使整条链失效 | 链上出现查询时刻已发布的撤销公告 → `inactive_chains[].reason=cancelled` |
| 未来公告不可见 | 发布时间晚于查询时刻的公告不参与链还原与判定 |
| 同线路独立公告分别判断 | 每条根公告独立成链；示例中 12 路有两条同时有效的独立停运 |
| 无法匹配的引用 | 被引用编号在整站不存在时收集到 `unmatched_references`，不影响其余判定 |
| 同站限制 | 只沿协议+主机+端口一致的链接爬行，跨站链接跳过并在 `skipped_offsite_links` 返回 |
| 同编号重复详情 | 按编号分组核对 `kind/route/停运起止/发布时间/引用编号`：全部一致则合并（来源 URL 全部保留），结论与分页抓取次序无关 |
| 同编号详情冲突 | 任一字段不一致即冲突：该编号及更正/撤销关联链（两个版本各自指向的原公告两边都覆盖）进入 `pending_verification`，列出冲突来源，**不输出这些链的确定停运结论**；无关联的独立公告照常判定 |

停运区间按半开 `[suspend_start, suspend_end)` 判定：查询时刻等于结束时刻即视为已失效。
所有时间均要求带时区（ISO 8601 偏移），比较基于同一时间线瞬间。

## 目录结构

```
app/                 FastAPI 采集服务
  app/models.py      Pydantic 模型（入参/出参/公告实体/冲突与待核实模型）
  app/parser.py      BeautifulSoup 解析：分页 rel=next、详情链接、详情字段
  app/crawler.py     同站分页 + 详情抓取（httpx 异步，限量/去重/错误收集）
  app/engine.py      公告链还原、同编号详情核对与有效性判定（纯函数，无 IO）
  app/main.py        POST /query、GET /health
  Dockerfile
site/                本地示例公告站（数据驱动生成纯静态 HTML）
  build_site.py      生成 html/（5 个列表页 + 21 条详情，含同编号重复/冲突场景）
  Dockerfile         python -m http.server 托管静态站
tests/               单元/集成测试（pytest）
demo/                本地与 Compose 演示脚本
docker-compose.yml   api(8000) + notice-site(8080)
```

## 运行方式

### 方式一：Docker Compose（推荐，完整离线演示）

```bash
python3 site/build_site.py          # 生成示例站静态文件
docker compose up -d --build

curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"index_url":"http://notice-site:8080/index.html",
       "query_time":"2026-11-11T10:00:00+08:00"}'
```

容器内 API 通过 Compose 网络访问 `notice-site`；从宿主机调用时 `index_url`
也可换成 `http://127.0.0.1:8080/index.html`（两个地址同构，同站判定均成立）。
交互式文档：http://127.0.0.1:8000/docs

### 方式二：本地 venv（无 Docker 环境）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py        # 若 ensurepip 不可用，先 bootstrap pip
.venv/bin/pip install -r app/requirements.txt
./demo/run_local_demo.sh           # 自动建站、起两个服务并发起查询
```

### 测试

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest -q      # 需先在 127.0.0.1:8080 启动示例站
```

## 示例站点场景（查询时刻 2026-11-11 10:00 +08:00）

| 公告 | 场景 | 判定结果 |
| --- | --- | --- |
| N-2026-001 | 12 路原始停运；另页刊出同编号、内容完全一致的重复详情 | 有效，两份来源 URL 合并保留；不计入冲突 |
| N-2026-002 | 18 路停运已结束 | `expired` |
| 003 → 006 → 010 | 25 路停运被两次更正（更正引用更正） | 有效，区间为 010 替换后的 11-10 ~ 11-20 |
| 004 → 007 | 33 路停运被撤销 | `cancelled`，整条链失效 |
| N-2026-005 | 12 路另一条独立停运；012 更正次日才发布 | 有效，链上只有 005 |
| N-2026-008 | 5 路停运尚未开始 | `not_yet_effective` |
| 009 → 011 | 40 路更正，且 011 误引用不存在的 N-2026-999 | 有效，同时报告悬空引用 |
| N-2026-013 | 50 路整条公告查询时刻之后才发布 | `not_yet_effective` |
| 014 → 015 | 60 路更正 015 的同编号详情在两页刊出，停运截止时间冲突 | `pending_verification`，不输出确定结论 |
| 017 → 016 ← 018 | 016 的两份同编号详情分别引用 017 与 018（指向不同原公告） | 两条关联链均 `pending_verification` |
| N-EXT-001 | 列表中的跨站链接 | 跳过，出现在 `skipped_offsite_links` |

### 同编号详情核对与待核实

`/query` 响应中与重复详情相关的两个字段：

* `duplicate_conflicts`：本次抓取发现的全部同编号多版本核对结果。
  内容一致的重复页不产生记录（静默合并）；冲突记录包含
  `conflicting_fields`（`kind/route/suspend_start/suspend_end/published_at/references`
  中不一致的字段）与 `sources`（每个版本的完整字段与详情页 URL）。
* `pending_verification`：因冲突而无法给出确定结论的公告链。每条记录给出
  `chain`（链上编号）、`sources`（链上全部版本的 URL）、`conflict_notice_ids`
  及对应 `conflicts` 明细。这些链既不会出现在 `active_suspensions`，也不会
  出现在 `inactive_chains` 中，需人工核实后再判定。

核对以 URL 排序后的版本为确定性基准，因此调换列表分页次序不会改变任何结论。

## 接入真实站点

真实页面结构不同时，只需替换 `app/parser.py` 中的 CSS 选择器与字段键映射
（`FIELD_KEYS` / `KIND_MAP`），引擎、爬虫与 API 契约无需改动。解析器约定详情页
使用 `<time datetime>` 携带带时区的 ISO 8601 时间、引用使用
`<a class="reference" data-ref-id="编号">`，避免依赖文案措辞。
