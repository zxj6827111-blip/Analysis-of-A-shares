# 正式 CLI 中断恢复与并发锁演练报告(Gate A)

| 项目 | 说明 |
|---|---|
| 状态 | 通过 |
| 日期 | 2026-07-26 |
| 证据 | [tmp/vendor_full_import_cli_recovery.json](../tmp/vendor_full_import_cli_recovery.json) |
| 环境 | 临时数据根(scratchpad),原始 ZIP 只读;正式命令 `python scripts/sync_market_data.py --source local_vendor --mode full` |

## 演练设计

- 宇宙:400 只沪主板老股票(universe-file,历史行数大,保证 kill 窗口),`--chunk-size 100` → 4 块。
- 触发器不依赖 stdout(首轮踩坑:管道缓冲导致 kill 落空),改用**磁盘状态**:轮询 checkpoint 文件,完成 2 块即强杀子进程;双进程测试轮询锁文件元数据确认 A 真实持锁后再启动 B。
- 预置一个旧 ready dataset,验证全程不可被破坏。

## 结果(逐项)

| 检查 | 结果 |
|---|---|
| preflight / dry-run(正式 CLI) | rc=0,含锁/checkpoint/磁盘行,零写入 |
| 强杀落点 | checkpoint=2/4 时 kill,子进程存活中(killed_mid_run=true) |
| 杀时锁持有者 | 锁元数据 pid=子进程 pid,无 released_at ✅ |
| 杀后无伪 ready | 唯一 manifest 仍为旧 ready;no_fake_ready=true ✅ |
| 旧 ready 保护 | manifest 字节级原样;resolve 仍返回旧 ready ✅ |
| checkpoint 持久化 | 2/4 块记录在盘,无 .tmp blob 残留 ✅ |
| 不带 --resume 重跑 | **拒绝执行**(提示 --resume/--fresh),未产生第二个 dataset ✅ |
| --resume 续传 | 跳过 2 个已完成块(日志 "skipped (checkpoint)"),沿用原 sync_run_id ✅ |
| 最终发布 | ready,400/400 imported,no_data=0,failed=0,coverage=1.0,2,339,035 行 ✅ |
| 数据完整性 | 抽样 5 只:日期无重复、blob SHA256=内容 ✅ |
| manifest 政策字段 | survivorship_bias=true、universe_type、warning_text 均在 ✅ |
| 收尾 | checkpoint 清理、锁 released_at 落盘、resolve 切换到新 ready ✅ |
| 已存 blob 复用 | 杀后 201 blob 未被重写重复(内容寻址幂等) ✅ |

## 双进程排他(Windows 真实)

独立 lock-root:A 任务(400 只)确认持锁且存活 → 立即启动同 scope B 任务(50 只):

- B 立即失败:`ERROR: Sync lock held: … holder pid=44088 host=Zhang start=2026-07-26T15:39:54 sync_run_id=localvendor_20260726T153954_… alive=True` → `concurrent_lock`;
- B **未写任何 manifest**(manifests 目录为空);
- 强杀 A 后锁由 OS 释放,后续任务可获锁并报告 stale 恢复(单元测试 TestCrossProcess 覆盖)。

## 性能演练(同轮,正式 CLI)

[tmp/vendor_full_import_performance_pilot.json](../tmp/vendor_full_import_performance_pilot.json):

- 500 只随机(全板块)×27 年,chunk=250:**18.7s**,1,400,841 行,500/500 ready,13.3 s/百万行;
- **RSS 峰值 711MB**(ctypes psapi 实测,分块限界,与宇宙规模无关);blob 28.2MB / manifest 0.14MB;
- 全量推算(行数法,非简单只数线性):约 16.24M 行 → 导入 ≈3.6 分钟 + 元数据宇宙扫描 2–4 分钟 ≈ **总计 ~7 分钟**;blob ≈0.33GB;建议预留磁盘 ≥20GB、可用内存 ≥8GB(实际远低于此)。
- 环境:E 盘空闲 120GB,RAM 63.1GB(空闲 28.7GB)→ 满足。
