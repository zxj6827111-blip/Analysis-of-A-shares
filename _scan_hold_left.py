# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/strategy.py").read_text(encoding="utf-8")
# find hold_left decrement and position create
for key in ["hold_left_sessions", "h_sess", "After entry day"]:
    i = 0
    c = 0
    while c < 8:
        j = t.find(key, i)
        if j < 0:
            break
        print(f"==== {key} @ {j}")
        print(t[j - 100 : j + 350])
        print()
        i = j + len(key)
        c += 1
