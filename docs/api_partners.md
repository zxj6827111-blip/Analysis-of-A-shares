# A股卦象数据系统 · 对外数据接口规范

> 版本：v2.9.7 · 文档更新：2026-09-11
> 本文档面向外部系统对接方，覆盖三类需求：**个股查询**、**卦象全功能查询**、**周报级 Excel 全字段数据**。
> 全部接口均为现有系统能力，无需额外开发；所有示例均为真实服务实测返回。

---

## 1. 接入说明

### 1.1 服务地址

| 环境 | Base URL | 说明 |
|---|---|---|
| 生产（**对接方服务同机部署，推荐**） | `http://127.0.0.1:8080` | 对接方系统（daily-stock，:8000）与本系统同在腾讯云 170.106.111.228 上，直接走本机回环，流量不出服务器 |
| 生产（公网） | `http://170.106.111.228:8080` | 仅跨机场景使用（如对接方测试服 43.159.129.227）；当前公网无认证、**不视为正式通道**，正式开放前我方将部署鉴权（见 §1.2），请勿在未经确认的情况下直接使用 |
| 接口文档（交互式 Swagger） | `<Base URL>/docs` | FastAPI 自动生成，可在线查看全部端点并直接试调 |
| 健康检查 | `GET /api/v1/health` | |
| 版本查询 | `GET /api/v1/version` | |

### 1.2 认证

| 对接路径 | 是否需要 Token |
|---|---|
| 同机回环 `http://127.0.0.1:8080`（推荐） | **不需要**。流量不出服务器，凭服务器登录权限即完成隔离 |
| 跨机 / 公网 `http://170.106.111.228:8080` | **需要**。本系统当前对公网未部署鉴权，该地址不视为正式通道；正式开通前我方将增加 Bearer Token 鉴权（与对接方 735 接口的 `Authorization: Bearer` 同款式）并配置 IP 白名单，届时另行通知；Token 由我方签发 |

> 注意：对接方现有的 `dsa_kb_*` Token 是对接方知识库接口的凭据，在本系统上无效；两套系统的 Token 互不通用。

### 1.3 通用约定

| 项 | 约定 |
|---|---|
| 协议/格式 | HTTP + JSON（UTF-8）；导出类接口返回 `.xlsx` 文件流 |
| 日期格式 | `YYYY-MM-DD` 或 `YYYYMMDD`（如 `2026-08-28` / `20260828`；`YYYY/MM/DD` 亦兼容） |
| 股票代码 | 支持 `600000`、`sh600000`、`SSE.STK.600000`、ts_code（`000001.SH`）、中文证券名（仅 `/quick`）多种写法，返回统一含 `code`（如 `sh600000`）、`std_code`（如 `SSE.STK.600000`）、`name` |
| 指数/ETF 代码 | 如 `sh000300`（指数）、`sh510300`（ETF） |
| 周期 `period` | `DAY`（日卦）\| `WEEK`（周卦）\| `MONTH`（月卦） |
| 复权口径 `adjust` | `tushare_qfq`（Tushare 前复权，正式 L1，**推荐**）\| `raw`（未复权，正式 L2）；`tdx_front` 已停用（请求返回 400） |
| 分页参数 | `page`（默认 1）、`page_size` |
| 超时建议 | 客户端 `timeout` 建议：单票/元数据 10s，批量 60s，全市场 batch/导出同步 600s |
| 错误返回 | 标准 HTTP 状态码：422 参数校验失败（FastAPI 自动校验，如必填参数缺失/过短，`detail` 为校验错误数组）/ 400 参数无法解析 / 404 代码或数据不存在 / 409 任务未就绪 / 500 服务端错误，其余错误 body 为 FastAPI 默认 `{"detail": "..."}` |

> 与对接方 735 接口的差异：735 用统一业务信封（`success` / `error_code` / `message`，部分错误 HTTP 仍返回 200）；本系统用 RESTful 风格——**以 HTTP 状态码为准**，错误详情在 `detail` 字段。对接方封装客户端时请按状态码分支处理。

### 1.4 卦象算法（供对方理解字段含义）

> 开盘定上卦：`digit_sum(open) mod 8`（0→8）；收盘定下卦：`digit_sum(close) mod 8`；最高+最低定动爻：`digit_sum(high)+digit_sum(low) mod 6`。
> 价格统一保留两位小数后取数字和。上卦+下卦成 64 卦，动爻定 384 爻之一（`state_id` 形如 `60-2` = 第 60 卦第 2 爻）。

### 1.5 数据新鲜度（重要）

- 行情/卦象数据链为**每周五 18:30 自动增量更新**（Tushare），数据天然滞后 0~7 个交易日。
- **查询日期超出数据范围时，接口自动回退到最近一个有数据的交易日**，并在 `notes` 中说明（例：请求 `2026-08-28` 返回 `bar.date=20260814` 并注明"已使用最近交易日"）。对接方**必须**以响应中的 `bar.date` / `query_date` 为准展示，不要假设请求日=数据日。
- 对接方可调用 `GET /api/v1/system/data-health` 获取当前数据截止日（`formal_l1.max_date` / `expected_latest_trading_day`），用于向最终用户展示"数据更新到哪天"。

---

## 2. 个股查询

### 2.1 个股速览（一行数据看全貌）

```
GET /api/v1/quick/{code}
```

`code` 支持 6 位数字、`sh/sz/bj` 前缀、标准符号、**中文名**（如"平安银行"）。返回最新行情概览 + 最新日卦/周卦 + 相关回测记录。**不传日期时自动取最新有数据的交易日**（响应 `market.latest_date` 为实际数据日）。服务端有 60 秒缓存，适合高频调用。

**Python 调用示例**：

```python
import requests

BASE = "http://127.0.0.1:8080"

def quick(code: str) -> dict:
    resp = requests.get(f"{BASE}/api/v1/quick/{code}", timeout=10)
    resp.raise_for_status()          # 4xx/5xx 直接抛错，错误详情在 resp.json()["detail"]
    return resp.json()

d = quick("600000")
print(d["name"], d["market"]["latest_date"], d["gua"]["summary"]["full_name"])
# 浦发银行 20260814 ䷻水泽节
```

**响应示例**（`GET /api/v1/quick/600000`，节选）：

```json
{
  "ok": true,
  "code": "sh600000",
  "name": "浦发银行",
  "std_code": "SSE.STK.600000",
  "symbol_type": "stock",
  "market": {
    "latest_date": 20260814,
    "open": 9.14, "high": 9.17, "low": 9.06, "close": 9.1,
    "prev_close": 9.12, "pct_change": -0.22,
    "bars_total": 6230, "first_date": 20000609,
    "dataset_source": "internal", "data_max_date": 20260814
  },
  "gua":      { "...": "与 §3.1 query_bagua 返回结构相同（日卦）" },
  "gua_week": { "...": "与 §3.1 query_bagua 返回结构相同（周卦）" },
  "related_runs": [ { "...": "包含该股票的最近回测记录" } ]
}
```

### 2.2 指数/ETF 清单（可查询的池子）

```
GET /api/v1/bagua/watchlist?kind=all|index|etf
```

**响应示例**（`kind=etf`，节选）：

```json
{
  "ok": true, "kind": "etf", "count": 10,
  "symbols": [
    {"code": "sh510050", "name": "上证50ETF", "type": "etf",
     "std_code": "SSE.ETF.510050", "available": true, "last_date": 20260812}
  ],
  "note": "指数/ETF 无复权口径，卦象按未复权(raw)价格计算。"
}
```

### 2.3 指数/ETF 成分股

```
GET /api/v1/bagua/constituents?code=sh510300&limit=2000
```

**响应示例**（`sh510300`，节选）：

```json
{
  "ok": true, "code": "sh510300", "name": "沪深300ETF",
  "symbol_type": "etf", "tracked_index": "sh000300",
  "tracked_index_name": "沪深300指数",
  "count": 300,
  "constituents": [
    {"code": "sz300750", "std_code": "SZSE.STK.300750", "name": "宁德时代"}
  ],
  "source": "tushare:fund_basic.benchmark(510300.SH->000300.SH);index_weight@20260731"
}
```

> `source` 标明成分股数据来源与快照日期；ETF 通过其跟踪指数取最新一期 `index_weight`。

### 2.4 数据健康度（对接方自检用）

```
GET /api/v1/system/data-health
```

关键字段：`status`（healthy）、`expected_latest_trading_day`（应为的最新交易日）、`formal_l1.max_date` / `formal_l2.max_date`（实际数据截止）、`trading_day_lag`（滞后交易日数）、`recent_sync_errors`。

---

## 3. 卦象查询（全功能）

### 3.1 单票卦象查询（核心接口）

```
GET /api/v1/bagua/query?code=600000&date=2026-08-28&period=DAY&adjust=tushare_qfq
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `code` | 是 | 股票/指数/ETF 代码（多种写法均可） |
| `date` | 是 | 查询日（非交易日自动回退最近交易日，见 `notes`） |
| `period` | 否 | `DAY`（默认）/ `WEEK` / `MONTH` |
| `adjust` | 否 | `tushare_qfq`（默认）/ `raw` |

**完整响应示例**（真实返回，`code=600000&date=2026-08-28&period=DAY&adjust=tushare_qfq`）：

```python
import requests

resp = requests.get(
    "http://127.0.0.1:8080/api/v1/bagua/query",
    params={"code": "600000", "date": "2026-08-28",
            "period": "DAY", "adjust": "tushare_qfq"},
    timeout=10,
)
resp.raise_for_status()
d = resp.json()
print(d["summary"]["full_name"], d["summary"]["action_signal"])   # ䷻水泽节 新开仓
print(d["bar"]["date"])   # 实际数据日（请求 20260828 回退到 20260814）
```

```json
{
  "ok": true,
  "code": "sh600000",
  "name": "浦发银行",
  "display": "sh600000 浦发银行",
  "std_code": "SSE.STK.600000",
  "symbol_type": "stock",
  "query_date": 20260828,
  "period": "DAY",
  "adjust": "tushare_qfq",
  "price_plane": "L1_signal_price",
  "bar_date_exact": false,
  "bar": {
    "date": 20260814, "start_date": 20260814, "end_date": 20260814,
    "n_days": 1, "closed": true,
    "open": 9.14, "high": 9.17, "low": 9.06, "close": 9.1
  },
  "bagua": {
    "open_price": "9.14", "high_price": "9.17", "low_price": "9.06", "close_price": "9.10",
    "open_digit_sum": 14, "high_digit_sum": 17, "low_digit_sum": 15, "close_digit_sum": 10,
    "upper_id": 6, "lower_id": 2,
    "upper_name": "坎", "lower_name": "兑",
    "upper_alias": "水", "lower_alias": "泽",
    "upper_symbol": "☵", "lower_symbol": "☱",
    "yao_order": 2, "yao_name": "九二",
    "gua_order": 60, "gua_symbol": "䷻", "gua_name": "水泽节", "full_name": "䷻水泽节",
    "gua_ci": "节，亨，苦节不可贞。",
    "core_gang": "节制有度过犹不及，控制仓位甘节则吉，仓位管理决定盈亏。",
    "yao_ci": "不出门庭，凶",
    "market_judgement": "不敢出门错过机会，凶险",
    "biangua": "屯",
    "state_id": "60-2",
    "action_signal": "新开仓",
    "main_hexagram_id": 60, "main_hexagram_name": "水泽节",
    "changed_hexagram_id": 3, "changed_hexagram_name": "屯",
    "market_summary": "不敢出门错过机会，凶险",
    "line_index": 2, "line_name": "九二", "line_text": "不出门庭，凶",
    "hexagram_symbol": "䷻"
  },
  "algorithm": {
    "open_to_upper": "digit_sum(open) mod 8 (0→8)",
    "close_to_lower": "digit_sum(close) mod 8 (0→8)",
    "hl_to_yao": "digit_sum(high)+digit_sum(low) mod 6 (0→6)",
    "price_format": "派生Tushare因子前复权数据集（仓库直接读取，两位小数）",
    "adjust": "tushare_qfq"
  },
  "adjust_meta": {
    "price_plane": "L1_signal_price", "model": "dataset_precomputed",
    "dataset_id": "overlay_v1_...",
    "dataset_cutoff": 20260814,
    "symbol_first_date": 20000609, "symbol_last_date": 20260814,
    "symbol_row_count": 6230, "covers_asof": false
  },
  "notes": [
    "算法同未复权；价格直接读取仓库 Tushare 前复权数据（不做二次因子计算）：…",
    "请求日期 20260828 非交易日或无日线，已使用最近交易日 20260814。"
  ],
  "summary": {
    "full_name": "䷻水泽节", "yao_name": "九二", "state_id": "60-2",
    "action_signal": "新开仓",
    "market_judgement": "不敢出门错过机会，凶险",
    "upper": "水(6)", "lower": "泽(2)", "yao_order": 2,
    "gaodao_commerce": "货物充积，时价得宜，本可获利，乃因拘墟失时，反致耗损。",
    "gaodao_category": "营商", "gaodao_is_fallback": false
  }
}
```

**字段速查**：

| 字段 | 含义 |
|---|---|
| `bar` | 起卦所用 K 线（`period=WEEK/MONTH` 时为聚合周K/月K，含 `start_date/end_date/n_days/closed`） |
| `bagua.full_name` | 卦名（本卦，含卦符） |
| `bagua.biangua` / `changed_hexagram_name` | 变卦名 |
| `bagua.state_id` | 384 爻唯一编号（`卦序-爻序`），跨接口对齐用 |
| `bagua.yao_ci` / `line_text` | 爻辞原文 |
| `bagua.market_judgement` | 爻辞白话解读（市场视角） |
| `bagua.action_signal` | 操作信号：新开仓 / 加仓 / 持有 / 减仓 / 清仓 等 |
| `bagua.core_gang` | 卦核心断语 |
| `summary.gaodao_commerce` | 《高岛易断》问营商断语（覆盖 379/384 爻；缺失时为时运/功名兜底，`gaodao_is_fallback=true` 标注） |
| `notes` | 人类可读的口径说明（回退、未收官等） |

> 指数/ETF 查询走同一接口（`code=sh510300`），返回结构一致（`symbol_type` 区分），指数/ETF 恒按未复权计算。

### 3.2 批量卦象查询

```
POST /api/v1/bagua/batch/query
Content-Type: application/json

{
  "codes": ["600000", "000001", "sh510300"],   // 或 "all_stocks": true 全市场
  "date": "2026-08-14",
  "period": "DAY",          // DAY | WEEK | MONTH，单周期
  "adjust": "tushare_qfq",
  "limit": 100              // 可选，截断行数
}
```

**响应**（节选）：`results` 数组每项与 §3.1 单票响应**完全同构**（多了 `error` 字段承载单票失败原因）：

```json
{
  "ok": true, "query_date": 20260814, "period": "DAY", "adjust": "tushare_qfq",
  "all_stocks": false, "requested": 2, "count": 2, "ok_count": 2, "error_count": 0,
  "results": [ { "...同 3.1 单票结构..." } ]
}
```

> `all_stocks=true` 时返回全市场（约 5200+ 股票），响应体较大（数 MB），建议用分页思路（`limit`）或直接走 §4 导出接口。全市场周K/月K口径批量查询耗时较长（分钟级），优先考虑导出接口。

### 3.3 边缘情况与错误行为（对接方必读）

| 场景 | HTTP | 行为 |
|---|---|---|
| 请求日期无数据 / 非交易日 | 200 | **自动回退最近有数据的交易日**，`notes` 注明；以 `bar.date` 为准 |
| 请求日期为未来日期（无论多远，如 2099-12-31） | 200 | **自动回退数据最后一根K线**，`bar_date_exact=false`；不会报错 |
| 请求日期早于该股首根K线（如 1990-01-01 查 600000） | 404 | `{"detail": "无 X 当日或之前K线"}` |
| 股票代码不存在 | 400 / 404 | `normalize_query_code` 抛 ValueError→400；无行情→404 |
| `period` / `adjust` 非法 | 400 | `{"detail": "..."}`（如 `tdx_front` 已停用） |
| `date` 过短（<4 字符，如 `abc`） | 422 | FastAPI 参数校验失败，`detail` 为校验错误数组 |
| `date` 长度达标但无法解析（如 `abcd`） | 400 | `{"detail": "invalid date: abcd"}` |
| 指数/ETF 请求 `tushare_qfq` | 200 | 自动按未复权计算，`notes` 注明"请求的复权口径对指数/ETF 不适用" |
| 周/月K未收官（查询当日所在周/月） | 200 | `bar.closed=false`，`notes` 注明"卦象可能随后续交易日变化" |
| 异步 job 未完成时取结果 | 409 | `{"detail": "job not ready: running"}`，继续轮询 |
| job_id 不存在 | 404 | `{"detail": "export job not found: ..."}` |

**服务重启的影响**：异步 job（导出 / 全市场同卦扫描）状态存于内存，服务重启后未完成 job 丢失，需重新发起；已完成的导出文件常驻磁盘可重复下载。

### 3.4 同卦匹配（找同一爻位的其他股票）

```
GET  /api/v1/bagua/same-gua?code=600000&date=2026-08-14&period=DAY&adjust=raw&limit=50
POST /api/v1/bagua/same-gua        // body: {"code","date","period","adjust","scope":[...],"limit"}
```

`scope` 传 ≤50 个股票代码则同步返回（快）；不传或 >50 个默认走异步 job，返回 `job_id` 后轮询：

```
GET /api/v1/bagua/same-gua/jobs/{job_id}         // 状态：status/message/progress
GET /api/v1/bagua/same-gua/jobs/{job_id}/result  // status=done 后取结果（409=未完成）
```

**同步响应示例**（`limit=5`，节选）：

```json
{
  "ok": true, "mode": "sync", "match_key": "60-2",
  "query_date": 20260814, "period": "DAY", "adjust": "raw",
  "all_stocks": true, "scanned": 6982, "count": 5,
  "target": {"code": "sh600000", "name": "浦发银行", "state_id": "60-2", "...": "..."},
  "results": [
    {"ok": true, "code": "sh600586", "name": "金晶科技", "state_id": "60-2",
     "full_name": "䷻水泽节", "yao_name": "九二", "yao_order": 2, "...": "..."}
  ]
}
```

### 3.5 同日柱匹配

```
GET  /api/v1/bagua/same-rizhu?code=600000&limit=50
POST /api/v1/bagua/same-rizhu     // body: {"code","scope":[...],"limit"}
```

返回与目标股票静态**日柱**（六十甲子，如"丙寅"，来自日柱表/上市日期推算）相同的其他股票，结构与 §3.4 类似（`match_key` 为日柱干支）。

### 3.6 卦爻知识库（384 爻全量数据）

```
GET /api/v1/gua/states?search=&main_hexagram_id=&action_signal=&page=1&page_size=50
```

分页返回 384 爻明细：`state_id`、卦名、卦辞/爻辞、白话解读（`market_judgement`）、操作信号、变卦、高岛断语、上下卦——**每条字段与 §3.1 `bagua` 对象一致**。支持按卦号、行动信号、关键字过滤。适合对方把整个知识库镜像到自己的系统。

```
GET /api/v1/gua/hexagrams
```

64 卦全量（含每卦六爻明细 `lines`）、规则版本、高岛覆盖度。适合做下拉选择器/展示页。

### 3.7 周期口径说明（写进对方产品展示时注意）

- `period=WEEK`：查询日所在自然周的周K（周一~周五聚合）；查询日所在周尚未收官时（周中查询），`bar.closed=false` 且 `bar.end_date` 截至查询日，卦象可能变化，`notes` 会注明。
- `period=MONTH`：查询日**所在自然月**的月K（**不是上一个月**）；月内查询时 `bar.closed=false`，卦象可能随后续交易日变化。注意：周报**导出**（§4.3）的月卦列按"目标周最近已收官月"归属（上月口径），与**查询接口**的 MONTH 口径不同，两者勿混用。

---

## 4. Excel 全字段数据（周报导出）

### 4.1 导出接口

```
POST /api/v1/bagua/export?async_mode=false|true      // JSON 提交
GET  /api/v1/bagua/export?date=...&period=DAY,WEEK,MONTH&adjust=tushare_qfq&all_stocks=true&async_mode=true
```

**Python 调用示例（全市场异步三步）**：

```python
import time
import requests

BASE = "http://127.0.0.1:8080"

# 1) 启动导出 job
job = requests.post(
    f"{BASE}/api/v1/bagua/export",
    json={"all_stocks": True, "date": "2026-08-14",
          "periods": ["WEEK", "MONTH"], "adjust": "tushare_qfq"},
    timeout=10,
).json()
job_id = job["job_id"]

# 2) 轮询进度（10~30 秒一次；全市场约 4 分钟）
while True:
    st = requests.get(f"{BASE}/api/v1/bagua/export/jobs/{job_id}", timeout=10).json()
    print(st["status"], st.get("message"))
    if st["status"] in ("done", "error"):
        break
    time.sleep(15)

# 3) 下载 xlsx
resp = requests.get(f"{BASE}/api/v1/bagua/export/jobs/{job_id}/download", timeout=60)
resp.raise_for_status()
open("weekly.xlsx", "wb").write(resp.content)
```

**请求参数**：

| 参数 | 说明 |
|---|---|
| `date` | 导出基准日（周报口径锚点，见 §4.3） |
| `period` / `periods` | 逗号分隔或列表；布局恒为 WEEK+MONTH（DAY 自动忽略） |
| `adjust` | `tushare_qfq`（推荐）/ `raw` |
| `all_stocks` | `true` 全市场（股票+ETF 全部池子）；`false` 时必须给 `codes` |
| `codes` | 自选代码列表（股票进 stock-all sheet，指数/ETF 进 etf-all sheet） |
| `limit` | 可选总行数上限（股票优先占额） |
| `async_mode` | 全市场默认 true：立即返回 job，轮询后下载；小批量可 false 同步直接返回 xlsx 文件流 |
| `review_rules` | 可选，信号规则勾选。**不传** = 默认行为（复用周五链复核结果，追加其中全部信号规则 sheet）；**空数组 / 空字符串** = 不附带任何信号 sheet；**非空** = 只导出勾选规则，未预计算/不存在的规则即时计算 |

> `review_rules` 两种传法：POST body 传数组（`"review_rules": ["txt_735金叉及趋势", "user_xxx"]`）；GET query 传逗号分隔字符串（`review_rules=txt_735金叉及趋势,user_xxx`）。GET 传空串（`review_rules=`）等价于 POST 传 `[]`。勾选未预计算/不存在的规则时会即时计算（不写回周五链复核文件；自定义规则的 sheet 名取规则显示名），**全市场导出耗时增加数分钟**，自选小批量秒级。

**小批量（同步）调用**——直接得到 xlsx 文件：

```bash
curl -X POST "http://<host>:8080/api/v1/bagua/export?async_mode=false" \
  -H "Content-Type: application/json" \
  -d '{"codes":["600000","000001","sh510300"],"date":"2026-08-14","periods":["WEEK","MONTH"],"adjust":"tushare_qfq"}' \
  -o export.xlsx
```

> **同步导出响应头**：`X-Bagua-Review-AsOf`（信号 sheet 实际使用的复核基准日，未解析信号基准日时为空）、`X-Bagua-Review-Note`（复核/回退说明，**URL 编码**，需 decode，如 Python `urllib.parse.unquote`）、`X-Bagua-Review-Fallback`（`1`=信号基准日与请求 `date` 不一致（数据面回退或复用了更早的复核结果），`0`=一致）。异步 job 无这些响应头，等价信息见 job 状态 JSON 的 `review_asof_used` / `review_note` / `review_fallback` 字段。

**全市场（异步）调用**——三步：

```bash
# 1) 启动导出 job
curl -X POST "http://<host>:8080/api/v1/bagua/export" \
  -H "Content-Type: application/json" \
  -d '{"all_stocks":true,"date":"2026-08-14","periods":["WEEK","MONTH"],"adjust":"tushare_qfq"}'
# → {"ok":true,"mode":"async","job_id":"bqexp_45be9bb3284e","status":"running","message":"正在准备…",...}

# 2) 轮询进度（建议 10~30 秒一次；全市场约 3~5 分钟，实测 6982 票 ~4 分钟）
curl http://<host>:8080/api/v1/bagua/export/jobs/bqexp_45be9bb3284e
# → {"status":"running","progress":{"done":3000,"total":6982,"ok_count":3000,"error_count":0,...},...}
# → {"status":"done","filename":"bagua_weekly_all_20260814_tushare_qfq_20260903_234820.xlsx",...}

# 3) 下载 xlsx
curl http://<host>:8080/api/v1/bagua/export/jobs/bqexp_45be9bb3284e/download -o weekly.xlsx
```

辅助端点：`GET /api/v1/bagua/export/jobs`（列出全部导出/匹配 job 及状态）。

### 4.2 Excel 结构（真实导出实测）

一次全市场导出（2026-08-14，tushare_qfq，未传 `review_rules`）实测包含 **5 个 sheet**（信号 sheet 随勾选与复核可用性变化）：

| Sheet | 内容 | 实测行数 |
|---|---|---|
| `meta` | 口径元数据键值对（见 §4.4） | 32 项 |
| `stock-all` | 全市场 A 股 | 5218 |
| `etf-all` | 全部 ETF | 1678 |
| `735` | 当日命中"735"公式的股票（周五链指标复核） | 84 |
| `5日外` | 当日命中"5日外"公式的股票 | 6 |

> 信号 sheet 生成规则（自选 codes 导出同样会追加）：
> - 优先复用最近一次复核结果（回看窗口 7 个自然日，周五链产物在周末/下周初导出仍可用）；信号 sheet 的行、周/月列头按复核基准日生成，可能与主表请求 `date` 不同（`meta.indicator_review_asof` 与 `indicator_review_query_date` 区分）。
> - 显式勾选 `review_rules` 时，复核结果里没有的规则（未预计算/不存在）会即时计算；复核缺失/过期且未勾选时，不生成信号 sheet，原因见 `meta.indicator_review_note`。
> - 即时计算失败（规则不存在/编译失败/数据面 no_go）时，仍生成一张**只有表头的占位空 sheet**，原因写入 `meta.indicator_review_placeholders` 与 `meta.indicator_review_note`。
> - 规则 0 命中同样生成只有表头的空 sheet（明确"确实无票"而非"sheet 丢失"）。
> - `review_rules` 为空时不生成任何信号 sheet；`meta.indicator_review_sheets` 列出实际生成的信号 sheet。

**列结构**（非跨月周 17 列；跨月周自动扩展为 21 列，多一组月卦）：

```
code | name | week_end | open | high | low | close | 日柱 |
周卦周线-组合(2026-W33) | 爻辞解释 | 周·高岛易断 | 周·倾向 |
月卦月线-组合(2026-07) | 爻辞解释 | 月·高岛易断 | 月·倾向 |
数据状态
```

**实测首行数据**（600000 浦发银行）：

| code | name | week_end | open | high | low | close | 日柱 | 周卦组合 | 爻辞解释 | 周·高岛易断 | 周·倾向 | 月卦组合 | 爻辞解释 | 月·高岛易断 | 月·倾向 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 600000 | 浦发银行 | 2026-08-14 | 9.2 | 9.38 | 9.06 | 9.1 | 丙寅 | 火泽睽\|5-天泽履 | 懊悔消散主线吃肉，跟进无咎 | 防合伙者有侵食之患，然径行而往，终得有利。 | （空） | 泽山咸\|2-泽风大过 | 小幅波动动则凶，持仓不动则吉 | 不利行商，利坐贾。 | （空） |

各列含义：

| 列 | 含义 |
|---|---|
| `week_end` | 周K截止日（该周最后交易日） |
| `open/high/low/close` | 该周周K的 OHLC（周卦起卦价格） |
| `日柱` | 该股票上市日的六十甲子日柱（Excel 日柱表优先，次新股/ETF 按上市日期推算补齐） |
| `周卦周线-组合(周标签)` | 本卦全名 `\|` 爻序-变卦名，如 `火泽睽\|5-天泽履`；括号内为 ISO 周标签 |
| `爻辞解释` | 该爻白话市场解读（= JSON `summary.market_judgement`） |
| `周·高岛易断` | 《高岛易断》营商断语（= JSON `summary.gaodao_commerce`） |
| `周·倾向` / `月·倾向` | 卦象信号与高岛断语的共识结论：▲双好 / ▼双差 / 分歧 / 空（仅解读参考） |
| `数据状态` | 正常行为空；失败行为结构化原因（`period[状态]: 原因`） |

### 4.3 周报口径（前瞻性，务必向最终用户说明）

导出是**为下一交易周做准备**的周报：

- **周卦**取基准日所在周（周五晚导出 → 本周收官周K）。
- **月卦**按**目标周**（锚点 = 基准日 + 3 天所在 ISO 周，即导出后即将交易的那一周）的交易日归属"最近已收官月份"：
  - 周五晚~周日导出：目标周 = 下周；
  - **跨月周**（目标周横跨两个月界，如 2026-08-31 所在周）：自动输出**两组**月卦列（8/31 用 2026-07 月卦、9/1~9/4 用 2026-08 月卦），列头以 `,适用日期段` 标注各自生效区间；
  - 周末导出时若次月月K未收官，第二组为按最新日线计算的临时卦象（`meta.note` 注明）。
- 全部口径细节以 `meta` sheet 为准（见下）。

### 4.4 meta sheet（口径与统计）

导出文件首个 sheet，键值对形式。关键字段：

```
layout                    = weekly_analysis stock-all
query_date                = 20260814
month_asof / month_asof_list / month_applies   （月卦起卦月与适用段）
periods                   = WEEK,MONTH
adjust                    = tushare_qfq
stock_count / etf_count / requested
sheets                    = meta,stock-all,etf-all,735,5日外
indicator_review_asof / indicator_review_query_date / indicator_review_sheets
indicator_review_rules_selected / indicator_review_rule_sources / indicator_review_placeholders / indicator_review_note
ok_total / error_total / rizhu_hit
rizhu_note                = Excel 日柱表优先；次新股/ETF 按上市日期推算 60 甲子补齐
gaodao_coverage           = 高岛断语覆盖度（379/384）
consensus_note            = 倾向列判定规则说明
exported_at               = 导出时间戳
```

`indicator_review_*` 字段含义（信号 sheet 相关）：

| 字段 | 含义 |
|---|---|
| `indicator_review_asof` | 信号基准日（实际复用的复核日 / 即时计算日 / 收敛后的数据面日）；`review_rules` 显式为空时不解析信号基准，此字段为空 |
| `indicator_review_query_date` | 导出请求的基准日（= 主表 `query_date`；信号 sheet 回看/回退前的原始日期） |
| `indicator_review_sheets` | 实际生成的信号 sheet 名（逗号分隔，含占位空 sheet） |
| `indicator_review_rules_selected` | 本次勾选的规则 ID（逗号分隔）；未传 `review_rules` 时为 `(default)`（复用复核结果全量规则），显式空时为空 |
| `indicator_review_rule_sources` | 每条规则的来源：`规则ID=precomputed:基准日`（复用复核结果）/ `computed:基准日`（即时计算）/ `placeholder:基准日`（占位空表），`;` 分隔 |
| `indicator_review_placeholders` | 占位空 sheet 及原因：`sheet=原因`，`;` 分隔；无占位时为空 |
| `indicator_review_note` | 复核读取/回退/即时计算/占位的人类可读说明（同步导出随 `X-Bagua-Review-Note` 响应头返回） |

### 4.5 JSON 与 Excel 字段对照（对方自建展示用）

若对方不想拉文件、只用 JSON 组装与 Excel 等价的数据：

| Excel 列 | JSON 来源（§3.1 响应） |
|---|---|
| `open/high/low/close` + `week_end` | `bar.open/high/low/close` + `bar.end_date`（`period=WEEK`） |
| `日柱` | 同日柱接口 `match_key`（`GET /api/v1/bagua/same-rizhu?code=X&limit=1` 的 target 内含 `rizhu`） |
| `组合` | `summary.full_name` + `bagua.yao_order` + 变卦名（`bagua.changed_hexagram_name`）拼装：`本卦\|爻序-变卦` |
| `爻辞解释` | `summary.market_judgement` |
| `·高岛易断` | `summary.gaodao_commerce` |
| `·倾向` | **不在 JSON 中直接提供**：由 `summary.action_signal` 与高岛断语合成（规则见 meta `consensus_method`）。如需完全一致成口，建议直接用导出文件；或按 `action_signal`（新开仓/加仓=好，减仓/清仓=差，持有=中）自行合成近似版 |
| `数据状态` | 各周期子查询的 `error/error_reason` |

### 4.6 导出性能参考

| 规模 | 模式 | 实测耗时（本地快照） |
|---|---|---|
| ≤ 3 只自选 | 同步 | 秒级 |
| 全市场 6982 票（WEEK+MONTH） | 异步 job | 本地快照实测约 4 分钟；服务器约 17~18 分钟（v2.9 性能优化的**外推值**，未在服务器实测，以实际为准） |
| 导出文件大小 | 全市场 | 约 2 MB |

> 全市场异步 job 在服务端内存中运行；服务重启会丢失未完成 job（job 列表不持久化），重启后需重新发起。文件生成后常驻 `bagua_exports` 目录可重复下载。

---

## 5. 对接注意事项（务必阅读）

1. **认证与网络**：对接方（daily-stock，:8000）与本系统（astock，:8080）**同机部署**，推荐直接用 `http://127.0.0.1:8080` 对接——流量不出服务器，无需 Token；公网 IP 当前无认证（**不视为正式通道**），正式开放前我方将部署 Bearer Token 鉴权 + IP 白名单，届时另行通知。
2. **频率建议**：
   - `quick`：服务端有 60 秒结果缓存，可放心较高频（如用户浏览触发）；
   - `bagua/query` / `batch/query`：**无响应级缓存**（每次实时计算；数据面有 5 分钟会话索引缓存，重复查询更快但结果不缓存），高频调用请在客户端侧自行缓存；
   - `batch/query all_stocks=true` / `same-gua` 全市场同步扫描：分钟级耗时，**不要**高频调用；全市场数据请走导出 job；
   - 导出 job：单服务建议同一时间 ≤ 1~2 个全市场导出。
3. **日期回退**：所有卦象接口在请求日无数据时自动回退最近交易日；以响应 `bar.date` 为准。
4. **数据更新节奏**：每周五 18:30（+ 指数/ETF 链、23:00 CA 链）自动增量；周五晚至周一早上之间查询，数据为上周五收盘后状态。用 `data-health` 接口确认 cutoff。
5. **`tdx_front` 已停用**：请求该口径返回 400，请使用 `tushare_qfq` 或 `raw`。
6. **高岛断语覆盖 379/384 爻**：个别爻为空属正常（原书无该爻占断），`gaodao_is_fallback=true` 表示该爻用了时运/功名兜底类别。
7. **`倾向` 列**为解读参考（不构成投资建议），周/月倾向独立判定，同一股票两个结论可能不同，不要合并展示。

---

## 6. 端点速查表

| 能力 | 端点 |
|---|---|
| 个股速览（行情+日卦+周卦） | `GET /api/v1/quick/{code}` |
| 单票卦象（任意周期/复权） | `GET /api/v1/bagua/query` |
| 单票卦象（POST 别名） | `POST /api/v1/bagua/query` |
| 批量/全市场卦象 | `POST /api/v1/bagua/batch/query` |
| 同卦匹配（同步/异步） | `GET/POST /api/v1/bagua/same-gua`（+ `/jobs/{id}`、`/jobs/{id}/result`） |
| 同日柱匹配 | `GET/POST /api/v1/bagua/same-rizhu` |
| 指数/ETF 清单 | `GET /api/v1/bagua/watchlist` |
| 指数/ETF 成分股 | `GET /api/v1/bagua/constituents` |
| 384 爻知识库（分页/筛选） | `GET /api/v1/gua/states` |
| 64 卦全量 | `GET /api/v1/gua/hexagrams` |
| 周报 Excel 导出（同步/异步） | `GET/POST /api/v1/bagua/export` |
| 导出 job 列表/状态/下载 | `GET /api/v1/bagua/export/jobs`、`/jobs/{id}`、`/jobs/{id}/download` |
| 数据健康度 | `GET /api/v1/system/data-health` |
| 交易日历范围 | `GET /api/v1/calendar/range` |
| 服务健康/版本 | `GET /api/v1/health`、`GET /api/v1/version` |

> 回测、实验、研究任务等操作型接口（`/api/v1/backtests`、`/experiments`、`/research` 等）不属于本供数范围，如需开放另行商定。

---

## 7. 附：接口能力对照表（对照对接方 735 API 场景）

对接方 735 系统当前提供的能力（每日 735 选股记录查询）与本系统的关系：

| 对接方 735 API 有 | 本系统对应能力 | 差异说明 |
|---|---|---|
| Bearer Token 认证（`dsa_kb_*`） | 无（同机对接不需要；公网须我方另建） | 两套 Token 不通用 |
| `date` 留空取最新 | `/quick`、`bagua/query` 传空/未来日期自动回退最近交易日；`data-health` 可查最新 cutoff | 语义近似，实现不同 |
| 统一信封 `success/error_code/message` | HTTP 状态码 + `detail` | 客户端处理方式不同（见 §1.3） |
| — | **本系统独有**：卦象计算（64卦/384爻）、日/周/月三周期、指数/ETF 卦象、成分股、日柱、高岛断语、全字段周报 Excel、384爻知识库 | 735 选股名单可通过本系统 §3.1 接口逐票补充卦象/名称等字段 |

**组合建议**：对接方拿到自己的 735 名单后，可用 `POST /api/v1/bagua/batch/query`（codes=名单，period=DAY/WEEK/MONTH）为每票挂上卦象字段——同机调用无认证开销，单批毫秒~秒级。

```python
# 组合示例：735 名单 + 卦象字段
import requests

picks = requests.get(
    "http://127.0.0.1:8000/api/v1/strategy/735/picks/daily",
    headers={"Authorization": "Bearer dsa_kb_xxxxxxxxxxxx"},
    timeout=10,
).json()["picks"]

codes = [p["stock_code"] for p in picks]
gua = requests.post(
    "http://127.0.0.1:8080/api/v1/bagua/batch/query",
    json={"codes": codes, "date": picks[0]["trigger_date"],
          "period": "DAY", "adjust": "tushare_qfq"},
    timeout=60,
).json()
for r in gua["results"]:
    if r.get("ok"):
        print(r["code"], r["name"],
              r["summary"]["full_name"], r["summary"]["action_signal"])
```
