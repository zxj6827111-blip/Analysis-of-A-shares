# TdxQuant 信号离线回测验证报告

**判定:PASS — 关闭/隔离客户端后回测成功,Provider=0 且 TdxDayReader=0**

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 信号 L1 | tdxquant/front `tdxquant_front_1d_20260726_09b179b48611`(显式锁定) |
| 执行 L2 | local_vendor/none `localvendor_none_1d_20260726_7089dc09c3c0` |
| 证据 | [tmp/tdxquant_front_offline_backtest.json](../tmp/tdxquant_front_offline_backtest.json)(源自产品验收 PART3) |

## 隔离方式(等效证明)

按规范第二十部分的替代条款执行(不强制关闭用户的通达信程序以避免风险):

1. **全 Provider 插桩即抛**:TdxQuantProvider.fetch_bars/_ensure_initialized/health_check、`_load_tqcenter`(tqcenter 导入路径)、Tushare 全方法、TdxLocal、LocalVendor(含 zipfirst)——任何调用立即 AssertionError 并计数;
2. **TdxDayReader.read 插桩即抛**(D:\通达信 本地 .day 读取零调用验证);
3. **socket 非回环全封**(仅放行 TestClient 进程内事件循环所需的 127.0.0.1 管道;外部网络不可达);
4. 回测环境 tdx_root 指向不存在目录。

## 结果

| 指标 | 值 |
|---|---|
| HTTP | **200**,run 成功(run_id bt_1785065807_7ac522) |
| **Provider 调用** | **0** |
| **TdxDayReader 调用** | **0** |
| L1 来源 | Repository(tdxquant ready dataset,任务创建即锁定,不重解析) |
| L2 来源 | Repository(local_vendor ready dataset) |
| 指标一致性 | 与在线同配置运行**逐字段一致**(same_metrics_as_online=true) |

结论:已发布 dataset 完全自给,回测链路不依赖通达信客户端在线、不读原始 .day、不触网。
