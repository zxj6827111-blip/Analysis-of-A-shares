# 数据同步中断恢复手册

## 同步任务

| 脚本 | 用途 | 幂等 |
|------|------|------|
| `scripts/sync_market_data.py` | local_vendor 全量导入 | 是（checkpoint） |
| `scripts/sync_tushare_delisted.py` | Tushare 退市股日线同步 | 是（checkpoint） |

## 中断恢复机制

### local_vendor 同步

- 使用文件级 checkpoint：每只股票处理完后记录进度
- 任务锁防止并发：`<market_data_root>/.sync_lock`
- 中断后重新运行同一命令即可从 checkpoint 恢复
- 已发布的 ready 数据集不会被重新处理

### Tushare 退市股同步

- 每只股票独立下载、验证、写入
- checkpoint 文件记录每只股票的状态：pending / downloaded / validated / no_data / failed
- 中断后重新运行只处理 pending 和 failed 的股票
- API 限速：指数退避重试
- 原子文件写入：先写 `.tmp` 再 `os.replace`

## 手动恢复步骤

```powershell
# 1. 确认没有残留锁
Get-Content "E:\AStockData\datasets\market_data\.sync_lock" -ErrorAction SilentlyContinue

# 2. 如果锁过期（进程已死），手动删除
Remove-Item "E:\AStockData\datasets\market_data\.sync_lock" -ErrorAction SilentlyContinue

# 3. 重新运行同步
python scripts/sync_market_data.py          # local_vendor
python scripts/sync_tushare_delisted.py     # tushare 退市股
```

## 故障排查

| 症状 | 原因 | 处理 |
|------|------|------|
| `LOCK_HELD` | 另一个同步进程在运行 | 等待或确认进程已死后删锁 |
| `API_RATE_LIMIT` | Tushare 限速 | 自动退避重试，无需干预 |
| `NO_DATA` | 股票在 Tushare 无数据 | 记录为 no_data，不阻塞其他股票 |
| `VALIDATION_FAILED` | OHLC 校验失败 | 标记为 failed，需人工检查 |
| manifest 状态为 building | 同步中断在发布前 | 重新运行同步，会自动完成发布 |

## 注意事项

- 不要在同步运行期间手动修改 manifest
- 不要删除 checkpoint 文件（除非要完全重跑）
- 同步完成后检查 dataset status 是否为 ready
- partial 状态的 dataset 不可用于回测
