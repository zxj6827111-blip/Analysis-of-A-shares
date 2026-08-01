# 供应商日线正式全量导入 — 执行报告

**判定: `PASS` — FULL_VENDOR_IMPORT_COMPLETED=true**

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 15:55:01 ~ 15:58:01(+监控收尾,总 179.7s) |
| 分支 / HEAD | `feat/multi-source-market-data` @ `8ee03196`(冻结提交 "prepare local vendor full import") |
| 导入期间代码修改 | **0**(git diff 全程为空;无 .py 文件晚于开始快照) |
| sync_run_id | `localvendor_20260726T155504_a1ef5173`(全程唯一,无中断) |
| **dataset_id** | **`localvendor_none_1d_20260726_7089dc09c3c0`** |
| source/adjustment/period | local_vendor / none / 1d |
| 状态 | building → 严格策略校验 → **原子发布 ready** |

## 执行命令(实际)

```bat
set ASTOCK_ENV=production
set MARKET_DATA_ROOT=E:\AStockData\datasets\market_data
set LOCAL_VENDOR_RAW_ROOT=E:\AStockData\raw\local_vendor\original_files\incoming
python -u scripts/sync_market_data.py --source local_vendor --mode full ^
  --universe-file "E:\AStockData\reports\vendor_full_import_universe.csv" ^
  --chunk-size 500 ^
  --report-path "E:\AStockData\reports\local_vendor_full_import_run.json" ^
  --log-path "tmp/local_vendor_full_import_run.json"
```

前置:preflight rc=0(零写入)、dry-run rc=0(零写入、5796 只、无锁残留),均经正式 CLI。

## 宇宙(动态,零硬编码)

- 文件:`E:\AStockData\reports\vendor_full_import_universe.csv`,SHA256 `fef78967…c67c11cd`(与 tmp 副本逐字节一致,已写入开始基线)
- 5796 included / 0 excluded / **0 unknown** / 北交所 571 / 最新年缺席 249;universe_definition_version=v1
- universe_hash 已随 sync_log 落盘(`sync_logs/localvendor_20260726T155504_a1ef5173.json`)

## 导入结果

| 指标 | 值 |
|---|---|
| expected / imported | 5796 / **5796** |
| failed / no_data | **0 / 0**(严格策略:无需 allowlist) |
| coverage_ratio | 1.0 |
| 总K线 | **16,046,025**(Gate A 推算 16.24M,偏差 1.2%) |
| 日期范围 | 20000403 ~ 20260717 |
| 分块 | 12 × 500,逐块 checkpoint,0 块续传(一次通过) |
| 重复 ZIP | 27 年每年仅读 1 份(优先非"(1)"),33 个 SHA256 重复副本零读取 |
| blobs | 111 → 5898(+5787,内容寻址;数据集总量 331.9MB/目录 333.5MB) |
| 锁 | 完整持有者元数据(pid 57564/host Zhang/sync_run_id),released_at=15:58:00 |

## 资源实测(非估算)

| 指标 | 值 |
|---|---|
| CLI 耗时 | 176.5s(监控口径 179.7s)≈ **3 分钟** |
| 每百万行耗时 | 11.2s |
| **RSS 峰值** | **2246.2MB**(ctypes psapi 轮询实测;分块限界) |
| E 盘空闲 | 129.19 → 128.54 GB(净耗 0.65GB) |
| 临时目录峰值增量 | **0MB**(ZIP 内存流式,无解压落盘) |
| manifest 大小 | ≈1.5MB(5796 symbol 记录) |

## manifest 字段审计(第十一节)

必备字段全部在位:dataset_id/source/adjustment/period/status/created_at/cutoff/sync_run_id/universe_definition_version/expected~warning 六项计数/coverage_ratio/row_count/manifest_sha256/**universe_type=vendor_available_historical_union / survivorship_bias=true / historical_universe_complete=false / delisted_coverage_complete=false / known_missing_delisted_count=6+样本清单 / warning_text / recommended_use / prohibited_or_discouraged_use** / no_data_allowlist(空)/ 每 symbol blob_sha256+首末日期+行数。

规格愿望清单中以下字段不在 manifest 本体,由**既有配套记录补偿**(本轮禁改生产代码,如需并入 manifest 属下一轮增强):universe_file/universe_sha256(在开始基线与 sync_log:universe_hash)、单位乘数(Provider 文档+守卫测试+政策文档)、source_archive_hashes/重复包统计(盘点报告 sha `f65dd57e…` 已冻结)、dataset 级 earliest/latest(API 实时由 per-symbol 推导)。

## 中断处理

无中断发生。中断预案(Gate A 已实测):checkpoint 逐块落盘,`--resume` 沿用原 sync_run_id 跳过已完成块;本次一次通过后 checkpoint 按设计清理(sync_log 留存)。

## 交付物

见 `docs/local_vendor_full_import_post_acceptance.md` 与 tmp/ 下 8 项 JSON/CSV;关键副本已复制 `E:\AStockData\reports\`。
