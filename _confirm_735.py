import sys, json
from pathlib import Path
from types import ModuleType

ROOT = Path(r"E:\Software Development\wtpy-master")
sys.path.insert(0, str(ROOT))
for k in list(sys.modules):
    if k.startswith("wtpy.apps.astock"):
        del sys.modules[k]

def ensure(n, p):
    if n in sys.modules:
        return
    m = ModuleType(n)
    m.__path__ = [str(p)]
    m.__package__ = n
    sys.modules[n] = m

for n, p in [
    ("wtpy", ROOT / "wtpy"),
    ("wtpy.apps", ROOT / "wtpy" / "apps"),
    ("wtpy.apps.astock", ROOT / "wtpy" / "apps" / "astock"),
    ("wtpy.apps.astock.data", ROOT / "wtpy" / "apps" / "astock" / "data"),
    ("wtpy.apps.astock.indicators", ROOT / "wtpy" / "apps" / "astock" / "indicators"),
    ("wtpy.apps.astock.bagua", ROOT / "wtpy" / "apps" / "astock" / "bagua"),
]:
    ensure(n, p)

from wtpy.apps.astock.cli import main, _astock_code_sha
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.indicators.registry import IndicatorRegistry
from wtpy.apps.astock.indicators.tn6_importer import load_source_map, resolve_formula_audit

cfg = get_default_config()
ind = Path(cfg.indicator_dir)
tn6 = list(ind.glob("*735*.tn6"))[0]
src = list(ind.glob("*735*.txt"))[0]
print("tn6", tn6.name)
print("src", src.name)

rc = main([
    "confirm-indicator-source",
    "--tn6", str(tn6),
    "--source", str(src),
    "--confirmed-by", "我",
    "--note", "用户在会话中明确确认：735金叉及趋势.txt 为对应 TN6 的人工提供公式源码",
    "--confirm-user-provided",
])
print("confirm_rc", rc)

reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
s735 = [s for s in reg.list() if s.package_sha256 and "735" in s.name][0]
print("indicator", s735.id, "compile", s735.compile_status)

mapping = load_source_map(cfg.mapping_path)
entry = mapping.get(s735.package_sha256) or {}
audit = resolve_formula_audit(entry, package_sha256=s735.package_sha256)
print("audit", json.dumps(audit, ensure_ascii=False))
print("validate", main(["validate-indicator", s735.id]))

codes = "sh600000,sz000001,sh600519,sz000002,sh601318,sz300750,sh600036,sz000858,sh601166,sz002415"
# formal backtest after confirm
rc_f = main([
    "backtest",
    "--indicator", s735.id,
    "--period", "DAY",
    "--hold", "5",
    "--codes", codes,
    "--start", "20200101",
    "--end", "20241231",
    "--stop-loss", "0.03",
    "--take-profit", "0.08",
    "--run-id", "pool10_735_formal_v1",
])
print("formal_rc", rc_f)

live = _astock_code_sha()
root = cfg.output_root / "pool10_735_formal_v1"
meta = json.loads((root / "run_meta.json").read_text(encoding="utf-8"))
m = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
repro = meta.get("repro") or {}
print("status", meta.get("status"))
print("price_mode", repro.get("price_mode"))
print("provenance", repro.get("formula_provenance"), repro.get("source_pair_status"))
print("formal_allowed", repro.get("formal_backtest_allowed"))
print("ret", m.get("total_return"), "buys", m.get("n_buys"), "sells", m.get("n_sells"), "open", m.get("n_open_positions"))
print("reasons", m.get("sell_reason_counts"))
print("code_sha_match", repro.get("astock_code_sha") == live)
print("xlsx", (root / "summary.xlsx").exists(), "failed", (root / "summary.xlsx.failed").exists())
print("LIVE", live)
print("path", root)
