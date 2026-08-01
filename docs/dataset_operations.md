# 数据集运维手册

## 数据集生命周期

```
building → validating → atomic_publish → ready
                                      ↘ failed
```

- `ready` 数据集不可修改（immutable）
- 不得覆盖同 ID 的旧数据集
- 不得删除已有 manifest 或 blob

## dataset_id 命名规则

```
<source>_<adjustment>_<period>_<cutoff>_<hash>
```

hash 由以下内容决定：source, adjustment, period, cutoff, parent IDs, parent hashes, formula version, merge rule version, schema version, content fingerprint。

## 数据目录结构

```
E:\AStockData\datasets\market_data\
├── manifests\           # JSON manifest 文件
│   └── <dataset_id>.json
├── blobs\               # content-addressed blob 存储
│   └── <sha256>.npz
└── universes\           # PIT 宇宙文件
    └── pit_universe_1d_<cutoff>_<hash>.json
```

## 常用操作

### 查看数据集列表

```python
from wtpy.apps.astock.config import get_default_config, load_env_file
from wtpy.apps.astock.data.dataset_store import DatasetStore
load_env_file()
cfg = get_default_config()
store = DatasetStore(cfg.market_data_root)
for mid in store.list_manifests():
    m = store.load_manifest(mid)
    print(f"{m.dataset_id}  status={m.status}  syms={m.symbol_count}  rows={m.row_count}")
```

### 验证数据集完整性

```python
m = store.load_manifest(dataset_id)
for s in m.symbols:
    assert store.blob_exists(s.blob_sha256), f"missing blob for {s.symbol}"
```

### 读取单只股票

```python
from wtpy.apps.astock.data.repository import MarketDataRepository
repo = MarketDataRepository(store)
bars = repo.load_bars(dataset_id=dataset_id, symbol="SSE.STK.600000",
                      start_date=20200101, end_date=20201231)
```

## 禁止事项

- 不得手动编辑 manifest JSON
- 不得删除 blob 文件
- 不得修改 ready 数据集
- 不得在回测运行时调用 Provider 或网络
- 不得将 partial/building/failed 数据集用于回测
