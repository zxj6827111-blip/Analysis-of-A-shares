# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/strategy.py").read_text(encoding="utf-8")
for key in ['side="SELL"', "side='SELL'", "def _is_exit_due", "def _price", "execute", "open_px", "close_px", "bar.open", "bar.close", ".open", ".close"]:
    i = t.find(key)
    print(key, i)

# print sell block by searching Fill( near SELL reason
i = 0
n = 0
while n < 5:
    j = t.find("Fill(", i)
    if j < 0:
        break
    snippet = t[j : j + 350]
    if "SELL" in snippet or "side" in snippet:
        print("==== Fill at", j)
        print(t[max(0, j - 200) : j + 400])
    i = j + 4
    n += 1

i = t.find("def _is_exit_due")
print("==== exit due")
print(t[i : i + 600])

# how sell price chosen
i = t.find("sell_codes")
print("==== sell_codes context")
print(t[i : i + 1500])
