# 数据集备份与恢复手册

## 备份范围

| 目录 | 内容 | 重要性 |
|------|------|--------|
| `E:\AStockData\datasets\market_data\manifests\` | 数据集 manifest | 关键 |
| `E:\AStockData\datasets\market_data\blobs\` | content-addressed 行情 blob | 关键 |
| `E:\AStockData\datasets\market_data\universes\` | PIT 宇宙文件 | 关键 |
| `outputs\astock\` | 回测结果 | 可重建 |
| `wtpy\apps\astock\*.db` | SQLite 运行记录 | 可重建 |

## 备份命令

```powershell
# 完整备份（推荐定期执行）
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dst = "E:\AStockData\backups\market_data_$stamp"
robocopy "E:\AStockData\datasets\market_data" $dst /MIR /MT:8 /R:2 /W:5
```

## 恢复

```powershell
# 从备份恢复
robocopy "E:\AStockData\backups\market_data_<stamp>" "E:\AStockData\datasets\market_data" /MIR /MT:8
```

## 完整性校验

恢复后验证：

```python
from wtpy.apps.astock.config import get_default_config, load_env_file
from wtpy.apps.astock.data.dataset_store import DatasetStore
load_env_file()
store = DatasetStore(get_default_config().market_data_root)
for mid in store.list_manifests():
    m = store.load_manifest(mid)
    if m.status != "ready":
        continue
    missing = [s.symbol for s in m.symbols if s.blob_sha256 and not store.blob_exists(s.blob_sha256)]
    if missing:
        print(f"BROKEN: {mid} missing {len(missing)} blobs")
    else:
        print(f"OK: {mid} ({m.symbol_count} symbols)")
```

## 注意事项

- blob 是 content-addressed 的，相同内容只存一份
- 不要只备份 manifest 不备份 blob
- 备份前确认没有正在运行的同步任务（检查任务锁）
- 恢复后运行完整性校验脚本确认无缺失
