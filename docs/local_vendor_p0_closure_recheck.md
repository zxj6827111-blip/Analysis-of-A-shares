# P0 关闭情况独立复核

**判定: `CONDITIONAL_PASS`**

- READY_FOR_FULL_DAILY_IMPORT: **false**
- 生成时间: `2026-07-26T05:18:48.609398+00:00`

## 声称已关闭的 P0

| P0 | 声称 | 独立复核 |
|----|------|----------|
| 1 sync 有 local_vendor | 关闭 | PASS |
| 2 sync 读 env 路径 | 关闭 | PASS |
| 3 静默内部仓 | 关闭 | PARTIAL |
| 4 数据管理 API | 关闭 | PASS |
| 5 tdx_local 静默 fallback | 关闭 | PASS(嵌套fallback已删)；默认 source 仍 tdx_local |
| 8 并发锁 | 关闭 | PASS |

## 仍未关闭（阻止 READY）

- **zip_first_performance**: 仍开放
- **frontend_page**: 仍开放
- **historical_universe**: 已关闭
- **delisted_rules**: 仍开放
- **dry_run_preflight**: 仍开放
- **silent_default_without_env**: 仍开放
- **default_exec_source_tdx_local**: 仍开放

## 关键证据

- with env sync==backtest formal: `{'cfg': 'E:\\AStockData\\datasets\\market_data', 'sync': 'E:\\AStockData\\datasets\\market_data', 'is_external': True, 'sync_eq_backtest': True, 'is_formal': True}`
- API status: `{'code': 200, 'body_keys': ['data_root', 'is_test_root', 'exists', 'manifest_count', 'blob_count', 'total_size_bytes', 'ready_dataset_count', 'partial_dataset_count', 'failed_dataset_count', 'total_bar_count', 'total_symbol_count', 'datasets'], 'sample': {'data_root': 'E:\\AStockData\\datasets\\market_data', 'is_test_root': False, 'ready_dataset_count': 2, 'partial_dataset_count': 2, 'failed_dataset_count': 0, 'blob_count': 111, 'total_size_bytes': 9140763, 'manifest_count': 4}}`
- universe fetch_n=5547 latest_n=5547
- cache isolation: True
- zip_first_done: False
- frontend: False
- dry_run: False

## 结论

用户列出的 1/2/4/5(嵌套)/8 基本属实；ZIP-first、前端、历史并集、退市、dry-run **未完成**。 **READY 仍为 false**。性能瓶颈根因（每 symbol 开 27 个 ZIP）**代码层面仍存在**。

### 宇宙校正
- fetch_universe=5547 = 2026 最新年截面，**不是** 2000-2026 并集(5796)。历史并集仍未实现。

