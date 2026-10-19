#!/usr/bin/env python3
"""Smoke check: do the Demo datasources have recent data in LogicMonitor, and are alerts open?

Run inside the lm-demo container (it has the admin token):
  docker exec lm-demo python3 -I /app/lm_check.py
"""
import sys, time
sys.path.insert(0, "/app")
import server as s

# every (resource, datasource) the console pushes to, from the config file
CHECKS = sorted({(host, ds) for host, ds, *_ in s.BASELINE})


def get(path, q=""):
    st, j = s.lm_call("GET", path, q)
    if st != 200:
        print(f"  !! {path} -> {st} {j.get('errorMessage', '')}")
    return j


def device_id(name):
    items = get("/device/devices", f'size=5&fields=id,name,displayName&filter=name:"{name}"').get("items") or []
    return items[0]["id"] if items else None


for host, ds in CHECKS:
    did = device_id(host)
    if not did:
        print(f"{host:14} {ds:14} no such resource"); continue
    hds = get(f"/device/devices/{did}/devicedatasources", f'size=5&fields=id,dataSourceName&filter=dataSourceName:"{ds}"').get("items") or []
    if not hds:
        print(f"{host:14} {ds:14} datasource not applied"); continue
    insts = get(f"/device/devices/{did}/devicedatasources/{hds[0]['id']}/instances", "size=20&fields=id,name,displayName").get("items") or []
    for inst in insts:
        data = get(f"/device/devices/{did}/devicedatasources/{hds[0]['id']}/instances/{inst['id']}/data", f"start={int(time.time()) - 1800}")
        pts = data.get("dataPoints") or []
        vals = data.get("values") or []
        times = data.get("time") or []
        last = dict(zip(pts, vals[0])) if vals else {}
        age = int(time.time() - times[0] / 1000) if times else None
        short = ", ".join(f"{k}={v}" for k, v in list(last.items())[:4])
        print(f"{host:14} {ds:14} {inst['displayName']:22} {len(times):3} pts, newest {age}s ago: {short}")

alerts = get("/alert/alerts", "size=50&fields=id,monitorObjectName,instanceName,dataPointName,alertValue,severity&filter=cleared:false").get("items") or []
print(f"\nopen alerts: {len(alerts)}")
for a in alerts:
    print(f"  sev{a.get('severity')} {a.get('monitorObjectName')} / {a.get('instanceName')} / {a.get('dataPointName')} = {a.get('alertValue')}")
