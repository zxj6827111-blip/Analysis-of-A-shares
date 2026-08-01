# 本地行情数据盘点报告（只读）

- 生成时间（UTC）: `2026-07-26T00:43:26.060063+00:00`
- 数据根目录: `E:\Software Development\wtpy-master\storage\astock\market_data`
- 分支上下文: `feat/multi-source-market-data`
- 范围: 仅检查 `manifests/` / `blobs/` / `sync_logs/`，不修改任何数据，不调用外部接口

## 总表

| source | adjustment | ready datasets | symbols | bars | earliest | latest | size |
| ------ | ---------: | -------------: | ------: | ---: | -------- | ------ | ---: |
| tdxlocal | none | 1 | 3 | 7413 | 20100104 | 20240701 | 143.9 KB |
| tushare | none | 1 | 3 | 7413 | 20100104 | 20240701 | 156.9 KB |
| tushare | qfq | 1 | 3 | 7413 | 20100104 | 20240701 | 152.4 KB |
| tdxquant | front | 0 | 0 | 0 | - | - | 0 B |
| internal | asof_qfq | 0 | 0 | 0 | - | - | 0 B |

## 一、目录与存储

- **真实数据根**: `E:\Software Development\wtpy-master\storage\astock\market_data`（与 DatasetStore 约定 `storage/astock/market_data` 一致）
- manifests: `5` 个
- blobs 唯一数量: `9`
- blobs 总大小: `453.1 KB` (463971 bytes)
- manifests 大小: `6.2 KB`
- sync_logs 大小: `0 B`
- 树合计: `459.3 KB`

## 二、全部数据集

### `partial_accept_test`

| 字段 | 值 |
| --- | --- |
| source | tushare |
| adjustment | qfq |
| period | 1d |
| weekly_bar_mode | local_aggregate |
| status | partial |
| cutoff | None |
| created_at |  |
| sync_run_id |  |
| symbol_count | 1 |
| total_bar_count | 0 |
| actual_bar_count | None |
| earliest_date | None |
| latest_date | None |
| actual_earliest | None |
| actual_latest | None |
| manifest_path | E:\Software Development\wtpy-master\storage\astock\market_data\manifests\partial_accept_test.json |
| referenced_blob_count | 0 |
| exclusive_blob_size | 0 |
| referenced_blob_total_size | 0 |
| has_missing_blob | False |
| has_corrupt_npz | False |
| is_ready | False |
| is_partial | True |
| is_failed | False |

### `tdxlocal_none_1d_20240701_eb0ebeb7b638`

| 字段 | 值 |
| --- | --- |
| source | tdx_local |
| adjustment | none |
| period | 1d |
| weekly_bar_mode | local_aggregate |
| status | ready |
| cutoff | 20240701 |
| created_at | 2026-07-26T08:06:24 |
| sync_run_id | tdxlocal_20260726T080624_dd46d2fc |
| symbol_count | 3 |
| total_bar_count | 7413 |
| actual_bar_count | 7413 |
| earliest_date | 20100104 |
| latest_date | 20240701 |
| actual_earliest | 20100104 |
| actual_latest | 20240701 |
| manifest_path | E:\Software Development\wtpy-master\storage\astock\market_data\manifests\tdxlocal_none_1d_20240701_eb0ebeb7b638.json |
| referenced_blob_count | 3 |
| exclusive_blob_size | 12407 |
| referenced_blob_total_size | 147342 |
| has_missing_blob | False |
| has_corrupt_npz | False |
| is_ready | True |
| is_partial | False |
| is_failed | False |

### `tdxlocal_none_1d_20240701_fddc47032c1c`

| 字段 | 值 |
| --- | --- |
| source | tdx_local |
| adjustment | none |
| period | 1d |
| weekly_bar_mode | local_aggregate |
| status | partial |
| cutoff | 20240701 |
| created_at | 2026-07-26T08:06:00 |
| sync_run_id | tdxlocal_20260726T080600_a95fb51e |
| symbol_count | 3 |
| total_bar_count | 6901 |
| actual_bar_count | None |
| earliest_date | 20100104 |
| latest_date | 20240701 |
| actual_earliest | None |
| actual_latest | None |
| manifest_path | E:\Software Development\wtpy-master\storage\astock\market_data\manifests\tdxlocal_none_1d_20240701_fddc47032c1c.json |
| referenced_blob_count | 2 |
| exclusive_blob_size | 0 |
| referenced_blob_total_size | 134935 |
| has_missing_blob | False |
| has_corrupt_npz | False |
| is_ready | False |
| is_partial | True |
| is_failed | False |

### `tushare_none_1d_anchor20240701_5abaecde9133`

| 字段 | 值 |
| --- | --- |
| source | tushare |
| adjustment | none |
| period | 1d |
| weekly_bar_mode | local_aggregate |
| status | ready |
| cutoff | 20240701 |
| created_at | 2026-07-26T08:04:49 |
| sync_run_id | tushare_20260726T080449_9954cc2c |
| symbol_count | 3 |
| total_bar_count | 7413 |
| actual_bar_count | 7413 |
| earliest_date | 20100104 |
| latest_date | 20240701 |
| actual_earliest | 20100104 |
| actual_latest | 20240701 |
| manifest_path | E:\Software Development\wtpy-master\storage\astock\market_data\manifests\tushare_none_1d_anchor20240701_5abaecde9133.json |
| referenced_blob_count | 3 |
| exclusive_blob_size | 160620 |
| referenced_blob_total_size | 160620 |
| has_missing_blob | False |
| has_corrupt_npz | False |
| is_ready | True |
| is_partial | False |
| is_failed | False |

### `tushare_qfq_1d_anchor20240701_8d6188e93726`

| 字段 | 值 |
| --- | --- |
| source | tushare |
| adjustment | qfq |
| period | 1d |
| weekly_bar_mode | local_aggregate |
| status | ready |
| cutoff | 20240701 |
| created_at | 2026-07-26T08:04:50 |
| sync_run_id | tushare_20260726T080449_9954cc2c |
| symbol_count | 3 |
| total_bar_count | 7413 |
| actual_bar_count | 7413 |
| earliest_date | 20100104 |
| latest_date | 20240701 |
| actual_earliest | 20100104 |
| actual_latest | 20240701 |
| manifest_path | E:\Software Development\wtpy-master\storage\astock\market_data\manifests\tushare_qfq_1d_anchor20240701_8d6188e93726.json |
| referenced_blob_count | 3 |
| exclusive_blob_size | 156009 |
| referenced_blob_total_size | 156009 |
| has_missing_blob | False |
| has_corrupt_npz | False |
| is_ready | True |
| is_partial | False |
| is_failed | False |

## 三、按来源汇总

### 1. tdxquant/front

**本地尚未同步 TdxQuant 前复权数据**（不得将 tdxlocal/none 计为 TdxQuant）。

### 2. tushare/qfq

- ready: **1** / total: 2
- partial: 1, failed: 0
- ready ids: `['tushare_qfq_1d_anchor20240701_8d6188e93726']`
- symbols (sum of ready manifest counts): 3
- bars (actual via Repository where ready): 7413
- date range: 20100104 → 20240701
- size (unique blobs of ready): 152.4 KB

### 3. internal/asof_qfq

本地无此类别数据集。

### 4. tdxlocal/none

- ready: **1** / total: 2
- partial: 1, failed: 0
- ready ids: `['tdxlocal_none_1d_20240701_eb0ebeb7b638']`
- symbols (sum of ready manifest counts): 3
- bars (actual via Repository where ready): 7413
- date range: 20100104 → 20240701
- size (unique blobs of ready): 143.9 KB

### 5. tushare/none

- ready: **1** / total: 1
- partial: 0, failed: 0
- ready ids: `['tushare_none_1d_anchor20240701_5abaecde9133']`
- symbols (sum of ready manifest counts): 3
- bars (actual via Repository where ready): 7413
- date range: 20100104 → 20240701
- size (unique blobs of ready): 156.9 KB

### 6. 其他 / legacy

无额外独立 legacy 类别。`partial_accept_test` 计入 tushare/qfq 的 total/partial。

## 四、数据源重点

### Tushare qfq

- ready dataset 数量: **1**
- 最新 ready dataset_id: `tushare_qfq_1d_anchor20240701_8d6188e93726`
- 股票数: **3**
- 日K线总数（Repository 实测）: **7413**
- 最早日期: **20100104**
- 最晚日期: **20240701**
- 磁盘占用（ready 唯一 blob）: **152.4 KB**
- 覆盖主板: True; 创业板: True; 科创板: False; 北交所: False; 退市: False
- partial/failed 同类: partial=1, failed=0

### TdxQuant front

**本地尚未同步 TdxQuant 前复权数据**。本地 tdxlocal/none 不是 TdxQuant。

### tdxlocal/none

- ready dataset 数量: **1**
- 最新 dataset_id: `tdxlocal_none_1d_20240701_eb0ebeb7b638`
- 股票数: 3
- K线总数（实测）: 7413
- 起止: 20100104 → 20240701
- symbol 格式与 SSE.STK/SZSE.STK 兼容测试:
  - dataset `tdxlocal_none_1d_20240701_eb0ebeb7b638` stored: `['sh601088', 'sz000001', 'sz301107']`
    - query `SZSE.STK.000001`: ok=True bars=3449 err=
    - query `000001.SZ`: ok=True bars=3449 err=
    - query `sz000001`: ok=True bars=3449 err=
    - query `SSE.STK.601088`: ok=True bars=3452 err=
    - query `sh601088`: ok=True bars=3452 err=
    - query `SSE.STK.301107`: ok=False bars=- err=Symbol SSE.STK.301107 not in dataset tdxlocal_none_1d_20240701_eb0ebeb7b638
    - query `sz301107`: ok=True bars=512 err=

## 五、去重与完整性

- manifest 引用 blob 总次数: **11**
- 实际唯一 blob 数量（磁盘）: **9**
- 被引用唯一 blob: **9**
- 重复引用节省空间: **131.8 KB** (134935 bytes)
- 孤儿 blob 数量: **0**（本次不删除，仅记录）
- manifest 引用但不存在的 blob: **0**
- 损坏 NPZ: **0**

## 六、股票级与特别检查

- ready 数据集 symbol 行数（CSV）: 9
- Repository 读取失败: 0

### 特别股票

- dataset=`tdxlocal_none_1d_20240701_eb0ebeb7b638` query=`000001.SZ` found=True bars=3449 sse_szse_ok=True err=
- dataset=`tdxlocal_none_1d_20240701_eb0ebeb7b638` query=`601088.SH` found=True bars=3452 sse_szse_ok=True err=
- dataset=`tdxlocal_none_1d_20240701_eb0ebeb7b638` query=`301107.SZ` found=True bars=512 sse_szse_ok=True err=
- dataset=`tushare_none_1d_anchor20240701_5abaecde9133` query=`000001.SZ` found=True bars=3449 sse_szse_ok=True err=
- dataset=`tushare_none_1d_anchor20240701_5abaecde9133` query=`601088.SH` found=True bars=3452 sse_szse_ok=True err=
- dataset=`tushare_none_1d_anchor20240701_5abaecde9133` query=`301107.SZ` found=True bars=512 sse_szse_ok=True err=
- dataset=`tushare_qfq_1d_anchor20240701_8d6188e93726` query=`000001.SZ` found=True bars=3449 sse_szse_ok=True err=
- dataset=`tushare_qfq_1d_anchor20240701_8d6188e93726` query=`601088.SH` found=True bars=3452 sse_szse_ok=True err=
- dataset=`tushare_qfq_1d_anchor20240701_8d6188e93726` query=`301107.SZ` found=True bars=512 sse_szse_ok=True err=

- ready 中北交所 symbol 行数: 0
- **当前 ready 数据集中未发现北交所股票**
- **当前样本中未发现明确标记的退市股票**

### 质量告警 symbol 数: 6

- tushare_none_1d_anchor20240701_5abaecde9133 SZSE.STK.301107: dup=False unsorted=True nan=False nonpos=False h<l=False oc_out=False readable=True 
- tushare_none_1d_anchor20240701_5abaecde9133 SSE.STK.601088: dup=False unsorted=True nan=False nonpos=False h<l=False oc_out=False readable=True 
- tushare_none_1d_anchor20240701_5abaecde9133 SZSE.STK.000001: dup=False unsorted=True nan=False nonpos=False h<l=False oc_out=False readable=True 
- tushare_qfq_1d_anchor20240701_8d6188e93726 SZSE.STK.301107: dup=False unsorted=True nan=False nonpos=False h<l=False oc_out=False readable=True 
- tushare_qfq_1d_anchor20240701_8d6188e93726 SSE.STK.601088: dup=False unsorted=True nan=False nonpos=False h<l=False oc_out=False readable=True 
- tushare_qfq_1d_anchor20240701_8d6188e93726 SZSE.STK.000001: dup=False unsorted=True nan=False nonpos=False h<l=False oc_out=False readable=True 

## 七、交付物

| 文件 | 路径 |
| --- | --- |
| Markdown | `docs/local_market_data_inventory.md` |
| JSON | `tmp/local_market_data_inventory.json` |
| Dataset CSV | `tmp/local_market_data_dataset_summary.csv` |
| Symbol CSV | `tmp/local_market_data_symbol_summary.csv` |
| Commands | `tmp/local_market_data_inventory_commands.txt` |

## 八、结论摘要

1. Tushare qfq ready: **1**
2. TdxQuant front ready: **0** — 本地尚未同步则计 0
3. tdxlocal raw ready: **1**
4. 总磁盘占用: **459.3 KB**
5. partial=2, failed=0, corrupt=0, missing_blob_refs=0
