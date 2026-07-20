# -*- coding: utf-8 -*-
from pathlib import Path
import re

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
i = t.find("function fracToPctNumber")
print("=== PCT HELPERS ===")
print(t[i : i + 1400])
print("=== METRIC RENDER ===")
i = t.find("keys.forEach(k =>")
print(t[i : i + 900])
print("=== history cells ===")
i = t.find("historyReturnDisplay(m)")
print(t[i - 200 : i + 400])
