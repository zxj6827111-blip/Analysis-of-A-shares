# 本地购买日线全量导入前 — 最终独立验收(第二轮)

**判定: `FAIL`**

- READY_FOR_FULL_DAILY_IMPORT: **false**
- FULL_IMPORT_RECOMMENDED: **false**
- 分支: `feat/multi-source-market-data` HEAD `cad0742746aa6f675b6fc4b17798ee43b482c2df`(工作区有 12 个未提交生产文件改动)
- 生成时间: 2026-07-26(UTC)
- 本报告取代 2026-07-26T04:27Z 的上一轮 FAIL 报告(已备份为 `E:\AStockData\reports\local_vendor_full_import_final_independent_acceptance_prev_20260726T0427.*`)
- ⚠️ 验收进行期间(本地 14:42)`scripts/sync_market_data.py` 与 `local_vendor.py` 仍在被修改;本报告以 14:42:46 后的文件状态为准。验收快照未冻结、未提交,复现时需以提交后的 commit 为基准重跑。

## 与上一轮相比:已确认修复的 P0

| 上轮 P0 | 本轮独立复核 |
|---|---|
| sync 无 local_vendor 源 | ✅ 已加入(`--source local_vendor`) |
| sync 硬编码内部数据根 | ✅ `get_storage_root()` 读 `MARKET_DATA_ROOT` |
| BacktestService 静默 fallback tdx_local | ✅ 已改硬失败(缺 ready 执行数据集→400/ValueError,实测) |
| 数据管理 API 字段缺失 | ✅ `/api/v1/market-data/status` 齐全(缺日期范围,P1) |
| 前端页面无数据仓库展示 | ✅ 数据仓库页从真实 API 渲染(根/环境标签/统计/明细表) |
| 历史并集宇宙未实现 | ✅ `fetch_universe` 已按 27 年 ZIP 并集(5796) |
| ZIP-first 性能 | ✅ 500 只演练 72s 属实(独立抽样核对通过) |
| dry-run/preflight 缺失 | ✅ 14:42 修改后已真实实现(实测不写数据) |
| 无并发锁 | ⚠️ 代码已加,但 **win32 上不可达**(见 P0-1) |

## 本轮新发现 P0(阻断)

### P0-1 正式全量命令在 Windows 上无法运行(并发锁同时失效)
`sync_local_vendor_full` 函数入口无条件 `import fcntl`([sync_market_data.py:349](../scripts/sync_market_data.py))。本机(win32/Python 3.14.3)实测:

```
python scripts/sync_market_data.py --source local_vendor --mode full --symbol SSE.STK.600000 --storage-root <临时目录>
→ ModuleNotFoundError: No module named 'fcntl'
```

崩溃发生在锁获取之前 → msvcrt 锁代码永远执行不到。**正式命令不可用 + 无有效并发锁**,两条硬 FAIL 条款同时触发。修复方向:`fcntl` 导入移入 POSIX 分支(`if sys.platform != "win32": import fcntl`)。

### P0-2 供应商日 K 数据存在幸存者偏差(历史退市股缺失)
- 乐视网 300104、长生生物 002680、华锐风电 601558、康得新 002450、邯郸钢铁 600001、齐鲁石化 600002 —— **在全部 27 个年度 ZIP 中均不存在**(乐视网 2010–2020 交易十年,任何年份都没有)。
- 2000.zip 共 873 只,其中 **870 只至今仍在 2026.zip**;并集 A 股 5224 与 2026 年 A 股 5218 仅差 6 只(000627/000851/300280/600200/601989/603388,全部为 2025–2026 近期退市)。
- 结论:年度 ZIP 是按"约 2025 年仍上市股票"**回填**的历史,不是真实时点数据。以此数据做 27 年回测存在系统性幸存者偏差,"完整、可追溯的 27 年历史宇宙"无法由该数据单独构成。
- 需用户决策:① 接受偏差并在所有报告显著标注;② 用 Tushare 补退市股日 K(开发侧已验证 Tushare 有 338 只退市股含日线与复权因子,见 `tmp/blocker_fix/delisted_verification.json`);③ 要求供应商补数。

### P0-3 "5055 只 A 股"任务口径与一切实测不符
| 口径 | 实测 |
|---|---|
| 2024 截面沪深 A 股 | **5097** |
| 2024 截面 A+北交 | 5359(北交 262 ✅ 与声称一致) |
| 2026 最新截面 | 5547 |
| 2000–2026 历史并集 | **5796**(A 股 5224 + 北交 571 + 302132 中航成飞) |

5055 不匹配任何一个数字,确认为某次截面式统计,**不是 27 年宇宙**。正式任务标题与宇宙定义必须改为"年度 ZIP 并集"口径(建议 5795 只 A+北交,并单列北交/近似退市纳入规则)。

### P0-4 正式数据根缺信号数据集,正式环境端到端回测当前不可运行
`E:\AStockData\datasets\market_data` 只有 4 个 local_vendor manifest;实测 `resolve_latest_ready`:tdxquant/front、tushare/qfq、tdx_local/none **全部 DatasetNotFound**。产品路径回测要求 signal 源 ∈ {tdxquant, tushare},故正式环境目前任何回测必然 400。"外部正式数据根回测验证已完成"的开发声称与事实不符。全量导入后仍需向正式根同步信号数据集。

## 独立实证通过项(硬门槛)

| 门槛 | 结果 | 证据 |
|---|---|---|
| 默认 pytest | ✅ | `python -m pytest -q`:**554 passed, 1 skipped(live_tdxquant), 0 failed**,92.95s;collect-only 555 项,tmp 未被收集,无未知 marker |
| 292 ZIP 分类 / unknown=0 | ✅ | 独立重分类:daily 54 + 分钟 236 + 复权因子 2 = 292,unknown=0,2000–2026 逐年完整 |
| SHA256 重复治理 | ✅ | 按体积配对 33 个多余副本共 4.22GB;6 组(4 日 K+2 分钟)重算 SHA256 全部一致;导入按年取单副本;跨 ZIP 重复日期去重实测(600960:5189→5175,14 个重复日) |
| 未复权确认 | ✅ | 沿用 301107 回归与 tdx_local 对照(默认套件内通过) |
| volume/amount 单位 | ✅ | 10 股(含北交 835185)×40 日=400 行原始 ZIP↔Repository 逐条 0 误差;VWAP 落在当日高低区间的单位推断 400/400 支持**千元**(万元 0 命中)。⚠️ local_vendor.py 模块头仍写"万元",文档需改 |
| execution 缓存隔离 | ✅ | 真实产品路径:同参数只换 execution_dataset_id → run_id 不同;**B 成交价恰为 A×1.01**(买 10.02→10.1202,卖 10.08→10.1808),证明各读各的数据集;SQLite 两行分别落库正确 execution_dataset_id;同数据集重跑结果逐字段一致 |
| execution_dataset_id 落库 | ✅ | runs 表 signal_data_source/signal_adjustment/dataset_id/execution_data_source/execution_dataset_id 全非空 |
| 新任务缺 ready 执行集硬失败 | ✅ | HTTP 400 "No ready …/none execution dataset";partial 数据集 400 拒绝 |
| L2 全走锁定 dataset | ✅ | fills.csv execution_price=数据集 open 精确一致,price_source=raw;估值同源。⚠️ 交易日历依赖 calendar.json/D:\通达信 sh000001.day(dataset 之外);legacy 信号路径(非 tdxquant/tushare)保留 TdxDayReader 仅作兼容 |
| 回测 Provider 调用=0 | ✅ | 5 个 Provider fetch_bars+zipfirst+TdxDayReader.read 调用即抛异常 + __init__ 计数,全程 **0** |
| 中断恢复(库级) | ✅ | 400 只真实演练:STORE 150/400 强杀 → 无新 manifest、无 .tmp 残留、旧 ready 字节级原样、resolve 仍指向旧 ready;重跑 6.6s 发布新 ready(395 ok/203,644 行),151 个已存 blob 内容寻址复用不重写,抽样无重复日期,blob SHA=内容。⚠️ 无法经正式 CLI 演练(P0-1) |
| building/partial/failed 不可回测 | ✅ | E2E 400 + 正式根 partial(91a4053f)被 resolve 正确跳过 |
| ready 原子发布 | ✅ | 缺 blob 发布 → ValueError + status=failed + 不可加载;tmp→replace 原子写;不覆盖旧 ready。⚠️ no_data 股票不阻断 ready(500 演练 1 只 no-data 仍 ready),容忍策略需明确 |
| 准全量性能 | ✅(带条件) | 开发 500 只演练属实(manifest/blob 实存,抽样核对一致):72s/1,395,102 行/峰值 568MB(tracemalloc);独立 400 只演练 fetch 3.3s+store 6.6s。全量推算:5796 只/约 16.2M 行/**约 14 分钟**/blob 0.31GB/Python 堆峰值≈6.6GB(RSS 预估 8–12GB)。⚠️ 单次全内存构建,中断即整体重跑 |
| 磁盘与内存 | ✅ | E 盘空闲 120GB(需求<1GB),RAM 63.1GB/空闲 28.7GB,满足;建议安全余量 20GB |

## 环境与流程警告(P1)

1. `serve()` 启动不打印数据根;`start_astock_serve.bat` 未设 `MARKET_DATA_ROOT` → 按标准方式启动即**静默连接内部测试仓**(页面仅橙色"测试目录"标签,无警告/硬失败)。sync 侧 14:42 后已加 WARNING。
2. `BacktestRequest`/API/experiments 默认 `execution_data_source="tdx_local"` —— 建议改为必填或默认 local_vendor。
3. **前端无 execution 数据源/数据集选择控件**;实验中心不展示 dataset_id/截止日期/股票数 → 用户无法从页面发起 local_vendor 执行回测(后端支持,UI 不通)。
4. market-data API 缺每 dataset 的 earliest/latest 日期范围(页面因此无日期范围列)。
5. `fetch_universe` 的 include_delisted/include_bse 形参被忽略;`is_ashare_code` 不识别深交所 302 新段(302132 中航成飞会被误判非 A 股)。
6. 上一轮声称的 7 个交付文件仍缺失(docs 4 + tmp 3)。
7. 规则创建/首次回测期间输出 `login success!/logout success!`,来源未定位(疑似指标编译链路连接 TDX 客户端),建议排查网络副作用。
8. psutil 在 requirements.txt 中但当前 Python 3.14 环境未安装。
9. 验收期间代码仍在被修改(14:42:14 / 14:42:46 两处),且 12 个生产文件改动未提交 —— 下轮验收前请**冻结并提交**。

## 结论

**不允许启动 5055×27 年正式全量导入。** 四项 P0 必须先解决:
1. 修复 `import fcntl`(win32 崩溃+锁失效)后,经正式 CLI 重演 dry-run→preflight→有限样本→中断演练;
2. 幸存者偏差需用户明确决策(接受/Tushare 补退市/供应商补数),并将决策写入任务定义;
3. 任务宇宙改为年度 ZIP 并集口径(5795 A+北交),明确退市与北交纳入规则;
4. 正式根补齐信号数据集后完成一次真实正式环境端到端回测。

其中 1、3、4 为工程可修项;**2 是数据本身的缺陷,不改数据源无法在系统内修复**。

## 交付物

- `docs/local_vendor_full_import_final_independent_acceptance.md`(本文件)
- `tmp/local_vendor_full_import_final_independent_acceptance.json`
- `tmp/local_vendor_full_import_final_acceptance_commands.txt`
- `tmp/local_vendor_full_import_preflight.json`
- `tmp/local_vendor_full_import_universe.csv`(5796 行并集,含类别/首末年份/2026 在市标记)
- `tmp/local_vendor_full_import_dataset_inventory.csv`(正式根+演练根全部 dataset)
- 副本:`E:\AStockData\reports\`(上一轮报告已备份为 `*_prev_20260726T0427.*`)
