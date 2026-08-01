# Tushare 因子信号链路产品验收报告(Gate C 第一阶段)

**判定: `PASS` — READY_FOR_TUSHARE_FACTOR_SIGNAL=true**

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 保持 | READY_FOR_SURVIVORSHIP_SAFE_BACKTEST=**false**;READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST=**false**(TdxQuant/front 与退市 composite 未完成) |
| 证据 | [tmp/tushare_factor_signal_product_acceptance.json](../tmp/tushare_factor_signal_product_acceptance.json) |
| 默认 pytest | **670 passed / 1 skipped(live_tdxquant)/ 0 failed**,89.0s(631 旧 + 39 新全绿) |

## 1. 正式产品路径真实回测(正式根)

配置:signal=`internal/tushare_factor_qfq`(自动解析并锁定 `internal_tsfqfq_1d_20260717_c962acb8af26`)+ execution=`local_vendor/none`(显式锁定 `localvendor_none_1d_20260726_7089dc09c3c0`),经 API→BacktestRequest→Repository→引擎→SQLite→result detail 全链:

- HTTP 200,run 成功;任务创建即锁定两个 dataset_id,运行中不重解析;
- **结果详情(meta)完整血缘**:dataset_id=派生集、raw_dataset_id=全量 raw、factor_dataset_id=因子集、signal_formula_version=tsqfq_v1、execution_dataset_id=执行集;
- **SQLite** runs 行:signal_data_source/signal_adjustment/dataset_id/**signal_raw_dataset_id/signal_factor_dataset_id**(新列)/execution 双字段全部正确(fresh-DB schema 缺列 bug 由测试代理发现并已修复 SCHEMA_SQL);
- **L1/L2 分离铁证**(fills.csv):执行价=raw(如 600000 BUY@6.59=raw_price=execution_price),qfq 仅作参考列(5.8913);买卖/止盈止损/涨跌停/滑点/估值全走 raw;
- **Provider 调用=0**(TdxQuant/Tushare/TdxLocal/LocalVendor fetch+zipfirst+TdxDayReader 全插桩)。

## 2. 缓存与任务隔离(真实端到端,临时环境)

同策略/同股票/同日期,**只换 factor_dataset_id**(facA 1.25 vs facB 1.50 → 两个派生集):

- 两次运行 run_id 不同;SQLite 各自记录正确的 signal_factor_dataset_id;B 未复用 A 的任何信号/结果;
- 同一完整配置重跑结果逐字段一致(确定性复现);
- 信号/执行缓存键均含 raw_parent/factor_parent/formula_version/anchor_policy(+既有 9 字段),39 项单测覆盖逐字段隔离与向后兼容。

## 3. 离线回测(断网 + Token 不可用)

`socket.create_connection` 封锁 + `ts.get_token` 抛错 + 全 Provider 调用即抛:**回测 HTTP 200 成功,Provider 调用=0** —— 既有 dataset 完全自给。

## 4. 不回落 legacy

signal 配置指向不存在的 dataset 组合 → **HTTP 400**("No ready dataset … Run sync first"),不进入 legacy、不静默换源。legacy 旧链路仅当显式不选数据集信号源时可用,前端已标注"(旧链路 legacy)",结果页对空 signal_data_source 显示 `legacy_mode` 灰标;legacy 缓存键与数据集任务天然隔离(source/dataset 字段参与 hash)。

## 5. 前端(index_v3.html,桩测 32+ 断言)

- 三处信号源下拉新增"**本地行情+Tushare因子前复权**"(internal);无 ready 派生集时禁用并提示;选择后自动携带 signal_adjustment=tushare_factor_qfq 与 execution_data_source=local_vendor;
- Tushare 原生 QFQ 重标为"**校验模式**";
- **L2 执行只读区**(回测表单+实验中心):local_vendor/none、dataset_id、股票数、K 线数、日期范围、ready 状态、幸存者偏差橙标;无 ready 执行集时红色警告且不可创建数据集链路任务;
- 结果页数据链路卡片:信号源标签 + raw/factor 父集 ID;**绝不显示为"Tushare原生QFQ"**。

## 6. TdxQuant 下一阶段就绪检查(只检查,未执行)

`tmp/tdxquant_pre_sync_readiness.json`:CLI/外部根写入/front+none 配置/ready 原子发布/Provider 仅同步期调用——就绪;缺 (tdxquant,front,1d) 作用域锁与 checkpoint 断点(建议按本轮 factor 模式补齐,约半天);TDX 客户端离线时立即失败不重试,未做任何全量尝试。

## 遗留(非本轮阻断)

- 幸存者偏差(Gate B):派生集如实继承标记;
- 北交所 242 只缺席股无因子(已排除并记录);49 只前导缺口丢行(审计在案);
- API 层 BacktestBody 未暴露 use_signal_cache(实验中心内部路径已启用信号缓存;单发回测走全量重算,结果确定性一致)——建议后续补充。
