# TdxQuant 信号链路产品验收报告(Gate C 第二阶段)

**判定:`PASS` — READY_FOR_TDXQUANT_SIGNAL=true**

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 保持 | READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST=**false**(双信号源最终对照验收未做,本轮不判);READY_FOR_SURVIVORSHIP_SAFE_BACKTEST=**false** |
| 证据 | [tmp/tdxquant_signal_product_acceptance.json](../tmp/tdxquant_signal_product_acceptance.json) |
| 默认 pytest | **702 passed / 0 failed / 0 skipped**,96.8s(含新增 31 项;live_tdxquant 真实链路测试因客户端在线实跑通过) |

## 1. 正式产品路径真实回测(正式根)

配置:signal=`tdxquant/front`(显式锁定 `tdxquant_front_1d_20260726_09b179b48611`)+ execution=`local_vendor/none`(锁定 `localvendor_none_1d_20260726_7089dc09c3c0`),经 API→BacktestRequest→Repository→引擎→SQLite→result detail 全链:

- HTTP 200;任务创建即锁定两个 dataset_id,运行中不重解析;不带 dataset_id 的请求经 resolve_latest_ready 锁定同一正式集 ✅;
- **detail meta**:dataset_id=TDX 集、execution_dataset_id=执行集、signal_data_source=tdxquant、signal_adjustment=front;raw/factor 父集为空(非派生集,正确);
- **SQLite runs 行**:signal_data_source/signal_adjustment/dataset_id/execution 双字段全部正确;
- **L1/L2 分离铁证**(fills.csv):`600000 BUY@6.59 = raw_price = execution_price`,前复权参考列 5.44 仅供展示;买卖/止盈止损/涨跌停/滑点/估值全走 L2 raw;
- **Provider 调用=0,TdxDayReader 调用=0**(全插桩);回测不读 D:\通达信 本地 .day、不读 ZIP。

## 2. 缓存与任务隔离

- 同策略/同股票/同日期,tdxquant 信号 vs internal(Tushare 因子)信号:**run_id 不同,SQLite 各自血缘正确**(internal 行含 raw/factor 父集,tdxquant 行为空父集);
- 同一完整 tdxquant 配置重跑:**逐字段一致**(确定性);
- 缓存键隔离由 31 项新单测锁定:signal_data_source、signal_dataset_id(两套 tdxquant 集互异)、execution_dataset_id、weekly_bar_mode 逐字段参与 hash;legacy 键与数据集任务天然隔离;
- 备注:验收所用规则 XG:C>0 在两源产生相同信号事件集,故 signal_fp/指标相同——这是**内容指纹**而非缓存串用(run 独立计算,SQLite 双行为证)。

## 3. 离线回测(客户端隔离)

全 Provider+tqcenter 导入+TdxDayReader 插桩即抛、非回环 socket 全封:**HTTP 200,Provider=0,TdxDayReader=0,指标与在线一致**(详见 [离线报告](tdxquant_front_offline_backtest_report.md))。

## 4. 不回落 legacy

signal 指向不存在的组合(tdxquant/front_missingvariant)→ **HTTP 400**,不进 legacy、不静默换源;legacy 链路仅显式不选数据集信号源时可用,页面保留"(旧链路 legacy)"标注与 legacy_mode 灰标。

## 5. 前端(index_v3.html,桩检 10/10)

- 三处信号源下拉:**"通达信原生前复权"**(tdxquant)——无 ready tdxquant/front 集时禁用(提示"需先同步通达信前复权数据集"),启用由 `/api/v1/market-data/status` 的 ready 集驱动;
- 选中后 payload 注入 signal_adjustment=front + execution_data_source=local_vendor(L2 只读区同 internal);
- 结果页数据链路卡片:tdxquant→标签"通达信原生前复权"+锁定 dataset_id,**绝不显示为 Tushare 或 internal**;悬浮提示仿射负价语义;
- 实验中心 payload 同步注入;legacy 标注保持不变。

## 6. 双源最终对照(下一轮 Gate C 收官的输入)

两套信号源(tdxquant/front 与 internal/tushare_factor_qfq)已同时 ready 且互相隔离;口径差异已量化(见[对照报告](tdxquant_vs_tushare_factor_comparison.md))。本轮按规范仅完成最小产品对照(同配置双源各自成功运行、互不串扰),**不宣布 READY_FOR_MULTI_SOURCE_PRODUCTION_BACKTEST**。
