# Gate A 验收:READY_FOR_VENDOR_FULL_IMPORT

**判定: `PASS`**

| 项目 | 值 |
|---|---|
| READY_FOR_VENDOR_FULL_IMPORT | **true** |
| READY_FOR_SURVIVORSHIP_SAFE_BACKTEST(Gate B) | false(本轮不评,退市股未补) |
| READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST(Gate C) | false(本轮不评,正式根尚无信号数据集) |
| 日期 | 2026-07-26 |
| 分支 / HEAD | `feat/multi-source-market-data` @ `cad0742746aa6f675b6fc4b17798ee43b482c2df`(工作区未提交;pre/post patch 已存档) |
| 默认 pytest | **631 passed / 1 skipped(live_tdxquant)/ 0 failed**,85.6s(整改后复跑确认) |
| JSON 证据 | [tmp/vendor_full_import_gate_a.json](../tmp/vendor_full_import_gate_a.json) |

## 22 条硬门槛逐项

| # | 门槛 | 结果 | 证据 |
|---|---|---|---|
| 1 | Windows CLI 不再因 fcntl 崩溃 | ✅ | sync_lock.py POSIX 分支内 import;CLI 真实运行多次;测试守卫禁止无条件 import fcntl |
| 2 | Windows 并发锁真实有效 | ✅ | 双进程实测:A 持锁存活时 B 立即 concurrent_lock、零写入 |
| 3 | stale lock 规则明确并通过测试 | ✅ | OS 级句柄锁随进程死亡自动释放;recovered_stale 上报;3 项跨进程测试 + drill |
| 4 | MARKET_DATA_ROOT 启动防呆 | ✅ | production+内部根/缺 env → SystemExit(2);serve 打印 env/根/ready 数/最新 dataset;bat 加载 .env;8 项 guard 测试 |
| 5 | sync 与 backtest 同一数据根 | ✅ | 双方 env 解析一致,均加载 .env |
| 6 | 宇宙由 2000–2026 并集动态生成 | ✅ | vendor_universe 元数据确认模式;CSV 5796 行实时产出 |
| 7 | 无 5055 硬编码 | ✅ | 生产代码 grep 5055/5795/5796 = 0 命中 |
| 8 | 非 A 股排除 | ✅ | 分类器排除 fund/index/bond/B股/未识别;本数据实测 excluded=0(数据本身纯净),测试覆盖各类别 |
| 9 | 302 等段通用规则 | ✅ | 30x 整段规则(无单票特例);302132/305999 测试通过;is_ashare_code 同步通用化 |
| 10 | 幸存者偏差写入 manifest | ✅ | drill 实产 manifest:survivorship_bias=true + 12 个政策字段 |
| 11 | 页面显示偏差警告 | ✅ | 数据仓库面板红/橙横幅(非小标签),固定文案;stub-DOM 23 断言;API 字段对正式根实测 |
| 12 | no_data/failed 策略明确 | ✅ | 严格策略:failed=0 且 no_data 全 allowlist 才 ready;9 项测试 |
| 13 | amount 单位文档统一(千元×1000) | ✅ | 模块头修正 + 守卫测试扫描 data 层无冲突表述 |
| 14 | CLI preflight 通过 | ✅ | 真实执行零写入,含锁/checkpoint/磁盘信息 |
| 15 | CLI dry-run 通过 | ✅ | 真实执行零写入,含宇宙规模与分块计划 |
| 16 | CLI 中断恢复通过 | ✅ | 正式 CLI:2/4 块强杀 → 拒绝无 --resume → --resume 跳过 2 块 → ready 发布(详见 cli_recovery_report) |
| 17 | ready 原子发布通过 | ✅ | 缺 blob → failed 不可回测;旧 ready 不可覆盖(byte 级验证) |
| 18 | building/partial/failed 不可回测 | ✅ | 既有测试 + 上轮产品路径 400 实证保持 |
| 19 | 准全量性能满足 | ✅ | 500 只/1.40M 行/18.7s/RSS 711MB;全量推算 ~7 分钟/0.33GB |
| 20 | 磁盘内存满足 | ✅ | E 空闲 120GB、RAM 63.1GB vs 需求 0.33GB/≈0.7GB |
| 21 | 默认 pytest 直接通过 | ✅ | 631/1/0 |
| 22 | 无新 P0 | ✅ | 本轮未发现;遗留项均列为非阻断后续任务 |

## 首轮踩坑与修正(如实记录)

1. 锁字节 0 与元数据同区 → Windows 强制锁阻塞探测读取 → 锁字节移至 1MB 偏移后全绿;
2. 首版演练以子进程 stdout 行为触发器 → 管道缓冲导致 kill 落空、演练失真 → v2 改为磁盘 checkpoint/锁文件轮询触发,全部指标真实落地。

## 已知非阻断遗留(移交后续 Gate)

- 供应商数据幸存者偏差是**数据缺陷**:本 Gate 仅完成"标记+警示+政策",补齐属 Gate B(设计已交付:tushare_delisted_composite_dataset_plan.md);
- 正式根无 TdxQuant/Tushare 信号数据集(Gate C);
- limit_rules/cross_section 的 300/301 硬编码待 30x 通用化(影响新段涨跌停标签);
- 前端 execution 数据集选择控件(Gate C);
- 工作区 20+ 文件未提交——**建议在启动正式全量导入前先提交冻结**。

## 正式全量导入操作序列(Gate A 通过后,由用户择时执行)

```bat
REM 0) 确认 .env: ASTOCK_ENV=production / MARKET_DATA_ROOT / LOCAL_VENDOR_RAW_ROOT
python scripts/sync_market_data.py --source local_vendor --mode full --preflight
python scripts/sync_market_data.py --source local_vendor --mode full --dry-run
python scripts/sync_market_data.py --source local_vendor --mode full --chunk-size 500 ^
    --universe-file "E:\AStockData\reports\vendor_full_import_universe.csv" ^
    --report-path "E:\AStockData\reports\vendor_full_import_run.json"
REM 中断后: 同命令 + --resume
```
