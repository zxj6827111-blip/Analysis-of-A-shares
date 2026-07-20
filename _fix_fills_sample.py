# -*- coding: utf-8 -*-
from pathlib import Path
import ast

p = Path("wtpy/apps/astock/reports.py")
lines = p.read_text(encoding="utf-8").splitlines(True)
out = []
changed = False
for l in lines:
    if "fills_sample" in l and "to_dict" in l:
        out.append(
            '    full_meta["fills_sample"] = [\n'
            "        asdict(f) if is_dataclass(f) else str(f) for f in result.fills[:20]\n"
            "    ]\n"
        )
        changed = True
    else:
        out.append(l)
text = "".join(out)
if "is_dataclass" not in text[:800]:
    text = text.replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\nfrom dataclasses import asdict, is_dataclass\n",
        1,
    )
p.write_text(text, encoding="utf-8")
ast.parse(text)
print("changed", changed, "ok")
