# D1/D4 — Dataset 绑定强校验与 HTTP 错误语义整改报告

- 日期:2026-07-26;缺陷:Gate C P0-D1(backtest.py:187-193 显式 dataset_id 不校验 source/adjustment 与 manifest 一致 → 错配 run 以 ok 落盘、lineage 污染)与 P2-D4(不存在 dataset → HTTP 500)

## 1. 统一校验器

新模块 `wtpy/apps/astock/data/dataset_binding.py`:

- `validate_signal_dataset_binding(repo, dataset_id, source, adjustment, period)`
- `validate_execution_dataset_binding(repo, dataset_id, source, period)`
- 校验项:manifest 可解析(损坏→`DATASET_MANIFEST_INVALID` 422,无堆栈泄漏)→ 存在(`DATASET_NOT_FOUND` 404)→ status=ready(`DATASET_NOT_READY` 400)→ dataset_type=bars(factor 集禁作行情,`DATASET_ROLE_MISMATCH`)→ period 一致 → source 完全一致 → adjustment 完全一致(`DATASET_BINDING_MISMATCH` 400)→ 角色(信号禁 raw/none;执行必须 adjustment=none)→ 派生集父 lineage 存在(`DATASET_LINEAGE_BROKEN` 422)→ blob 抽检(缺失→`DATASET_CORRUPT` 422);
- MARKET_DATA_ROOT 一致性:repo 恒由 cfg.market_data_root 构造,manifest 即在该根内解析成立;
- 错误模型(DatasetBindingError.to_payload):`code / message / dataset_id / requested_source / requested_adjustment / manifest_source / manifest_adjustment / remediation`(+扩展字段如 excluded 列表)。

## 2. 入口覆盖

统一汇聚点 `backtest.resolve_market_data_bindings(cfg, req, codes)`(backtest.py),**所有 run 创建路径必经**:

- 单次回测 API(sync):run_backtest 内部解析即校验,DatasetBindingError→结构化 4xx;
- 单次回测 API(async):**提交前预校验**(api.py),错配 400/404 且不创建 job/run;
- experiments(含模板/dual_source/signal_variants):创建实验时逐 variant 校验(`_resolve_variant_datasets_and_common_universe`),错配 400 且不建实验、不落 variant;
- 重跑/复制/resume/CLI/内部 service:全部经 BacktestService.run→run_backtest→同一校验(无旁路);
- 隐式解析(resolve_latest_ready)路径:结果天然一致,附加执行角色校验。

错配时:HTTP 4xx、**不创建 run、不写 SQLite、不写缓存、不 fallback、不污染 lineage**(探针实测新 run 数=0)。

## 3. D4 错误语义矩阵(实测)

| 场景 | HTTP | code |
|---|---|---|
| dataset 不存在 | **404**(修复前 500) | DATASET_NOT_FOUND |
| dataset 非 ready(partial/building) | 400 | DATASET_NOT_READY |
| source/adjustment/period 错配 | 400 | DATASET_BINDING_MISMATCH |
| 角色错配(factor 作行情/复权集作执行/raw 作信号) | 400 | DATASET_ROLE_MISMATCH |
| symbol 不覆盖 / allowlist 缺数 | 400 | SYMBOL_NOT_COVERED(逐股 reason) |
| execution dataset 缺失(无 ready) | 404 | DATASET_NOT_FOUND |
| manifest 损坏 / blob 缺失 / 父集缺失 | 422 | DATASET_MANIFEST_INVALID / DATASET_CORRUPT / DATASET_LINEAGE_BROKEN |

前端 `api()` 将结构化 detail 渲染为可读文案:`[code] message (dataset) 请求=a/b manifest=c/d — remediation`。

## 4. 探针证据(tmp/dataset_binding_error_probes.json,真实在线 API)

13/13 PASS:tdx源+internal集、internal源+tdx集、front+tsfqfq集、不存在集(404)、partial 信号集、partial 执行集、执行角色错配、执行源错配、信号股票不覆盖(BSE 43x)、白名单缺数股(600193)、执行股票不覆盖(服务层沙箱,生产执行集为两信号集超集故经隔离库验证)、单 variant 失败→实验 failed、废弃 (tushare,qfq) 对拒绝(404)。全程新建 run 数 **0**。

## 5. 测试

tests/apps/astock/test_gate_c_remediation.py::TestD1DatasetBinding(9 项:source/adjustment/period/status/404/factor 角色/执行角色/raw 角色/入口级)+ TestD4ErrorModel(payload 形状 + API 4xx 映射与零 run)。
