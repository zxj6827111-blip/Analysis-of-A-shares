# -*- coding: utf-8 -*-
import json
from pathlib import Path
from datetime import datetime, timezone
import sys
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
]:
    ensure(n, p)

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.indicators.tn6_importer import load_source_map, save_source_map, resolve_formula_audit

cfg = get_default_config()
mapping = load_source_map(cfg.mapping_path)
pkg = "bbd591d2e0e4aae24aa26a37ed963665d87332359c290598c84ef48f3bf22144"
src = "d3f513487e4c89df3569461b51ff6afe1e745b9dc34fac6460922d7c99d6638c"
entry = dict(mapping[pkg])
entry["formula_provenance"] = "user_provided_human_formula"
entry["source_pair_status"] = "paired_confirmed"
entry["formal_backtest_allowed"] = True
entry["research_backtest_allowed"] = True
entry["note"] = "用户在会话中明确确认：735金叉及趋势.txt 为对应 TN6 的人工提供公式源码"
entry["confirmation"] = {
    "package_sha256": pkg,
    "source_sha256": src,
    "confirmed_at": datetime.now(timezone.utc).isoformat(),
    "confirmed_by": "我",
    "confirmation_method": "cli_confirm_user_provided",
    "note": "用户在会话中明确确认：735金叉及趋势.txt 为对应 TN6 的人工提供公式源码",
    "schema_version": 1,
}
mapping[pkg] = entry
save_source_map(cfg.mapping_path, mapping)
audit = resolve_formula_audit(entry, package_sha256=pkg)
print(json.dumps({"confirmed_by": entry["confirmation"]["confirmed_by"], "audit": audit}, ensure_ascii=False, indent=2))

# also patch formal run_meta provenance fields if missing formal_backtest_allowed
meta_path = cfg.output_root / "pool10_735_formal_v1" / "run_meta.json"
meta = json.loads(meta_path.read_text(encoding="utf-8"))
repro = meta.setdefault("repro", {})
repro["formula_provenance"] = "user_provided_human_formula"
repro["source_pair_status"] = "paired_confirmed"
repro["formal_backtest_allowed"] = True
repro["confirmed_by"] = "我"
meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print("run_meta updated")
