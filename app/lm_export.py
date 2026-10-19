#!/usr/bin/env python3
"""Export dashboard widget data and open alerts from LogicMonitor as JSON (for the deck's backup slides).

  docker exec lm-demo python3 -I /app/lm_export.py 45 > export.json     # last 45 minutes
"""
import json, sys, time, urllib.parse
sys.path.insert(0, "/app")
import server as s

DASHES = list(s.DASHBOARD_NAMES.values())   # the dashboards named in the config file
minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 45
end = int(time.time()); start = end - minutes * 60
out = {"start": start, "end": end, "dashboards": {}, "alerts": []}
for name in DASHES:
    st, j = s.lm_call("GET", "/dashboard/dashboards", "size=5&fields=id,name&filter=" + urllib.parse.quote(f'name:"{name}"'))
    if not j.get("items"):
        continue
    did = j["items"][0]["id"]
    st, j = s.lm_call("GET", f"/dashboard/dashboards/{did}/widgets", "size=100&fields=id,name,type,colSpan,rowSpan")
    widgets = []
    for w in sorted(j.get("items") or [], key=lambda w: w["id"]):
        if w["type"] not in ("cgraph", "bigNumber"):
            continue
        st, d = s.lm_call("GET", f"/dashboard/widgets/{w['id']}/data", f"start={start}&end={end}")
        widgets.append({"name": w["name"], "type": w["type"], "status": st, "data": d})
    out["dashboards"][name] = {"id": did, "widgets": widgets}
st, j = s.lm_call("GET", "/alert/alerts", "size=100&fields=id,monitorObjectName,instanceName,dataPointName,alertValue,severity,startEpoch,threshold&filter=cleared:false")
out["alerts"] = j.get("items") or []
json.dump(out, sys.stdout)
