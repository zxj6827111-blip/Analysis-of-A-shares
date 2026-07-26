# 多行情源改造 — 第二轮修复独立复核

**复核日期**: 2026-07-25  
**角色**: 独立代码审查 / 验收测试（不修改正式代码、不 commit/push）  
**对照**: 上一轮复核 `docs/multi_source_market_data_recheck_acceptance.md`（FAIL，P0-R1/R2 阻断）

---

## 1. 最终结论

### **CONDITIONAL PASS — 有条件通过**

相对上一轮 **FAIL**，本轮针对复核阻断项的修复 **经代码审查 + 运行探针 + 全量测试核实，主路径成立**。

| 项 | 结论 |
|----|------|
| 默认 `python -m pytest -q` | **537 passed, 1 skipped, 0 failed**（~67s）；无 collection error |
| P0-R1 符号互查 | **关闭**（代码 + 独立脚本 + 新单测） |
| P0-R2 成功 run 落库 | **关闭**（`backtest_artifacts` + `experiments._run_one`） |
| P1-R3 adjustment 默认 | **关闭** |
| P1-R5 双源创建锁 dataset | **基本关闭**（有 soft-fail 残留，见 P1） |
| Live TdxQuant / Tushare | **仍未独立验证** |
| 100 股双源 / 真实同步 | **仍 NOT VERIFIED** |

**不满足正式 PASS**（原标准要求 live 通过、双源真实回测证据等）。  
**满足 CONDITIONAL PASS**：核心接线与阻断 P0 已关；剩余主要为 live / 规模验收 / 体验与边界（P1/P2）。

**合并建议**: **有条件可合并到功能分支继续联调**；**不建议**在无 live 与无真实 dataset 的情况下宣称「生产多源已完成」。若合入主线，须在发布说明中写明 **CONDITIONAL** 与剩余事项。

`merge_recommended`: **conditional**（功能分支可合；主线需附条件）

---

## 2. Git 状态（复核时）

| 项 | 值 |
|----|-----|
| 分支 | `feat/multi-source-market-data` |
| HEAD | `cad0742746aa6f675b6fc4b17798ee43b482c2df` |
| 工作区 | **不干净**（修复未 commit） |
| 已跟踪 diff | 11 files，约 +422 / -47（另有未跟踪 providers/store/scripts/tests/`pytest.ini`） |

---

## 3. 阻断项逐条复核

### P0-R1 符号格式 — **FIXED**

**代码**: `repository.py`  
- `_symbol_variants()`：`SSE.STK.600000` ↔ `600000.SH` ↔ 裸码；SZ/BSE 同类  
- `_find_symbol_record()` + `load_bars` 单股路径使用别名查找  

**独立探针**:

```
store symbol=600000.SH → load_bars(symbol='SSE.STK.600000') → 3 bars, close 正确
store SZSE.STK.000001 → load 000001.SZ → OK
store SSE.STK.600000 → load 600000.SH → OK
```

**测试**: `TestSymbolFormatResolution`（4 例，含 missing 仍 raise）

**残余 P2**: 同步层仍可写入异构 symbol；依赖读侧兼容，长期建议 sync 统一 `SSE.STK.*`。

---

### P0-R2 成功 run 落库 — **FIXED**

| 路径 | 证据 |
|------|------|
| 成功回测 | `backtest_artifacts.py` `append_run_index` 含 6 字段（~427–432） |
| 实验成功 | `experiments.py` `_run_one` upsert 含 6 字段（~1296–1301） |
| no_go 路径 | `backtest.py` ~605+ 亦含多源字段（相对更完整） |
| DB upsert | 此前已支持 INSERT/UPDATE 6 列 |

**残余 P1**: 成功路径依赖 `getattr(req, ...)`；若 lock 写在局部变量未回写 `req` 会丢字段——当前 `req.dataset_id = _resolved_dataset_id` 与 `req.signal_adjustment = ...` **已回写**，可接受。

**测试**: 仍以直接 `upsert_run_from_index_row` 为主；**缺少**「跑完 artifacts 再读 DB」的端到端断言（P1 测试债）。

---

### P1-R3 adjustment 空值 — **FIXED**

`backtest.py` ~171–177：`tdxquant`/`tushare` 且 adjustment 空时用 `SIGNAL_SOURCE_ADJUSTMENT` 填充并写回 `req`。

---

### P1-R5 双源创建锁 dataset — **MOSTLY FIXED**

`experiments.py` dual 分支：

1. 对 tdxquant/front、tushare/qfq 调用 `resolve_latest_ready`  
2. `dv["dataset_id"] = _resolved_ds.get(src.value)`  

**残余 P1-R5b（重要）**:

- resolve 失败时 `except: _resolved_ds[src]=None`，**仍创建实验**，variant 带 `dataset_id=None`  
- 仅当 `market_data` 目录存在时才 resolve  
- **不会**在双源创建时强制「两源都必须 ready」  

→ 相对「创建时锁定」目标：**尽力锁定**，非 **硬失败**。运行期 backtest 仍会再次 resolve 或报错，可接受为 P1，不升 P0。

集成测试 `test_dual_source_generates_two_variants_per_base` **仍手写** `dataset_id=None`，未断言生产 resolve 行为（测试债）。

---

## 4. Gate 复核

| Gate | 上轮 | 本轮 | 说明 |
|------|------|------|------|
| 1 Provider/Repo/兼容 | PARTIAL | **PASS** | 抽象 + 接线 + 符号兼容；旧路径默认仍可用 |
| 2 同步/301107/原子 | PARTIAL | **PARTIAL** | 实现在；**无 live** |
| 3 回测 source/锁/缓存 | PARTIAL | **PASS** | resolve/lock、L1 repo、缓存 key、落库主路径成立 |
| 4 实验/双源 UI 链路 | PARTIAL | **PASS*** | 双源扩展 + 尽力锁 id；*创建 soft-fail 见 P1 |
| 5 BSE/退市/100 股 | PARTIAL | **PARTIAL** | 单测有；真实与 100 股未做 |

与开发自述「Gate 1/3/4 = PASS，2/5 = PARTIAL」**基本一致**；Gate 4 附 soft-fail 脚注。

---

## 5. 自动化测试

```
python -m pytest -q
→ 537 passed, 1 skipped, 4 warnings, ~67.13s
→ EXIT=0
```

- 开发称 **0 skipped**：本环境仍有 **1 skipped**（可忽略差异，非失败）。  
- Collection errors：**无**。  
- Live markers：已注册，**本轮未跑** `-m live_*`。

---

## 6. 安全

- 未发现 Token 明文进入跟踪代码。  
- `token_leak_found = false`

---

## 7. 残留问题清单

### P0

**无**（上一轮 P0-R1/R2 关闭）

### P1

1. **双源创建 soft-fail**：缺 ready dataset 仍创建且 `dataset_id=None`  
2. **端到端测试债**：完整 `BacktestService.run` + mock Provider + 读 DB 六字段仍弱  
3. **Live / 真实同步 / 301107 真实日期** 无本轮证据  
4. **Tushare 增量/adj 因子驱动 rebuild** 仍不完整  
5. **vendor_native 周线** 字段贯通，引擎侧实装仍偏薄  

### P2

- 根目录指数 CSV、`tmp/tdxquant_probe` 不宜入库  
- sync 层 symbol 未强制标准化  
- 工作区未 commit，交付物分散  

---

## 8. 正式上线前剩余事项（CONDITIONAL 条件）

1. 在 live 环境跑通：`pytest -m live_tdxquant` / `live_tushare`（脱敏证据）  
2. 小规模真实 sync → 双源实验 → 结果页核对 dataset_id  
3. 双源创建：任一侧无 ready 时 **拒绝创建或明确 warning+阻断运行**  
4. 补一条：artifacts 成功路径后 SQLite 六字段断言  
5. 100 股差异报告（Gate 5）可后置但应排期  

允许延后：Parquet、Linux 同步、非核心 UI。

---

## 9. 是否建议合并

| 目标 | 建议 |
|------|------|
| 继续在 `feat/multi-source-market-data` 联调 | **是** |
| 合并主线并宣称多源生产就绪 | **否**（缺 live 与真实数据门禁） |
| 合并主线并标注 CONDITIONAL | **可**，须附上节条件 |

---

## 附录

- 命令：`tmp/multi_source_market_data_recheck2_commands.txt`  
- JSON：`tmp/multi_source_market_data_recheck2_acceptance.json`  
- Pytest 日志：`tmp/_recheck2_pytest.txt`
