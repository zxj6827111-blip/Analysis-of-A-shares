# A股多周期指标回测系统

基于 **wtpy**（WonderTrader Python 子框架）构建的 **A 股全市场回测与研究平台**，支持 TN6 指标导入、多周期（日/周/月）信号生成、参数实验、预测周报等完整工作流。

> 本仓库 fork 自 [wondertrader/wtpy](https://github.com/wondertrader/wtpy)，在保留原 wtpy 底层框架（`wtpy/wrapper`、`wtpy/monitor`、`demos`）的基础上，新增了完整的 A 股业务层：`wtpy/apps/astock/`。

---

## 目录

- [核心功能](#核心功能)
- [目录结构](#目录结构)
- [安装方法](#安装方法)
- [外挂数据（E:\AStockData）](#外挂数据eastockdata)
  - [数据总览](#数据总览)
  - [数据集仓库格式（market_data）](#数据集仓库格式market_data)
  - [原始数据目录格式（raw）](#原始数据目录格式raw)
  - [复权因子目录格式（factors）](#复权因子目录格式factors)
  - [项目内本地存储（storage/astock）](#项目内本地存储storageastock)
- [环境变量配置](#环境变量配置)
- [数据同步](#数据同步)
- [启动 Web 控制台](#启动-web-控制台)
- [命令行工具](#命令行工具)
- [回测功能说明](#回测功能说明)
- [指标导入与配对](#指标导入与配对)
- [高岛易断解读层](#高岛易断解读层)
- [运行测试](#运行测试)

---

## 核心功能

- **多数据源行情**：TdxQuant（通达信量化终端）、Tushare、通达信本地 `.day` 文件、本地供应商年度 ZIP 包，四种来源统一落入内容寻址数据集仓库
- **TN6 指标体系**：导入通达信 TN6 公式包并与人工维护的公式源文件显式配对（不逆向破解），支持编译校验、依赖分析、注册表管理
- **多周期信号**：日线（DAY）、周线（WEEK）、月线（MONTH）、DWM 共振信号，支持 T+1 延迟入场、指定星期买卖
- **复权体系**：原始价（raw）、通达信前复权（front）、Tushare 官方前复权（qfq）、Tushare 复权因子推导 QFQ（tushare_factor_qfq）、复合数据集（composite_*，退市股补全）、asof 前瞻复权，正式模式默认 fail-closed 防前视偏差
- **研究平台**：参数实验（自由轴/网格）、任务队列（SQLite 持久化）、漂移监控、截面研究、结果评分与 Excel 导出
- **预测周报**：每周选股周报导入、搜索、评分与导出
- **关键发现 Dashboard**：`/dashboard` 只读面板聚合数据健康、同步状态、Top 实验发现与自选池，一键掌握系统状态
- **个股快速查询**：顶栏搜索框 / `/quick.html` 输入代码或名称，直达行情概览、当前卦象与相关回测
- **高岛易断解读层**：384 爻旁挂《高岛易断》「问营商」断语（覆盖 379/384），在卦象查询卡片、个股快速查询页、爻象目录与导出 Excel 中展示；**仅作解读，不参与选股与回测**
- **Web 控制台**：FastAPI（路由按域拆分为 `api_routes/`）+ 单文件前端（`/v3` 为主界面），回测任务异步执行、进度实时推送
- **数据治理**：不可变 manifest + SHA256 内容寻址、严格发布策略（no_data 必须显式白名单）、幸存者偏差元数据记录、同步任务锁与断点续传

---

## 目录结构

```
wtpy-master/
├── wtpy/                        # wtpy 库本体（底层框架 + A股业务层）
│   ├── wrapper/                 #   与 C++ 底层对接的接口模块（x64/x86/linux DLL）
│   ├── monitor/                 #   内置监控服务（web 控制台）
│   └── apps/
│       ├── datahelper/          #   数据落地辅助模块（原始 wtpy）
│       └── astock/              # ★ A股回测系统核心
│           ├── cli.py           #   命令行入口（python -m wtpy.apps.astock）
│           ├── api.py           #   FastAPI 组装器（路由在 api_routes/，端口 8765）
│           ├── api_routes/      #   路由模块（rules/backtests/experiments/research/forecast/bagua/system）
│           ├── config.py        #   运行时配置（路径、环境变量解析）
│           ├── strategy.py      #   组合回测器
│           ├── study.py         #   信号研究（DWM 共振、组合信号等）
│           ├── bagua/           #   卦象体系
│           │   ├── calculator.py      # OHLC 数字和起卦（不含高岛逻辑）
│           │   ├── bagua_384.json     # 384 爻知识库（sha256 绑定权威 Excel）
│           │   ├── bagua_gaodao.json  # ★《高岛易断》营商断语 sidecar（旁挂，379/384）
│           │   └── gaodao.py          # sidecar 读取模块（fail-open，唯一入口）
│           ├── data/            #   数据层
│           │   ├── dataset_store.py   # 内容寻址数据集存储（blobs/manifests）
│           │   ├── repository.py      # 数据集仓库（解析/绑定/派生）
│           │   ├── data_store.py      # 本地 DataStore（旧格式 csv/npz）
│           │   ├── adjustments.py     # 复权因子构建（正式/研究模式）
│           │   ├── affine_adjust.py   # 仿射前复权（TDX 1:1 匹配）
│           │   ├── providers/         # 数据源适配：tdxquant/tushare/tdx_local/local_vendor
│           │   └── ...                # 日历、股票池、公司行动、退市股、同步锁等
│           ├── forecast/        #   预测周报服务（知识库 KB）
│           ├── indicators/      #   TN6 导入、公式编译、注册表
│           ├── research/        #   研究平台（实验/队列/评分）
│           ├── service/         #   业务服务（回测/任务/规则/运行管理等）
│           └── web/static/      #   前端（index.html / index_v3.html）
├── scripts/                     # 数据同步脚本
│   ├── sync_market_data.py      #   ★ 主同步程序（多源多模式）
│   ├── sync_ca_events.py        #   公司行动事件同步
│   ├── sync_tushare_delisted.py #   退市股数据同步
│   ├── build_gaodao_sidecar.py  #   《高岛易断》营商断语 sidecar 抽取
│   └── reconcile_sqlite_runs.py #   运行记录对账
├── deploy/                      # Linux 部署（install_astock.sh、PM2 配置）
├── tests/                       # pytest 测试（1422 个用例）
├── demos/                       # 原始 wtpy 官方示例
├── storage/astock/              # 项目内本地存储（见下文）
├── 指标/                        # 本地 TN6 包与公式源文件（已 gitignore）
├── .env                         # 本机环境配置（不入 Git，见 .env.example）
└── start_astock_serve.bat       # Windows 一键启动
```

---

## 安装方法

### 1. 环境要求

- **Python 3.8+**（建议 3.10+，已在本机 Python 3.14 验证）
- Windows（默认）或 Linux（见 `deploy/`）
- 外挂数据盘 `E:\AStockData`（见下文"外挂数据"章节）

### 2. 安装依赖

```shell
pip install -r requirements.txt
```

依赖列表：`numpy`、`pandas`、`chardet`、`pyyaml`、`xlsxwriter`、`openpyxl`、`pyquery`、`psutil`、`fastapi`、`uvicorn`、`deap`、`websockets>=10.4`、`pypinyin`

### 3. 配置 .env

复制 `.env.example` 为 `.env`，按本机修改（`.env` 已 gitignore，不会提交）：

```ini
# 部署环境: production | development | test
ASTOCK_ENV=production

# 正式行情数据根目录（必须显式设置；production 缺失将拒绝启动）
MARKET_DATA_ROOT=E:\AStockData\datasets\market_data

# 供应商原始日K ZIP 根目录（--source local_vendor 使用）
LOCAL_VENDOR_RAW_ROOT=E:\AStockData\raw\local_vendor\original_files\incoming

# Tushare adj_factor 原始 CSV 缓存目录（--adjustment adj_factor 使用）
TUSHARE_FACTOR_RAW_ROOT=E:\AStockData\factors\tushare\adj_factor

# Tushare API Token（仅在执行 tushare 同步时使用，永不被打印/持久化）
TUSHARE_TOKEN=your_token_here
```

> **注意**：`ASTOCK_ENV=production` 时，若 `MARKET_DATA_ROOT` 未设置或指向项目内部存储，系统会拒绝启动（防呆设计）。仅调试时可设 `ASTOCK_ALLOW_INTERNAL_DATA_ROOT=1` 临时放行。

**服务器部署要点**（Linux + PM2/systemd，详见 `deploy/`）：

- **`TUSHARE_TOKEN` 必配**：tushare 数据同步/因子同步依赖 Tushare Token，缺失将同步失败。可在启动前 `export TUSHARE_TOKEN=你的token`（PM2 启动时自动透传宿主环境变量），或写入 `deploy/astock.env`（`install_astock.sh` 生成，systemd 模式自动读取）
- **服务器时区**：建议设置 `TZ=Asia/Shanghai`（北京时间）。EOD 自动同步按服务器本地时间 18:30 触发，时区错位会导致自动更新时机错位。`install_astock.sh` 生成的 `deploy/astock.env` 与 `deploy/ecosystem.config.cjs` 均已内置 `TZ=Asia/Shanghai`
- **首次同步自动生成退市池**：手动点击「更新Tushare日线」或等待 EOD 自动同步，会自动补全退市股票数据（无需人工准备候选清单），随后自动合成正式 L1/L2 数据面；若个别股票合并失败，可在数据中心页点击「手动合并计算」查看失败原因或立即触发重算

### 4. 验证安装

```shell
python -m wtpy.apps.astock list-indicators
python -m wtpy.apps.astock inspect-data
```

### 5. 启动

```shell
# 方式一：双击（Windows）
start_astock_serve.bat

# 方式二：命令行
python -m wtpy.apps.astock serve --host 127.0.0.1 --port 8765
```

浏览器访问 <http://127.0.0.1:8765/>（旧版界面为 `/legacy`、`/v2`，主界面为 `/v3`，任务详情 `/v3/task-detail`）。

---

## 外挂数据（E:\AStockData）

行情数据采用**外挂数据盘**方式存放（默认 `E:\AStockData`，路径由 `.env` 的 `MARKET_DATA_ROOT` 等变量指定），与代码仓库分离。删除或重建代码目录**不影响**行情数据；反之，行情数据可独立备份/迁移。

```
E:\AStockData\
├── datasets\
│   └── market_data\            # ★ 数据集仓库（主数据）
│       ├── blobs\              #   内容寻址 K 线数据 {sha256}.npz
│       ├── manifests\          #   数据集清单（不可变 JSON）
│       ├── universes\          #   PIT 股票池 JSON（时点口径）
│       ├── ca_events\          #   公司行动（分红送股除权）事件 {code}.json
│       ├── sync_logs\          #   每次同步的运行日志 JSON
│       └── .locks\             #   同步任务锁（防并发）
├── raw\
│   ├── local_vendor\
│   │   ├── original_files\incoming\   # ★ 供应商年度 ZIP（一年一个）
│   │   ├── extracted\                 #   解压缓存
│   │   └── metadata\                  #   供应商元数据
│   └── tushare\
│       └── delisted_daily\raw\        #   退市股日线原始 CSV
└── factors\
    └── tushare\
        └── adj_factor\                # Tushare 复权因子原始缓存
            └── {sync_run_id}\         #   每次同步一个子目录，内含 {ts_code}.csv
```

### 数据集仓库格式（market_data）

这是系统唯一正式读取的行情数据源，由 `scripts/sync_market_data.py` 写入、`wtpy/apps/astock/data/dataset_store.py` 管理。

#### blobs/ —— 内容寻址 K 线

```
blobs/{sha256}.npz
```

- 每个文件是单只股票、单个（来源 × 复权口径 × 周期）组合的 K 线数组
- 文件名为内容本身的 **SHA256 摘要**：相同内容只存一份，内容变化必然产生新文件名（天然防篡改、防重复）
- npz 内部字段为 numpy 数组，K 线字段顺序与 `providers/base.py` 的 `MarketBar` 定义一致

#### manifests/ —— 数据集清单（不可变）

```
manifests/{dataset_id}.json
```

- 一个 manifest 描述一个**不可变数据集**（一次同步的产物），发布后只追加不修改
- `dataset_id` 命名规则：

```
{source}_{adjustment}_{period}_{cutoff或anchor}_{manifest_sha前12位}
```

| 字段 | 取值示例 | 说明 |
|---|---|---|
| source | `tdxquant` / `tushare` / `tdxlocal` / `localvendor` / `internal` | 数据来源 |
| adjustment | `none` / `front` / `qfq` / `adj_factor` / `tushare_factor_qfq` / `composite_none` / `composite_tushare_factor_qfq` / `asof_forward_qfq` | 复权口径 |
| period | `1d`（日）/ `1w`（周） | 周期 |
| cutoff | 如 `20260717` | 数据截止日期 |
| sha | manifest 内容摘要前 12 位 | 防碰撞/防混用 |

manifest 关键字段：

| 字段 | 说明 |
|---|---|
| `symbols[]` | 每只股票的记录：`symbol`（标准代码）、`blob_sha256`（指向 blobs/）、`first_date`/`last_date`/`row_count`、`quality`（ok/no_data/error） |
| `status` | `building`（构建中）→ `ready`（发布）/ `partial`（部分失败但按策略发布） |
| `source`/`adjustment`/`period` | 数据集口径（与 dataset_id 一致） |
| `dataset_type` | `bars`（K线）/ `factor`（复权因子） |
| `parent_dataset_id` | 派生数据集（如 composite、tushare_factor_qfq）记录父数据集 |
| `coverage_*` | 覆盖率统计（预期/导入/缺数/失败股票数） |
| `survivorship_bias` / `known_missing_delisted_*` | 幸存者偏差元数据与已知缺失退市股清单 |
| `provenance` / `formula_version` / `anchor_policy` | 派生口径、复权公式版本与锚定策略（可追溯） |

#### universes/ —— PIT 股票池

```
universes/pit_universe_1d_{date}_{sha}.json
```

时点（point-in-time）全市场股票池快照，用于按历史日期还原当时真实可交易标的，避免幸存者偏差。

#### ca_events/ —— 公司行动

```
ca_events/{code}.json        # 如 000001.SZ.json
```

每股分红、送转等公司行动事件，是复权因子构建和"前瞻复权（asof）"的锚定依据。

#### sync_logs/ —— 同步日志

每次同步运行写入一个 `{sync_run_id}.json`，记录运行 ID、数据集 ID、成功率、耗时、检查点信息等，可用于审计与断点续传。

### 原始数据目录格式（raw）

#### local_vendor（供应商年度 ZIP）

```
LOCAL_VENDOR_RAW_ROOT\           # 默认 E:\AStockData\raw\local_vendor\original_files\incoming
├── {year1}.zip                  # 一年一个 ZIP，内含当年全市场日K CSV
├── {year2}.zip
└── ...
```

- ZIP 为供应商原始日线数据（未复权），仅支持 `--source local_vendor --adjustment none --period 1d`
- 同步时按 500 只股票一个 chunk 分批解压读取，支持断点续传（`--resume`）
- 由于供应商数据通常缺少部分退市股票，发布时 manifest 会显式标记 `survivorship_bias=true` 并记录已知缺失清单

#### tushare（退市股日线）

```
E:\AStockData\raw\tushare\delisted_daily\raw\{code}.csv
```

从 Tushare 下载的退市股票日线原始 CSV，用于构造复合数据集（composite_*）补全 local_vendor 缺失的退市股。

### 复权因子目录格式（factors）

```
TUSHARE_FACTOR_RAW_ROOT\        # 默认 E:\AStockData\factors\tushare\adj_factor
└── {sync_run_id}\              # 每次同步一个子目录
    ├── {ts_code}.csv           # 如 600000.SH.csv，列: trade_date, adj_factor
    └── ...
```

- Tushare `pro.adj_factor` 接口的原始缓存（版本化，按 sync_run 隔离）
- 同步完成后因子数据本身进入数据集仓库的 `blobs/`（`dataset_type=factor` 的 manifest 引用）
- 原始 CSV 仅作缓存与审计用途

### 项目内本地存储（storage/astock）

以下文件在**项目目录内**（不入 Git），与数据集仓库互补：

| 路径 | 内容 | 可再生性 |
|---|---|---|
| `manifest.json` / `universe.json` / `calendar.json` / `config.json` | 全局目录、股票池、交易日历、配置 | 可重新生成，删除影响当前运行 |
| `indicators/registry.json` | 指标注册表（导入的 TN6/公式） | 从 `指标/` 目录重新导入 |
| `indicators/tn6_source_map.json` | TN6 包与公式源文件的配对记录 | 需重新执行配对 |
| `adjustments/` | 复权因子缓存（affine_*.json 等） | 自动重建 |
| `cache/` | 信号/执行缓存 | 自动重建 |
| `forecast/` | 预测知识库（KB）与周报数据 | 需重新导入 |
| `research_platform.db` | 研究平台 SQLite（实验/任务记录） | 不可再生，删除将丢失实验历史 |

---

## 环境变量配置

| 变量 | 必填 | 说明 |
|---|---|---|
| `ASTOCK_ENV` | 否 | `production` / `development` / `test`，默认 `development`；production 下有数据根目录防呆 |
| `MARKET_DATA_ROOT` | **是（production）** | 数据集仓库根目录，如 `E:\AStockData\datasets\market_data` |
| `LOCAL_VENDOR_RAW_ROOT` | local_vendor 同步时 | 供应商年度 ZIP 目录 |
| `TUSHARE_FACTOR_RAW_ROOT` | adj_factor 同步时 | 复权因子原始缓存目录 |
| `TUSHARE_TOKEN` | tushare 同步时 | Tushare API Token（通过 `ts.set_token()` 亦可） |
| `ASTOCK_ALLOW_INTERNAL_DATA_ROOT` | 否 | 调试用：`1` 允许 production 使用内部测试数据 |

---

## 数据同步

所有行情同步均通过 `scripts/sync_market_data.py` 执行：

```shell
# 全量同步
python scripts/sync_market_data.py --source tdxquant --mode full
python scripts/sync_market_data.py --source tushare --mode full
python scripts/sync_market_data.py --source tdx_local --mode full
python scripts/sync_market_data.py --source local_vendor --mode full
python scripts/sync_market_data.py --source all --mode full

# 增量同步（TdxQuant/Tushare）
python scripts/sync_market_data.py --source tdxquant --mode incremental
python scripts/sync_market_data.py --source tushare --mode incremental

# 复权因子同步（需 --universe-file 指定冻结股票池 CSV）
python scripts/sync_market_data.py --source tushare --adjustment adj_factor --mode full \
    --universe-file vendor_universe.csv

# 数据审计
python scripts/sync_market_data.py --source tdxquant --mode audit
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--start-date` / `--end-date` | 日期范围（YYYYMMDD） |
| `--anchor-date` | 锚定日期（asof 复权） |
| `--resume` | 断点续传（从检查点继续） |
| `--fresh` | 丢弃旧检查点重新开始 |
| `--symbol` | 仅同步指定股票（逗号分隔） |
| `--universe-file` | 冻结股票池 CSV（local_vendor/adj_factor 全量用） |
| `--chunk-size` | 分批大小（默认 500） |
| `--include-delisted` / `--include-bse` | 包含退市股 / 北交所 |
| `--skip-ca-detect` | 跳过公司行动检测（快速增量） |
| `--allow-no-data-file` | no_data 白名单 CSV（`symbol,reason`） |

**同步产出**：每个（来源 × 复权 × 周期）组合生成一个 `ready`（或 `partial`）状态的数据集 manifest + 全市场 blob 数据，随后 Web 控制台与回测即可使用。

其他数据脚本：

```shell
python scripts/sync_ca_events.py          # 同步公司行动事件
python scripts/sync_tushare_delisted.py   # 同步退市股日线（复合数据集素材）
python scripts/reconcile_sqlite_runs.py   # 对账研究平台运行记录
```

---

## 启动 Web 控制台

```shell
# Windows 一键
start_astock_serve.bat

# 手动
python -m wtpy.apps.astock serve --host 127.0.0.1 --port 8765
```

主要页面与 API 分组：

| 路径 | 说明 |
|---|---|
| `/` `/legacy` `/v2` | 旧版控制台 |
| `/v3` | 主界面（回测、任务、研究平台） |
| `/v3/task-detail` | 任务详情 |
| `/dashboard` | 关键发现面板（数据健康 + 同步状态 + Top 实验发现 + 自选池，只读） |
| `/quick.html?code=600000` | 个股快速查询（行情概览 + 当前卦象 + 相关回测） |
| `/api/v1/dashboard/overview` | Dashboard 聚合数据 |
| `/api/v1/quick/{code}` | 个股快速查询 API（支持裸码 / ts_code / 中文名） |
| `/api/v1/health` | 健康检查 |
| `/api/v1/market-data/status` | 数据仓库状态（各数据集就绪度、最新同步） |
| `/api/v1/rules*` | 指标规则 CRUD 与校验 |
| `/api/v1/backtests*` | 回测提交、异步任务队列、结果与权益曲线 |
| `/api/v1/runs*` | 运行记录、对比、产物下载、删除 |
| `/api/v1/experiments*` | 参数实验（创建/启动/取消/导出 xlsx） |
| `/api/v1/research/*` | 研究平台（任务、试验、搜索、调度、漂移、截面） |
| `/api/v1/forecast/*` | 预测知识库、周报上传/激活、搜索、导出 |
| `/api/v1/data-sync/*` | 后台数据同步任务的启动/停止/状态 |
| `/api/v1/ca-events/status` | 公司行动覆盖状态 |

---

## 命令行工具

所有命令通过 `python -m wtpy.apps.astock <command>` 调用：

```shell
# 指标管理
list-indicators                                        # 列出已注册指标
import-indicator <xx.tn6> --source <xx.txt> --note "..."   # 导入 TN6 并配对公式源
validate-indicator <indicator_id>                      # 编译校验
pair-735 --tn6 <735.tn6> --source <735.txt>            # 配对 735 包（不逆向）
confirm-indicator-source --tn6 <xx.tn6> --source <xx.txt> --confirmed-by <who> --confirm-user-provided
prune-source-map                                       # 清理失效配对记录

# 数据
inspect-data                                           # 检查通达信数据/交易日历
import-data --codes sh600000,sz000001                  # 从通达信导入（旧格式，一般用同步脚本代替）
rebuild-catalog                                        # 重建全局目录 manifest/universe
min60-status                                           # 检查通达信分钟线覆盖

# 信号与回测
build-signals --indicator <id> --period DAY --codes ...
backtest --indicator <id> --period DAY --hold 1 --entry-lag 1 \
         --account-mode portfolio --stop-loss 0.03 --take-profit 0.08
report <run_id>                                        # 查看一次运行的全部产物
```

---

## 回测功能说明

### 账户模式

| 模式 | 说明 |
|---|---|
| `portfolio`（默认） | 组合共享资金（初始 100 万，单票最大权重 10%） |
| `per_symbol` | 每只股票独立资金（TDX 风格） |
| `tdx` | 通达信风格 |

### 交易参数

| 参数 | 说明 |
|---|---|
| `--hold` | 持有天数（默认 1） |
| `--entry-lag` | 信号后第 N 个交易日开盘买入（默认 1 = T+1） |
| `--buy-on` / `--sell-on` | 开盘/收盘价成交 |
| `--signal-weekdays` | 仅接受指定星期信号（1=周一 … 7=周日） |
| `--buy-weekday` / `--exit-weekday` | 指定星期买入/强制平仓（覆盖 entry-lag/hold） |
| `--combine all/any` | 多指标信号组合（全部满足/任一满足） |
| `--dwm` | 日周月共振模式 |
| `--stop-loss` / `--take-profit` | 止损/止盈比例（0<x<1） |
| `--research-unadjusted` | 研究模式：允许未复权数据跑信号 |
| `--research-unconfirmed-formula` | 研究模式：允许未确认来源的公式跑回测 |

### 数据口径（fail-closed）

- **正式模式**：信号必须使用已确认来源的公式 + 复权口径数据集（默认 TDX 前复权或 Tushare QFQ），公司行动事件缺失会**拒绝运行**而不是静默降级
- **研究模式**：可显式放行未复权/未确认公式，产物会标记 `research_*` 状态
- 回测产物（权益曲线、成交、信号、统计）写入 `outputs/astock/{run_id}/`，可在 Web 界面查看与导出

### 费用模型

佣金 0.03%（最低 5 元）、印花税 0.1%（卖出）、滑点默认 0（可在 `config.py` 的 `CostConfig` 调整）。

---

## 指标导入与配对

系统支持通达信 TN6 公式包，但**不逆向破解 TN6 二进制**——要求与人工维护的公式源文本显式配对：

1. 将 `.tn6` 包和对应公式源 `.txt` 放入 `指标/` 目录（已 gitignore，不会提交）
2. 配对：

```shell
python -m wtpy.apps.astock pair-735 --tn6 "指标/735选股.tn6" --source "指标/735选股.txt"
```

3. 确认公式源确为人工提供（完成后才允许正式回测）：

```shell
python -m wtpy.apps.astock confirm-indicator-source \
    --tn6 "指标/735选股.tn6" --source "指标/735选股.txt" \
    --confirmed-by <你的名字> --confirm-user-provided
```

4. 验证：

```shell
python -m wtpy.apps.astock validate-indicator <indicator_id>
python -m wtpy.apps.astock list-indicators
```

---

## 高岛易断解读层

在既有 384 爻卦象体系之上**旁挂**《高岛易断》「问营商」断语，作为纯解读文本层。

### 定位与边界

- **只解读，不选股**：不参与 `GuaFilter` 过滤、不影响买卖信号、不进入 `signals.csv`，回测结果与加入前完全一致
- **不动起卦算法**：`bagua/calculator.py` 的 OHLC→卦爻映射（开盘 mod8 定上卦、收盘 mod8 定下卦、(高+低) mod6 定动爻）保持原样
- **不动知识库**：`bagua_384.json` 头部 `source_sha256` 与权威 Excel 绑定、且 `reimport_excel()` 会整体重建，因此高岛断语单独存 sidecar，互不干扰

### 数据来源与覆盖

| 项 | 值 |
|---|---|
| 源文件 | 《高岛易断》全文 txt（本机外挂目录，不入 Git） |
| sidecar | `wtpy/apps/astock/bagua/bagua_gaodao.json`（记录源文件 sha256 可追溯） |
| 配对口径 | 按 `(gua_order, yao_order)` —— 原书按通行本顺序编排，64 卦名与知识库逐卦一致 |
| 营商类命中 | 375 爻（别名：营商/商业/经商/买卖/贸易/营业/生意/财运） |
| 时运/功名兜底 | 4 爻（展示时追加「（时运）」类别后缀，避免口径误读） |
| 原书无占断 | 5 爻：`11-4`、`26-2`、`33-4`、`47-4`、`61-5`（留空，由 `market_judgement` 兜底） |
| **总覆盖** | **379 / 384** |

### 重建 sidecar

```shell
python -X utf8 scripts/build_gaodao_sidecar.py             # 生成/更新 sidecar
python -X utf8 scripts/build_gaodao_sidecar.py --dry-run   # 只看覆盖统计不写文件
python -X utf8 scripts/build_gaodao_sidecar.py --txt "D:\其他路径\高岛易断_全文.txt"
```

sidecar 缺失或损坏时系统 **fail-open**：所有高岛字段返回空串，卦象查询与导出照常工作。

### 展示位置

| 位置 | 表现 |
|---|---|
| `/v3` 卦象查询结果卡片 | 「卦辞 / 爻辞」面板之后新增「高岛易断·营商」面板 |
| `/quick.html?code=600000` | 日卦与周卦面板各新增一段高岛解读 |
| `/v3` 爻象勾选列表 / 搜索结果 / 按信号浏览 | 「行情：」之后追加「高岛：」片段 |
| 导出 Excel | 新增「周·高岛易断」（第 11 列）与「月·高岛易断」（第 14 列），meta sheet 记录来源与覆盖度 |
| `/api/v1/gua/states`、`/api/v1/gua/hexagrams` | 每爻增 `gaodao_commerce` / `gaodao_category`，返回体增 `gaodao_coverage` |
| `/api/v1/bagua/query`、`/api/v1/quick/{code}` | `summary.gaodao_commerce` / `summary.gaodao_category` |

导出表格列布局（共 15 列）：

```
0 code  1 name  2 week_end  3 open  4 high  5 low  6 close  7 日柱
8 周卦组合  9 爻辞解释  10 周·高岛易断
11 月卦组合 12 爻辞解释  13 月·高岛易断  14 数据状态
```

---

## 运行测试

```shell
python -m pytest tests/apps/astock -q        # 全量（1422 个用例）
```

仓库带有 GitHub Actions CI（`.github/workflows/ci.yml`）：每次 push / PR 自动在 Python 3.11 / 3.12 上运行全量测试（跳过 live_tdxquant / live_tushare 实盘标记用例）。

---

## 相关资源

- WonderTrader 官方：<https://github.com/wondertrader/wondertrader>
- wtpy 上游：<https://github.com/wondertrader/wtpy>
- WonderTrader 文档：<https://wondertrader.github.io/>
