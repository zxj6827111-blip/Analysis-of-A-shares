# Gate C 复验专项：D2 单实验双 variant + 共同池/截止 + 离线一致（独立复验)

- 证据：`tmp/gate_c_single_experiment_dual_variants.json`、`tmp/gate_c_offline_revalidation.json`、`tmp/gate_c_dataset_binding_probes.json`、`tmp/gate_c_revalidation/d17_summary.json`

## 1. 两档正式实验（真实产品入口 POST /api/v1/experiments → /start）

| 档 | experiment | 池 | 窗口 | variant | run_id | n_signals | n_trades |
|---|---|---|---|---|---|---|---|
| A(45只=20BSE+5负价+主/创/科) | exp_2faa813390 | 45→45（排除0） | 20120102–20260717 | tdxquant/front | bt_1785078236_93366e | 4277 | 4049 |
| | | | | internal/tsfqfq | bt_1785078242_2b7040 | 3757 | 3729 |
| B(500只 分层含20BSE) | exp_eb0e7c53c9 | 500→500（排除0） | 20120102–20260717 | tdxquant/front | bt_1785078371_8dd136 | 61135 | 60373 |
| | | | | internal/tsfqfq | bt_1785078462_eed1cf | 66263 | 65645 |

- 同一 experiment_id、两个 variant_id、两个互异 run_id ✅
- 锁定参数（codes/start/end/hold/entry_lag/买卖点/止损止盈/费用/execution 全套）两 variant 逐键一致，仅信号三键不同 ✅
- execution dataset 完全相同（localvendor_none_1d_20260726_7089dc09c3c0）✅
- L1/L2 分离：signal_price_mode=asof_forward_qfq / execution_price_mode=raw ✅；成交价全正、price_source=raw ✅
- 实时落库：runs/metrics/experiment_variants 均在生产 SQLite ✅；history 首屏可见 ✅

## 2. lineage 三方一致

detail API 顶层 lineage、SQLite runs 行、history API 行，9 字段 × 4 run 全比对：**0 不一致**。
（上一会话报告的 "LINEAGE_PARITY_OK: False" 系旧脚本只读 repro.request 所致，产品的平铺 run_meta 回退契约工作正常。）

results API 每 variant 带 signal_data_source/signal_adjustment/signal_dataset_id/execution_dataset_id ✅；export.xlsx 含双 run_id/variant_id + 指标（O1：本体无 lineage 列，P3）。

## 3. 共同池与共同截止（动态）

- live 四集交集（tdx∩tsfqfq∩exec∩factor）= **5528**，实测计算，非硬编码
- 实验 config.common_universe：requested/common、逐源排除数、requested_end=20260717、dataset_common_cutoff=20260717、effective_end=20260717（min() 动态）
- TDX 侧 20260718–20260724 尾段未参与（有效截止 20260717）✅

## 4. 单 variant 失败语义（§17)

- 生产入口 fail-fast：signal_variants 携带错配 dataset → 400 DATASET_BINDING_MISMATCH，experiments/variants/runs 计数零变化（不产生错误 lineage、不 fallback）✅
- 运行期失败（隔离环境、真实 ExperimentRunner 终态聚合 + 真实 db 层）：失败 variant=failed+error、兄弟 variant=succeeded 真实、实验 status=failed、failed_variants=1 ✅
- 修正后重跑 → 新实验 completed；原实验/variant 行快照逐字节不变（不污染）✅
- 页面：运行监控列表中 probe10 失败实验显示 “失败” 状态 ✅

## 5. 缓存隔离（§20)

- signal_cache_key 实含 16 成分（源/复权/数据集/raw 父/factor 父/公式版/锚策略/周线模式/日历版本/执行源/执行集/池 hash/起止/指标/factor_manifest_sha）✅
- 行为（产品路径）：exp_87ae188854（仅改 take_profit → param_hash 新、信号键不变）两 variant `signal_cache_hit=true`，n_signals 与冷跑一致（4277/3757）✅
- 键分离构造证明：换源/换信号集/换 factor 父/换执行集/换日历 hash/legacy 六种变体键全部不同 ✅
- O2（P3）：run_meta 的 research fingerprint 元数据不含源信息（展示层）；真实键包含。

## 6. 离线复跑（§18)

- 离线沙箱（Provider/tqcenter/TdxDayReader/Tushare/Baostock/非回环 socket/ZIP 全部拦截 + Token 清除）8767 端口重跑 A 档同参数单实验双 variant（exp_135100ead3）
- 44 metrics 键 × 2 源逐字段与在线**完全一致**；n_signals 4277/3757 一致；lineage、日历 sha 一致
- 计数器全 0；沙箱 SQLite v3 落库 2 runs/2 metrics/2 variants；history 可见；无 Token、无通达信客户端依赖 ✅
