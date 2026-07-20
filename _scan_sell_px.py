# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/strategy.py").read_text(encoding="utf-8")
# sell loop from for code in list(sell_codes)
i = t.find("for code in list(sell_codes):")
print(t[i : i + 2200])
print("\n===== BUY px =====")
i = t.find('side="BUY"')
print(t[i - 500 : i + 200])
print("\n===== module docstring =====")
print(t[:900])
