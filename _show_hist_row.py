# -*- coding: utf-8 -*-
from pathlib import Path
t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
i = t.find("return \"<tr class='hist-row")
print(t[i : i + 1200])
