# Gate A 工程整改报告(供应商日线全量导入前)

| 项目 | 说明 |
|---|---|
| 状态 | 完成 |
| 日期 | 2026-07-26 |
| 分支 | `feat/multi-source-market-data` @ `cad0742746aa6f675b6fc4b17798ee43b482c2df`(工作区未提交,基线见 patch) |
| 关联 Gate | Gate A(READY_FOR_VENDOR_FULL_IMPORT) |
| 基线快照 | `tmp/vendor_full_import_gate_a_pre_fix_status.txt` / `pre_fix.patch`;整改后 `post_fix_status.txt` / `post_fix.patch` |

## 0. 基线与并发进程

- 开始时间 15:18,冻结 pre-fix patch(sha256 前 24 位 `4790064f5cf698e535d15183`)。
- 检测到 `grok.exe`(PID 48172)驻留但全程未修改仓库:开始/结束两次全量 `.py` mtime 快照比对,变更文件**恰好等于**本轮自有修改集(8 个文件),无第三方干扰。

## 1. Windows 跨平台任务锁(P0-1 关闭)

新增 [wtpy/apps/astock/data/sync_lock.py](../wtpy/apps/astock/data/sync_lock.py):

- **导入安全**:`fcntl` 仅在 POSIX 分支内 import;模块在 win32 可无条件导入(测试守卫:`test_sync_lock.py::TestImportSafety` 同时扫描 CLI 脚本禁止无条件 `import fcntl`)。
- **锁原语**:持有者全程保持文件句柄上的 OS 级字节锁(win32 `msvcrt.locking`,POSIX `fcntl.flock`)。锁字节位于 1MB 偏移,避开元数据区(Windows 字节锁是强制锁,锁字节 0 会阻塞元数据读取——首版实测踩坑后修正)。
- **作用域规则(明确契约)**:一个锁 = 一个 `(market_data_root, source, adjustment, period)` 元组。相同元组绝不并发;不同元组(不同 source 或不同数据根)各自锁文件、允许并发。
- **stale 策略**:进程死亡 → OS 自动释放字节锁 → 下一个任务可直接获得;残留元数据被识别并以 `recovered_stale`(含 pid/alive 判定)上报。pid 存活探测在 Windows 用 ctypes `OpenProcess`+`GetExitCodeProcess`(明确规避 `os.kill(pid,0)` 在 Windows 会**终止**目标进程的陷阱)。
- **锁文件元数据**:pid、hostname、start_time、source、adjustment、period、market_data_root、sync_run_id;释放时补记 `released_at`。

### Windows 真实测试(正式 CLI,双进程)

`tmp/vendor_full_import_cli_recovery.json` 第 8 节:

- A 任务确认持锁且存活时启动同参数 B 任务 → B **立即失败** `concurrent_lock`,错误信息含持有者 pid/host/start/sync_run_id/alive=True;
- B **未写任何 manifest/blob**;
- A 结束后锁释放(`released_at`);
- 强杀持有者后新任务可获锁并报告 stale 恢复(`test_sync_lock.py::TestCrossProcess` 3 项 + drill 实测)。

## 2. MARKET_DATA_ROOT 启动防呆(P0 关闭)

- [config.py](../wtpy/apps/astock/config.py):新增 `astock_env`(`ASTOCK_ENV=production|development|test`,默认 development)、`load_env_file()`(项目根 `.env`,已有环境变量优先)、`market_data_root_guard()`(production 下 env 缺失或指向项目内部 storage → `blocked`,仅 `ASTOCK_ALLOW_INTERNAL_DATA_ROOT=1` 可显式放行)。
- [api.py](../wtpy/apps/astock/api.py) `serve()`:启动时打印 **ASTOCK_ENV / MARKET_DATA_ROOT(标注 internal|external)/ ready dataset 数 / 最新 local_vendor ready dataset**;guard.blocked → 打印阻断原因并 `SystemExit(2)`(测试:`test_cli_dry_run_preflight.py::TestServeGuard`)。
- [start_astock_serve.bat](../start_astock_serve.bat):启动前解析 `.env` 注入环境(无 `.env` 给出显式警告),默认 `ASTOCK_ENV=production`,并回显两变量。
- `.env`(本机实值,已在 .gitignore)与 `.env.example`(模板,豁免忽略)就位;通用代码中无 E 盘硬编码(local_vendor incoming 改为 `--incoming-root`/`LOCAL_VENDOR_RAW_ROOT`,未配置即报错)。
- sync CLI 亦加载 `.env`;sync 与 backtest 数据根同为 `MARKET_DATA_ROOT` 环境解析,链路一致。
- 数据管理 API 新增 `astock_env`、`latest_local_vendor`、每 dataset `earliest_date/latest_date/survivorship_bias/universe_type/warning_text`;对正式根实测返回真实值。

## 3. 宇宙动态化与证券分类(P0-3 关闭)

新增 [wtpy/apps/astock/data/vendor_universe.py](../wtpy/apps/astock/data/vendor_universe.py)(详见《vendor_full_import_universe_definition.md》):

- 分类 = 通用代码段规则(SSE 600-605/688-689;SZSE 000-004/**30x 整段**(302132 等新段自然覆盖,无单票特例);BSE 4/8/92x)+ **CSV 元数据确认**(total_share/float_share>0);交易所-代码段不匹配拒绝;未识别绝不纳入。
- `is_ashare_code` 的 `("300","301")` 两处硬编码改为通用 `30x` 段判断。
- 生产代码 grep 证实无 5055/5795/5796 硬编码;正式导入宇宙实时构建。
- 实测(元数据确认模式,2026-07-26):并集 5796 全部纳入(沪主板 1710/科创 611/深主板 1500/创业板 1404/北交 571),excluded=0,unidentified=0,疑似退市(最新年缺席)249;快速段规则与元数据确认两模式 hash 一致(`70c5c0becd27…`)。产出 `tmp/vendor_full_import_universe.csv` + `E:\AStockData\reports\vendor_full_import_universe.csv`(18 列/5796 行)。

## 4. 幸存者偏差策略落码(P0-2 标记,数据缺陷本身归 Gate B)

- `DatasetManifest` 新增 12 个宇宙/偏差字段 + 8 个覆盖核算字段(见 dataset_store.py),旧 manifest 向后兼容(缺省加载,测试覆盖)。
- local_vendor 全量同步默认写入:`universe_type=vendor_available_historical_union`、`survivorship_bias=true`、`historical_universe_complete=false`、`delisted_coverage_complete=false`、覆盖年份、六只实证缺失退市股样本(明确标注非穷举)、固定警示文案、recommended/prohibited 用途。
- 前端数据仓库面板:显著红/橙警告横幅(非小标签)、最新 local_vendor 数据集行、每行日期范围与"偏差"标记(旧后端字段缺失时全部容错为"—")。

## 5. no_data / failed 严格策略

`evaluate_strict_publish()`(dataset_store.py):ready 仅当 `failed==0` 且每个 no_data 均在**显式 allowlist**(CSV symbol,reason)内且未超 `--max-no-data-count/ratio` 上限;否则 partial 并打印阻断原因。manifest 记录 expected/imported/excluded/no_data/failed/warning/coverage_ratio/no_data_allowlist。测试 6 项覆盖(含比例阈值与超限)。

## 6. 单位文档统一

- local_vendor.py 模块头改为"千元 →×1000",并注明 400/400 VWAP 实证与"禁止改回万元"。
- 守卫测试:`test_unit_consistency.py` 断言 `Amount unit: 千元` 存在、`Amount unit: 万元` 不存在、×100/×1000 数值解析正确,并扫描 data 层禁止冲突表述。

## 7. CLI 完整性(断点续传)

`sync_local_vendor_full` 重构:锁 → 宇宙(--symbol > --universe-file > 动态元数据宇宙)→ **分块 ZIP-first + checkpoint**(每块完成原子落盘;中断后 `--resume` 按 universe_hash/日期/块大小校验匹配并跳过已完成块;不带 `--resume` 且存在 checkpoint 时**拒绝执行**,`--fresh` 显式作废)→ 严格发布策略 → 幸存者字段 manifest → sync_log(+`--log-path/--report-path` 副本)→ 成功后清理 checkpoint。分块设计同时把内存峰值限定在块级(实测 RSS 711MB @ chunk=250)。`--dry-run/--preflight` 真实生效(preflight 含锁探测/checkpoint 状态/磁盘);local_vendor 拒绝非 none/1d;分钟数据结构上不可达(仅日 K 目录被识别)。

## 8. 测试

新增 7 个测试文件共 77 项(锁 14、防呆 8、宇宙 40、策略 9、单位 5、CLI 6 等),**默认 `python -m pytest -q`:631 passed / 1 skipped(live_tdxquant)/ 0 failed**。

## 9. 后续(非 Gate A 阻断,移交后续轮)

- `limit_rules.py` / `cross_section.py` 仍有 `("300","301")` 硬编码(涨跌停/板块标签,302 段应同步通用化——影响 30x 新段涨跌停判定,建议 Gate C 前处理);
- Tushare 退市补充与 composite dataset(设计见《tushare_delisted_composite_dataset_plan.md》);
- TdxQuant/Tushare 信号数据集向正式根同步(Gate C);
- 前端 execution 数据集选择控件(Gate C)。
