# -*- coding: utf-8 -*-
from pathlib import Path
import re

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
for m in re.finditer(r'id="[^"]*"', t):
    s = m.group(0)
    if any(k in s.lower() for k in ("hold", "entry", "lag", "stop", "take", "period")):
        print(s, "at", m.start())
        print(t[max(0, m.start() - 100) : m.start() + 150])
        print("---")

# body construction
i = t.find("hold:")
print("hold body", t[i - 200 : i + 400] if i >= 0 else "none")
