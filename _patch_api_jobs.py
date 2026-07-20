# -*- coding: utf-8 -*-
from pathlib import Path

p = Path(r"E:\Software Development\wtpy-master\wtpy\apps\astock\api.py")
t = p.read_text(encoding="utf-8")
needle = '''    @app.get("/api/v1/runs")
    def api_runs(limit: int = Query(50, ge=1, le=200)) -> List[dict]:
        return list_runs(cfg, limit=limit)
'''
insert = '''    @app.get("/api/v1/runs")
    def api_runs(limit: int = Query(50, ge=1, le=200)) -> List[dict]:
        return list_runs(cfg, limit=limit)

    @app.get("/api/v1/backtests/jobs")
    def api_jobs(limit: int = Query(30, ge=1, le=100)) -> List[dict]:
        return jobs.list_public(limit=limit)
'''
if needle not in t:
    raise SystemExit("api runs block missing")
p.write_text(t.replace(needle, insert, 1), encoding="utf-8")
print("api patched")
