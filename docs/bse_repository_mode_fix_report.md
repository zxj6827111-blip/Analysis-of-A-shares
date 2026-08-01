# D7 — 北交所 Repository 模式去 Baostock 门禁整改报告

- 日期:2026-07-26;缺陷:Gate C P1-D7(backtest.py:319 repo 模式仍逐股 build_factor_series(prefer_baostock=True) 驱动 L3 复权/公司行为门禁;Baostock 无北交所数据+缓存 0 覆盖 → BSE 不能正式/离线回测;共同池 5,528 只中正式引擎仅 4,962 可跑)+ 潜伏 D9(BSE 代码解析缺陷)

## 1. 方案:因子门禁改由锁定因子数据集驱动

- 新函数 `data/adjustments.py: build_factor_series_from_dataset(store, factor_manifest, code, dates)`:从不可变 **tushare/adj_factor FACTOR 数据集** blob(trade_date+adj_factor)构建 FactorSeries——asof 前向填充、最早因子播种前史、公司行为事件=因子变化日(连续等值去重),`source="dataset"/quality="complete"`;缺覆盖 → `dataset_missing/incomplete`(显式 fail-closed,绝非静默 identity);
- 因子集解析入绑定期(resolve_market_data_bindings):派生信号集(internal)锁其 factor 父集(lineage 断裂→422);tdxquant 解析最新 ready tushare/adj_factor 并**钉入 req.ca_factor_dataset_id**(run_meta/detail 可溯);
- `formal_adjustment_ready` 放行 `source="dataset"`;factor_manifest_sha 继续入 L1 缓存键(因子来源变化自动隔离缓存);
- **repo 模式零 Baostock**:build_factor_series(baostock 路径)仅存于 legacy 通道;在线计数器 baostock=0(上一轮在线尚有 1 次页面驱动外联,本轮 0),离线拦截器 0 次尝试,沙箱**未拷贝任何 baostock adjustments 缓存**仍全部通过。

## 2. BSE canonical 代码支持(43/83/87/920)

- `data/universe.py`:to_std_code 接受 `BSE.STK.*` 直通;裸 `92xxxx` → BSE(920 迁移段;SSE B 股为 900xxx,无重叠);is_bse_code 增 92 段;43/83/87 既有;
- `service/backtest_universe.py: select_universe` 放行 `BSE.` 前缀(修复 D9 之"BSE.STK.* 入参崩溃");
- `data/repository.py:_symbol_variants` 裸 92xxxx 归 BSE(修复 920 误映 SSE);BSE.STK ↔ .BJ ↔ bj 前缀互认;
- 映射规则源于交易所代码段元数据(段区间),非单只股票硬编码。

## 3. 覆盖与排除语义

- 因子集 `tushare_adjfactor_1d_20260726_acc8d3cadc79` 实测:ok 5,554(含 **BSE 920 段 329 只**),no_data 242(全为执行集同样没有的 43 段,不在共同池);**共同池 5,528 只覆盖缺口=0**;
- 共同池可正式回测:**4,962 → 5,528**(+566,含北交所 328);
- 实验资格计算纳入 CA 因子覆盖:缺覆盖逐股排除并记 `ca_factor_*` 原因——绝不整板块静默排除。

## 4. 北交所真实回测(≥20 只,产品路径)

- 在线 `exp_2a1d441663`(20 只 BSE.STK.92*,2012→eff 20260717,单实验双 variant):
  - tdxquant:bt_1785075518_d1f48f,**287 信号 / 89 成交**,succeeded;
  - internal:bt_1785075519_4d21e3,**107 信号 / 107 成交**,succeeded;
- 离线复跑 `exp_ae866a616f`:双 succeeded,metrics 逐字段与在线 0 差异;
- 另小规模实验(exp_3290cb2129)含 6 只 BSE 与两档正式池同路径通过;
- Baostock 调用:在线 0 / 离线 0。

## 5. 测试

TestD7DatasetFactorGate:数据集因子序列(完整性/事件日/asof 跳变 20151231→20160104 1.0→1.25)、BSE 全程 monkeypatch 禁 baostock 仍通过、缺覆盖显式 incomplete 并被 formal 门禁拒绝、43/83/87/920/BSE.STK.*/900 B 股映射矩阵、repository 变体映射。
