# Gate C 整改后最终独立复验报告

- 日期：2026-07-26（复验开始 22:51 +0800，跨 2026-07-27 凌晨完成）
- 分支：`feat/multi-source-market-data`，HEAD `8ee03196aa20a4ce10f8674ead55dd34b33867e9`
- 性质：只验收，不开发；全部结论以本轮实测为准（不采信开发报告自述）

## 最终判定

```
PASS
READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST = true
READY_FOR_SURVIVORSHIP_SAFE_BACKTEST       = false   （维持：退市股/composite 执行集未完成）
```

## 1. 验收快照（冻结有效）

| 项 | 开始 | 结束 | 一致 |
|---|---|---|---|
| HEAD | 8ee03196aa20 | 8ee03196aa20 | ✅ |
| patch SHA256 (git diff, 4323 行) | 614ee643a67f3378… | 614ee643a67f3378… | ✅ |
| 生产/测试文件指纹（230 个文件合并） | a5b0efd892af3c37… | a5b0efd892af3c37… | ✅ |
| 四个 dataset manifest 指纹 | 828c1850/c162366f/1536af84/cffd2e89 | 同左 | ✅ |

**SNAPSHOT_VALID = true**（`tmp/gate_c_final_revalidation_start_status.txt` / `..._end_status.txt`、`tmp/gate_c_revalidation/fingerprints_{start,end}.json`）。
中途另做一次 patch SHA 复核（22:27+0800 记录于 commands 日志），亦一致。

## 2. 默认测试

- `python -m pytest -q`（不带 --ignore=tmp）：**734 passed, 0 failed, 0 skipped, 4 warnings, 86.60s**
- `--collect-only`：**734 collected**，无未知 marker、无意外 skip、tmp 未被收集
- live_tdxquant 在客户端在线状态下真实运行（输出含 TQ 连接关闭日志）
- 证据：`tmp/gate_c_revalidation/pytest_default_output.txt`

## 3. 分项结论（细节见专项报告）

| 项 | 结论 | 证据 |
|---|---|---|
| D5 SQLite（fresh/v2 生产副本/partial/写失败/reconcile） | PASS（R1 P2 非阻断） | `tmp/gate_c_sqlite_revalidation.json` |
| D1 dataset 绑定 15+3 探针 | 18/18 PASS，零污染 | `tmp/gate_c_dataset_binding_probes.json` |
| D2 单实验双 variant（A档45只/B档500只） | PASS | `tmp/gate_c_single_experiment_dual_variants.json` |
| 共同池/共同截止（动态） | PASS（实测全池 5528；实验请求 45/500 → 排除 0；有效截止 20260717 动态计算） | 同上 + 实验 config.common_universe |
| D6 历史日历（2000–2026 六窗口） | PASS | `tmp/gate_c_historical_calendar_evidence.csv`、`d6_summary.json` |
| D7 北交所 Repository 模式 | PASS | `tmp/gate_c_bse_trade_evidence.csv`、`d7_summary.json` |
| D3 缺行情五态区分 | 5/5 PASS | `d3_summary.json` |
| D4 HTTP 错误语义 | PASS（404/400/422 结构化八字段，无 500/堆栈） | D1 探针 |
| §17 单 variant 失败语义 | PASS | `d17_summary.json` |
| §20 缓存隔离（键成分+行为） | PASS | `d17_summary.json` |
| §18 离线双 variant 复跑 | PASS（44 指标键×2 源逐字段 0 差异，计数器全 0） | `tmp/gate_c_offline_revalidation.json` |
| §19 页面 G1/G2/G3 | PASS（浏览器实测） | `g_pages_summary.json` |

在线/离线全程计数器：Provider(tdxquant/tushare/local_vendor)=0、tqcenter=0、TdxDayReader=0、Baostock=0、非回环 socket=0、原始 ZIP=0。

## 4. 27 条硬门槛逐条

1 默认 pytest 通过 ✅；2 快照有效 ✅；3 fresh DB ✅；4 v2 迁移 ✅（行数不减、旧值不变、幂等）；5 partial schema ✅；6 新 run 真实落库 ✅；7 history API 可见 ✅；8 reconcile 幂等 ✅（2 补/1 拒/二次 0 插入）；9 所有错配拒绝 ✅（18 探针）；10 单实验双 variant ✅（A/B 两档）；11 共同池动态 ✅（不硬编码，live 交集 5528）；12 共同截止动态 ✅（min() 实算 20260717）；13 2000–2015 日历正确 ✅（逐年成交分布）；14 2016 前信号不聚集 ✅（20160104 成交 = 0）；15 北交所 Repository ✅（20 只双 variant + 328 共同池）；16 Baostock=0 ✅；17 缺行情/零信号区分 ✅（五态）；18 不存在 dataset 404 ✅；19 在线双 variant ✅；20 离线双 variant ✅；21 Provider=0 ✅；22 TdxDayReader=0 ✅；23 网络=0 ✅；24 SQLite/API/页面 lineage 一致 ✅（0 不一致×4 run）；25 缓存无串用 ✅；26 单 variant 失败不标全成功 ✅；27 无新 P0 ✅。

## 5. 非阻断发现（须继续跟踪）

| ID | 级别 | 内容 |
|---|---|---|
| R1 | P2 | D5 迁移失败回滚覆盖版本戳（语义闸门），加列 DDL 因 sqlite3 自动提交不回滚；幂等列守卫使重试收敛，无可观测破坏态 |
| R2 | P2 | 损坏 manifest：显式 dataset_id 路径干净 422；list_datasets/resolve_latest_ready 路径抛未包装 JSONDecodeError |
| O1 | P3 | export.xlsx 为 variant 指标表（含 run_id 可回链 lineage），本体无 lineage 列；results API/detail/history 均含完整 lineage |
| O2 | P3 | research fingerprint 元数据（signal_fp）不含信号源/数据集；真实缓存键（signal_cache_key）包含全部成分，行为验证无跨源复用 |

已文档化语义（非缺陷）：`runs.status=unsupported_corporate_action` 为 D8 fast 引擎 CA fail_closed 顶层口径（variant succeeded、成交真实）；直连 /api/v1/backtests 恒重算信号（不暴露 use_signal_cache），实验路径使用缓存。

**更正记录**：上一会话中断前的 "LINEAGE_PARITY_OK: False" 为验收脚本缺陷（只读 repro.request，未走平铺 run_meta 回退），非产品缺陷；本轮以正确契约复测 4 run × 9 字段 = 0 不一致。

## 6. 维持的风险披露（不阻断 Gate C）

- local_vendor 存在幸存者偏差（页面 偏差 标签 + 横幅警示已确认显示）
- 历史退市股票未补齐（Gate B 未开始）
- TdxQuant 仿射前复权早期可为负（仅 L1；表单+详情页均有可见块级警告）
- 两套复权口径结果可能合理不同（B 档 n_trades 60373 vs 65645，可解释）

## 7. 是否允许进入 Gate B

允许。Gate C 多信号源产品链路已独立复验通过；Gate B（退市股 composite 执行集）为下一工程，不受本轮阻塞。

## 8. 交付物清单

见 `tmp/gate_c_final_independent_revalidation.json`（机器可读汇总）与本目录三份专项报告；关键报告已镜像至 `E:\AStockData\reports`。
