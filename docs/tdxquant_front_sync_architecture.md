# TdxQuant 前复权日线全量同步架构(Gate C 第二阶段)

| 项目 | 值 |
|---|---|
| 日期 | 2026-07-26 |
| 数据产品 | `source=tdxquant / adjustment=front / period=1d`,dataset_type=bars |
| 接口 | `D:\通达信\PYPlugins\user\tqcenter.py`(Version **1.0.3**,2026-02-28)→ TPythClient.dll → 通达信客户端 |
| 生产命令 | `python scripts/sync_market_data.py --source tdxquant --adjustment front --mode full --universe-file <冻结宇宙CSV> --batch-size 15 --end-date <cutoff> --allow-no-data-file <证据allowlist> ...` |

## 1. 数据通路与关键语义(全部真实在线实测确认)

- `tq.get_market_data(stock_list, period="1d", dividend_type="front"/"none", start_time, end_time, fill_data)`;所谓"批量"在 DLL 层为**逐只调用**(`GetHISDATsInStr`),批只影响宽表合并与 Python 开销。
- **前复权锚定 = 请求 end_time 内最后一根K线**。正式同步 end_time=cutoff(含最新交易日)→ 与通达信客户端当前显示逐值一致。manifest `anchor_policy=front_anchored_at_latest_bar_on_or_before_request_end`,`anchor_date` 记录实际锚定日。
- **仿射前复权模型**(a·p+b,现金分红为减常数;与 cad0742 轮结论一致):长历史高分红股早期前复权价**可为 ≤0**(客户端同样显示负价)。数据集**原样保存**,`price_precision_policy` 与页面均有提示;涨跌停/成交等全部走 L2 raw,不受影响。
- **fill_data 必须为 False**:tqcenter 默认会把停牌日按前值**全字段填充**(含成交量)→ 批量请求中会伪造停牌日K线。Provider 已强制 `fill_data=False`,停牌日自然缺行。
- **单位**:Volume=股(未复权,与 vendor 逐值一致);Amount=**万元** → Provider 统一 ×10000 存为元(`amount_policy=tdx_amount_wan_yuan_scaled_x10000_to_yuan`,与 vendor 元级一致,实测比值=1.0000)。
- **空响应分类**:单只请求返回空 dict = 干净的 no_data(退市/旧码);多只请求内任一只 `Date=None` 会令 tqcenter 宽表合并崩溃 → Provider 捕获后**整批回退单只**,逐只分类,失败绝不静默消失。
- **连接名必须进程唯一**(`astock_sync_<pid>_<ts>_<seq>.py`):被 kill 的进程不会 CloseConnect,**固定连接名会卡死在客户端导致后续 InitConnect 全部失败**(实测复现)——此修复是崩溃后 resume 可用的前提。僵尸连接名重启客户端即清理。

## 2. 宇宙与排除(通用规则,零硬编码)

候选 = 冻结 vendor 宇宙 CSV(动态历史并集,5796 行)。逐行按元数据分区:

| 规则 | 结果 |
|---|---|
| `present_in_latest_year=True` | eligible(5547) |
| BSE 旧号段(43/83/87)且最新年缺席 | excluded:`bse_legacy_code_migrated_to_920_segment`(242;920 新码为独立候选行,重复同步会双计)|
| 其余最新年缺席 | excluded:`absent_latest_vendor_year_delisted_no_provider_data`(7,含 920680)|

实测:TdxQuant **只服务在市证券**(7 只已退市探针全形态无数据);北交所 2025 年 920 号段迁移后旧码全部无数据,新码含精选层(2020-07-27)起完整历史。eligible 中同步期间发现的 no_data 一律按**未预期**阻断 ready,除非提供逐只证据 allowlist(本轮 19 只 2026 年退市股:Tushare stock_basic=D + vendor 末交易日证据)。

## 3. 同步引擎(sync_tdxquant_front_full)

```
锁 → checkpoint 校验 → Provider init/health → 逐批: fetch → (失败→单只重试×2) → 入库质量门 → store_bars → 每批原子写 checkpoint → 全部完成 → 严格评估 → manifest(building) → publish(ready/partial) → 删 checkpoint → 覆盖表/同步日志
```

- **锁**:`SyncTaskLock(root, tdxquant, front, 1d)`,Windows msvcrt 字节锁(1MB 偏移),进程死亡 OS 自动释放,stale 元数据可识别;第二任务立即失败、零写入(双进程实测)。
- **checkpoint**(`sync_logs/checkpoint_tdxquant_front_1d.json`,版本 `tdx_ck_v1`):sync_run_id、universe_hash、universe_sha256、market_data_root、batch_size、eligible_count、逐 symbol done(状态/blob/行数/日期)、stats(provider_calls/retries/batch_fallbacks)、started/updated。**每批原子落盘**。
- **resume**:必须 `--resume`;宇宙 hash、数据根、checkpoint 版本任一不符即拒绝;沿用原 sync_run_id;已完成 symbol 零重复调用(实测:中断+续传总调用数 = ceil(N/batch) 与不中断完全相同)。中断时不产生任何 manifest(无伪 ready)。
- **批量**:默认 10–20(CLI 上限 50,Provider 内部拆到 ≤20);批失败→单只×(1+2 重试,0.5s·n 退避);批间节流 `--batch-pause`(默认 0.05s)。batch=10/20/50 三档实测 content_hash 完全一致(结果与批大小无关)。
- **入库质量门**(front 语义):日期严格升序无重复、无 NaN、high≥low、open/close∈[low,high];front 不要求价格>0(仿射负价合法),none 要求>0。违规 → failed(quality_*),不入库。
- **严格 ready**(evaluate_strict_publish):failed=0 且 no_data 全部在显式 allowlist 内(计数/比例上限)才 ready,否则 partial;发布原子(building→ready),旧 dataset 永不覆盖。

## 4. manifest 要点

dataset_id=`tdxquant_front_1d_<cutoff>_<sha12>`;记录 universe_file/universe_sha256、expected/imported/excluded/no_data/failed、行数与日期范围、blob 内容寻址与 content_hash、provider_versions{tdxquant, tqcenter}、batch_size/retry_policy/checkpoint_version、anchor/price/volume/amount 四策略、provenance(fill_data=false、affine 说明、provider_calls、provider_called_only_during_sync=true、silent_fallback=false)、幸存者偏差全套标记(TDX 无退市数据 ⇒ survivorship_bias=true)。

## 5. 增量策略(tdx_front_inc_v1)

前复权锚随最新交易日移动:任何新公司行为都会改写**全历史**前复权序列 ⇒ 增量语义 = 重新全量拉取并发布**新 dataset**(旧集与旧回测绑定不受影响);检测口径与 sync_tdxquant_incremental 的 60 日重叠比对一致。不做原地追加。

## 6. 与 L1/L2 产品链路的关系

本数据集仅作 **L1 信号**(页面标签"通达信原生前复权");L2 执行固定 `local_vendor/none` raw。回测仅经 Repository 读取:Provider 与 TdxDayReader 在回测期调用数为 0(插桩验证),缺 ready dataset 时 API 返回 400,绝不回落 legacy。
