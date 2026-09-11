# 一次性探测脚本：查几只股票在本机 tushare_qfq 数据集里的历史日期覆盖范围
# 用法：python -m tools.probe_data_coverage
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.repository import MarketDataRepository


def main() -> int:
    cfg = get_default_config()
    store = DatasetStore(cfg.market_data_root)
    repo = MarketDataRepository(store)
    ds_list = repo.list_datasets()
    ready = [d for d in ds_list if d.status == "ready"]
    print(f"ready datasets: {len(ready)}")
    for d in ready:
        print(
            f"  - {d.dataset_id} | adj={getattr(d,'adjustment',None)} "
            f"| mode={getattr(d,'storage_mode',None)} | cutoff={getattr(d,'cutoff',None)}"
        )

    # 找 tushare_qfq dataset
    tq = next((d for d in ready if "tushare_qfq" in d.dataset_id), None)
    if not tq:
        print("no tushare_qfq dataset")
        return 1
    print("\nusing:", tq.dataset_id)

    sym_map = {s.symbol: s for s in tq.symbols}
    print("total symbols in dataset:", len(sym_map))
    # 看 symbol 格式样本
    for s in list(tq.symbols)[:3]:
        print("  sample symbol:", s.symbol, "rows=", s.row_count, "blob=", s.blob_sha256[:12] if s.blob_sha256 else None)

    targets = ["SZSE.STK.000001", "SSE.STK.600000", "SZSE.STK.000333", "SSE.STK.510300"]
    for sym in targets:
        ent = sym_map.get(sym)
        if not ent:
            print(f"\n{sym}: NOT in dataset symbols")
            continue
        if not ent.blob_sha256:
            print(f"\n{sym}: no blob (overlay?) rows={ent.row_count}")
            continue
        try:
            arr = store.load_bars(ent.blob_sha256)
            td = arr["trade_date"]
            print(f"\n{sym}: rows={len(td)} min={td.min()} max={td.max()}")
        except Exception as e:
            print(f"\n{sym}: load err: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
