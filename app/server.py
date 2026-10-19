#!/usr/bin/env python3
"""LogicMonitor demo console.

Simulates website traffic, infrastructure trouble and business-level failures
for the sites described in a JSON config file and sends the resulting traces,
logs and metrics straight to LogicMonitor:

  traces  -> a LogicMonitor OTel collector (lmotel), OTLP/HTTP JSON
  logs    -> LogicMonitor Logs ingest API (/rest/log/ingest)
  metrics -> LogicMonitor Push Metrics API (/rest/metric/ingest)

Standard library only. Settings come from the environment (see .env.example);
the sites, the host and the business scenarios come from DEMO_CONFIG_JSON or
CONFIG_FILE (see config/demo.example.json).
"""
import base64, hashlib, hmac, json, os, random, threading, time, urllib.request, urllib.error, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


def env_on(name, default="true"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


ACCOUNT = os.environ.get("LOGICMONITOR_ACCOUNT", "")
AID = os.environ.get("LOGICMONITOR_ACCESS_ID", "")
AKEY = os.environ.get("LOGICMONITOR_ACCESS_KEY", "")
LMOTEL = os.environ.get("LMOTEL_URL", "http://127.0.0.1:4318").rstrip("/")
PORT = int(os.environ.get("PORT", "8089"))
DURATION = int(os.environ.get("DEMO_DURATION_MIN", "10")) * 60
BASELINE_ON = env_on("DEMO_BASELINE")                         # calm Demo_* values every minute between scenarios
CREATE_RESOURCES = env_on("LM_CREATE_RESOURCES", "false")    # let Push Metrics create missing resources
PORTAL = f"https://{ACCOUNT}.logicmonitor.com"
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(HERE, "config", "demo.json"))


def load_config():
    """DEMO_CONFIG_JSON (the whole config as one line of JSON, usually from .env) wins over CONFIG_FILE."""
    inline = os.environ.get("DEMO_CONFIG_JSON", "").strip()
    if inline:
        return json.loads(inline), "DEMO_CONFIG_JSON"
    if not os.path.isfile(CONFIG_FILE):
        raise SystemExit(f"no config: set DEMO_CONFIG_JSON in .env, or copy config/demo.example.json to config/demo.json and edit it (or set CONFIG_FILE)")
    with open(CONFIG_FILE, encoding="utf-8") as fh:
        return json.load(fh), os.path.abspath(CONFIG_FILE)


CONFIG, CONFIG_PATH = load_config()
TITLE = CONFIG.get("title", "Demo")
HOST = CONFIG.get("host") or {}
HOST_RESOURCE = HOST.get("resource", "")                     # system.hostname of the shared host's resource
HOST_LABEL = HOST.get("label", HOST_RESOURCE)
IFACE = HOST.get("interface", "eth0")

# ----------------------------------------------------------------- the sites
SITES = []
for _s in CONFIG.get("sites") or []:
    SITES.append({"id": _s["id"], "label": _s.get("label", _s["id"]), "url": _s.get("url", "https://" + _s.get("label", _s["id"])),
                  "service": _s["service"], "rum": _s.get("rum_service", _s["service"] + "-rum"), "container": _s.get("resource", _s["service"]),
                  "ns": _s.get("namespace", "demo"), "routes": _s.get("routes") or ["/"]})
SITE = {s["id"]: s for s in SITES}
BUSINESS = {k: v for k, v in (CONFIG.get("business") or {}).items() if v and v.get("site") in SITE}
COUNTRIES = [("United States", "US", 0.62), ("Canada", "CA", 0.08), ("Germany", "DE", 0.07), ("United Kingdom", "GB", 0.07), ("France", "FR", 0.04), ("Spain", "ES", 0.03), ("Singapore", "SG", 0.03), ("China", "CN", 0.03), ("Brazil", "BR", 0.03)]
REFERRERS = ["(direct)", "google", "linkedin", "x.com", "newsletter", "reddit"]
AGENTS = ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) Safari/17.6", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6) Mobile Safari", "Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile", "Mozilla/5.0 (X11; Linux x86_64) Firefox/131"]

# --------------------------------------------------------------- LM clients
def _lmv1(method, path, data):
    epoch = str(int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(AKEY.encode(), (method + epoch + data + path).encode(), hashlib.sha256).hexdigest().encode()).decode()
    return f"LMv1 {AID}:{sig}:{epoch}"


def lm_post(path, body, query="", base="/rest"):
    data = json.dumps(body)
    req = urllib.request.Request(PORTAL + base + path + (("?" + query) if query else ""), data=data.encode(), method="POST")
    req.add_header("Authorization", _lmv1("POST", path, data))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Version", "3")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # network
        return 0, repr(e)


def lm_call(method, path, query="", body=None):
    data = json.dumps(body) if body is not None else ""
    req = urllib.request.Request(PORTAL + "/santaba/rest" + path + (("?" + query) if query else ""), data=data.encode() if data else None, method=method)
    req.add_header("Authorization", _lmv1(method, path, data))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Version", "3")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"errorMessage": repr(e)}


HEALTH = {}   # channel -> {"ok": n, "fail": n, "last": "status text"}, shown in the console


def track(channel, status, text=""):
    h = HEALTH.setdefault(channel, {"ok": 0, "fail": 0, "last": ""})
    if 200 <= status < 300:
        h["ok"] += 1
    else:
        h["fail"] += 1
        h["last"] = f"{time.strftime('%H:%M:%S')} HTTP {status} {text[:120]}"
    return status


def otlp(path, body):
    req = urllib.request.Request(LMOTEL + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": str(v)}}


def resource_for(service, container, ns, extra=None):
    # Same attributes the contrib collector's transform/lm_entity stamps, so
    # LM maps the spans onto the container resource and its APM service.
    a = {"service.name": service, "service.namespace": ns, "service.version": "demo", "host.name": container,
         "system.hostname": container, "system.displayname": container, "resource.type": "host", "docker.host": HOST_LABEL,
         "deployment.environment.name": "production", "telemetry.sdk.name": "lm-demo-console", "telemetry.sdk.language": "python"}
    a.update(extra or {})
    return {"attributes": [attr(k, v) for k, v in a.items()]}


def ns_now():
    return int(time.time() * 1e9)


def span(name, kind, start_ns, dur_ms, attrs, trace_id=None, parent=None, status_err=None, events=None):
    s = {"traceId": trace_id or uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "name": name, "kind": kind,
         "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(start_ns + int(dur_ms * 1e6)),
         "attributes": [attr(k, v) for k, v in attrs.items()], "status": {"code": 2, "message": status_err} if status_err else {"code": 1}}
    if parent:
        s["parentSpanId"] = parent
    if events:
        s["events"] = [{"timeUnixNano": str(start_ns + int(dur_ms * 0.8e6)), "name": e["name"], "attributes": [attr(k, v) for k, v in e.get("attrs", {}).items()]} for e in events]
    return s


def send_spans(resource, spans):
    if not spans:
        return 0
    return track("traces", otlp("/v1/traces", {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": {"name": "lm-demo-console"}, "spans": spans}]}]}))


def send_logs(records):
    """records: list of (resource-hostname, message, extra dict). LM Logs maps on system.hostname."""
    if not records:
        return 0
    now = int(time.time() * 1000)
    body = []
    for host, msg, extra in records:
        rec = {"message": msg, "timestamp": now, "_lm.resourceId": {"system.hostname": host}}
        rec.update(extra or {})
        body.append(rec)
    st, txt = lm_post("/log/ingest", body)
    return track("logs", st, txt)


def push_metrics(resource_host, ds, instance, values, ds_display=None, ds_group="Demo"):
    now = str(int(time.time()))
    body = {"resourceIds": {"system.hostname": resource_host}, "dataSource": ds, "dataSourceDisplayName": ds_display or ds.replace("_", " "), "dataSourceGroup": ds_group,
            "instances": [{"instanceName": instance, "instanceDisplayName": instance,
                           "dataPoints": [{"dataPointName": k, "dataPointType": "GAUGE", "dataPointAggregationType": "none", "values": {now: v}} for k, v in values.items()]}]}
    if CREATE_RESOURCES:
        body["resourceName"] = resource_host
    st, txt = lm_post("/metric/ingest", body, "create=true" if CREATE_RESOURCES else "")
    return track("metrics", st, txt), txt


# ------------------------------------------------------------- generators
class Scenario:
    def __init__(self, kind, name, params):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.name, self.params = kind, name, params
        self.started = time.time()
        self.duration = int(params.get("minutes", DURATION / 60)) * 60
        self.stop = False
        self.sent = {"spans": 0, "logs": 0, "metrics": 0, "errors": 0}
        self.log = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def progress(self):
        return min(1.0, (time.time() - self.started) / self.duration)

    def phase(self):
        """0..1 ramp in the first 15 %, plateau, recover in the last 20 %."""
        p = self.progress()
        if p < 0.15:
            return p / 0.15
        if p > 0.8:
            return max(0.0, (1 - p) / 0.2)
        return 1.0

    def note(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.log = self.log[-30:]

    def run(self):
        self.note(f"started {self.name} for {self.duration // 60} min")
        while not self.stop and time.time() - self.started < self.duration:
            try:
                self.tick()
            except Exception as e:
                self.sent["errors"] += 1
                self.note(f"tick error {e!r}")
            time.sleep(5)
        self.note("finished")
        self.stop = True

    def to_json(self):
        return {"id": self.id, "kind": self.kind, "name": self.name, "params": self.params, "started": self.started, "duration": self.duration,
                "progress": round(self.progress(), 3), "phase": round(self.phase(), 2), "running": not self.stop, "sent": self.sent, "log": self.log[-8:]}

    def tick(self):
        raise NotImplementedError


RUM_OVERLAY = {}   # rum service -> recent RUM events and vitals, served at /api/rum/<svc> and available to a RUM script datasource


def rum_add(svc, key, n=1):
    """key: pageViews | engagements | jsErrors | ctaClicks (counted), lcp | inp | cls (sampled), countries (n = country)."""
    now = time.time()
    d = RUM_OVERLAY.setdefault(svc, {"events": [], "lcp": [], "inp": [], "cls": [], "countries": {}, "updated": 0})
    if key in ("lcp", "inp", "cls"):
        d[key] = (d[key] + [(now, n)])[-300:]
        d["events"].append((now, "webVitals"))
    elif key == "countries":
        d["countries"][n] = now
    else:
        d["events"].append((now, key))
    if len(d["events"]) > 20000:
        d["events"] = [e for e in d["events"] if now - e[0] < 600]
    d["updated"] = now


def rum_summary(svc, window=300):
    """Per-minute rates over the last 5 minutes and p75 / good share of vitals over the last 15, like the datasource's own math."""
    d = RUM_OVERLAY.get(svc)
    now = time.time()
    if not d or now - d["updated"] > 900:
        return {"active": False}
    out = {"active": True, "updated": d["updated"]}
    for key in ("pageViews", "engagements", "jsErrors", "ctaClicks", "webVitals"):
        out[key + "PerMin"] = round(sum(1 for t, k in d["events"] if k == key and now - t < window) * 60.0 / window, 2)
    for vital, good in (("lcp", 2500), ("inp", 200), ("cls", 0.1)):
        v = sorted(x for t, x in d[vital] if now - t < 900)
        out[vital + "P75"] = round(v[int(len(v) * 0.75)], 3) if v else None
        out[vital + "GoodPct"] = round(100.0 * sum(1 for x in v if x < good) / len(v), 1) if v else None
    out["countries"] = sum(1 for t in d["countries"].values() if now - t < 86400)
    return out


class WebTraffic(Scenario):
    """Server spans + browser RUM spans + app logs for the selected sites."""
    def tick(self):
        mode = self.params["mode"]            # normal | peak | error
        ph = self.phase()
        mult = {"normal": 1.0, "peak": 6.0, "error": 1.5}[mode]
        err_rate = {"normal": 0.01, "peak": 0.06, "error": 0.35}[mode] * (ph if mode != "normal" else 1)
        lat_mult = {"normal": 1.0, "peak": 1 + 2.5 * ph, "error": 1 + 1.5 * ph}[mode]
        for sid in self.params["sites"]:
            s = SITE[sid]
            n = max(1, int(random.gauss(2 * mult, 0.6 * mult)))
            spans, rum_spans, logs = [], [], []
            for _ in range(n):
                route = random.choice(s["routes"])
                method = "POST" if route.startswith("/api") and random.random() < 0.6 else "GET"
                t0 = ns_now() - random.randint(0, 4000) * 1_000_000
                base = {"/": 25, "/shop": 60, "/checkout": 180, "/api/checkout": 320, "/api/vectorize": 900, "/api/jobs": 70}.get(route, 45)
                dur = max(2, random.gauss(base, base * 0.3)) * lat_mult
                err = random.random() < err_rate
                code = random.choice([500, 502, 503, 504]) if err else (404 if random.random() < 0.02 else 200)
                tid = uuid.uuid4().hex
                country = random.choices(COUNTRIES, weights=[c[2] for c in COUNTRIES])[0]
                a = {"http.request.method": method, "http.response.status_code": code, "http.route": route, "url.path": route, "url.scheme": "https",
                     "server.address": s["label"], "client.address": f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
                     "user_agent.original": random.choice(AGENTS), "geo.country.iso_code": country[1], "network.protocol.version": "1.1", "demo.scenario": self.name}
                ev = None
                if err:
                    ev = [{"name": "exception", "attrs": {"exception.type": "UpstreamError", "exception.message": random.choice(["upstream timed out after 30s", "ECONNRESET from origin", "database pool exhausted", "worker crashed (SIGKILL)"])}}]
                root = span(f"{method} {route}", 2, t0, dur, a, tid, status_err=f"HTTP {code}" if code >= 500 else None, events=ev)
                spans.append(root)
                # a child DB or downstream span for realism
                if route.startswith("/api") or route in ("/shop", "/checkout", "/episodes"):
                    child = span("SELECT " + random.choice(["products", "orders", "sessions", "episodes", "jobs"]), 3, t0 + 2_000_000, dur * 0.5, {"db.system.name": "postgresql", "db.namespace": s["id"], "server.address": "postgres"}, tid, parent=root["spanId"])
                    spans.append(child)
                # browser side
                if method == "GET" and not route.startswith("/api"):
                    rum_spans.append(span("page_view", 3, t0, dur * 8 + 300, {"rum.route": route, "rum.referrer": random.choice(REFERRERS), "rum.country": country[0], "rum.ttfb_ms": int(dur), "http.url": s["url"] + route, "rum.session": uuid.uuid4().hex[:12], "demo.scenario": self.name}, tid))
                    rum_add(s["rum"], "pageViews"); rum_add(s["rum"], "countries", country[0])
                    lcp = max(400, random.gauss(1800 * lat_mult, 500)); inp = max(30, random.gauss(140 * lat_mult, 60)); cls = max(0.0, random.gauss(0.06, 0.04))
                    for vital, val, rating in (("LCP", lcp, "good" if lcp < 2500 else ("needs-improvement" if lcp < 4000 else "poor")), ("INP", inp, "good" if inp < 200 else ("needs-improvement" if inp < 500 else "poor")), ("CLS", cls, "good" if cls < 0.1 else ("needs-improvement" if cls < 0.25 else "poor"))):
                        rum_spans.append(span("rum.web_vital", 3, t0, val if vital != "CLS" else val * 1000, {"rum.vital.name": vital, "rum.vital.value": round(val, 3), "rum.vital.rating": rating, "rum.route": route, "rum.country": country[0], "demo.scenario": self.name}, tid))
                        rum_add(s["rum"], vital.lower(), val)
                    if random.random() < 0.4:
                        rum_spans.append(span("page_engagement", 3, t0, random.uniform(3000, 40000), {"rum.route": route, "rum.scroll_depth": random.choice([25, 50, 75, 100]), "rum.country": country[0]}, tid)); rum_add(s["rum"], "engagements")
                    if err and random.random() < 0.5:
                        rum_spans.append(span("rum.js_error", 3, t0, 1, {"rum.error.message": "TypeError: Cannot read properties of undefined (reading 'data')", "rum.route": route}, tid, status_err="js error")); rum_add(s["rum"], "jsErrors")
                lvl = "error" if err else ("warn" if code == 404 else "info")
                logs.append((s["container"], json.dumps({"level": 50 if err else 30, "msg": "request completed" if not err else "request failed: " + ev[0]["attrs"]["exception.message"], "req": {"method": method, "url": route}, "res": {"statusCode": code}, "responseTime": round(dur, 1), "trace_id": tid, "span_id": root["spanId"], "demo": self.name}), {"severity": lvl, "service.name": s["service"], "trace_id": tid, "span_id": root["spanId"]}))
            send_spans(resource_for(s["service"], s["container"], s["ns"]), spans)
            send_spans(resource_for(s["rum"], s["container"], s["ns"], {"telemetry.sdk.language": "webjs"}), rum_spans)
            send_logs(logs)
            self.sent["spans"] += len(spans) + len(rum_spans); self.sent["logs"] += len(logs)
        if int(self.progress() * 100) % 25 == 0:
            self.note(f"{mode}: phase {ph:.2f}, error rate {err_rate:.2f}")


class Infra(Scenario):
    """Synthetic host trouble on the shared host with the knock-on effects in services and logs."""
    def tick(self):
        kind = self.params["case"]
        ph = self.phase()
        logs = []
        if not HOST_RESOURCE:
            self.note("no host configured in the config file"); self.stop = True; return
        if kind == "memory":
            vals = {"MemoryAvailablePct": round(42 - 38 * ph + random.uniform(-1, 1), 1), "SwapUsedPct": round(5 + 88 * ph + random.uniform(-2, 2), 1),
                    "SwapInPerSec": round(random.gauss(900, 150) * ph, 0), "SwapOutPerSec": round(random.gauss(1200, 200) * ph, 0), "OOMKills": int(ph > 0.9 and random.random() < 0.5), "PageFaultsPerSec": round(1500 + 60000 * ph, 0)}
            push_metrics(HOST_RESOURCE, "Demo_Memory", HOST_LABEL, vals, "Demo: Memory pressure")
            if ph > 0.3 and random.random() < 0.7:
                logs.append((HOST_RESOURCE, f"kernel: [{int(time.time())}.{random.randint(100000,999999)}] kswapd0: high swap activity, {int(vals['SwapInPerSec'])} pages/s in, {int(vals['SwapOutPerSec'])} pages/s out", {"severity": "warn", "app": "kernel"}))
            if vals["OOMKills"]:
                victim = random.choice(["postgres", "node", "java", "python3"])
                logs.append((HOST_RESOURCE, f"kernel: Out of memory: Killed process {random.randint(1000,99999)} ({victim}) total-vm:{random.randint(2,9)}GB, anon-rss:{random.randint(1,4)}GB", {"severity": "critical", "app": "kernel"}))
            self.knock_on(ph, "database pool exhausted (postgres restarted after OOM)" if vals["OOMKills"] else "slow response: host swapping", slow=4 + 6 * ph)
        elif kind == "network":
            vals = {"PacketLossPct": round(0.1 + 18 * ph + random.uniform(-0.5, 0.5), 2), "LatencyMs": round(0.4 + 140 * ph + random.uniform(-5, 5), 1), "TcpRetransmitsPerSec": round(random.gauss(2, 1) + 400 * ph, 0),
                    "InterfaceErrorsPerSec": round(90 * ph * random.random(), 0), "DnsFailuresPerMin": round(30 * ph * random.random(), 0)}
            push_metrics(HOST_RESOURCE, "Demo_Network", IFACE, vals, "Demo: Network health")
            if ph > 0.3 and random.random() < 0.6:
                logs.append((HOST_RESOURCE, f"kernel: {IFACE}: NETDEV WATCHDOG: transmit queue timed out, link flapped ({int(vals['InterfaceErrorsPerSec'])} errors/s, {vals['PacketLossPct']}% loss)", {"severity": "error", "app": "kernel"}))
            if ph > 0.5 and random.random() < 0.4:
                logs.append((HOST_RESOURCE, "edge-proxy: failed to dial origin: dial tcp 172.27.0.19:80: i/o timeout", {"severity": "error", "app": "edge-proxy"}))
            self.knock_on(ph, "upstream connect error: connection reset by peer", slow=2 + 3 * ph, errors=0.3 * ph)
        elif kind == "cpu":
            vals = {"LoadAverage1m": round(3 + 58 * ph + random.uniform(-1, 1), 1), "ContextSwitchesPerSec": round(20000 + 900000 * ph, 0), "ZombieProcesses": int(400 * ph * random.uniform(0.8, 1.2)),
                    "ThreadCount": int(2200 + 30000 * ph), "RunQueueLength": int(2 + 70 * ph), "CpuStealPct": round(20 * ph * random.random(), 1)}
            push_metrics(HOST_RESOURCE, "Demo_CPU", HOST_LABEL, vals, "Demo: CPU and scheduler")
            if ph > 0.3 and random.random() < 0.7:
                logs.append((HOST_RESOURCE, f"systemd[1]: app-worker.service: spawned {random.randint(50,400)} defunct child processes; reaping failed (zombies={vals['ZombieProcesses']})", {"severity": "warn", "app": "systemd"}))
            if ph > 0.6 and random.random() < 0.5:
                logs.append((HOST_RESOURCE, f"kernel: sched: RT throttling activated; run queue {vals['RunQueueLength']}, ctxsw {int(vals['ContextSwitchesPerSec'])}/s", {"severity": "error", "app": "kernel"}))
            self.knock_on(ph, "request timed out waiting for worker thread (event loop blocked)", slow=3 + 8 * ph, errors=0.15 * ph)
        send_logs(logs); self.sent["metrics"] += 1; self.sent["logs"] += len(logs)
        if random.random() < 0.2:
            self.note(f"{kind}: phase {ph:.2f}")

    def knock_on(self, ph, reason, slow=3.0, errors=0.0):
        """Every site gets slower; some requests fail, with logs that carry the trace id."""
        logs = []
        for s in SITES:
            spans = []
            for _ in range(random.randint(1, 3)):
                route = random.choice(s["routes"]); t0 = ns_now() - random.randint(0, 4000) * 1_000_000
                dur = max(5, random.gauss(60, 20)) * (1 + slow * ph)
                err = random.random() < errors
                tid = uuid.uuid4().hex
                root = span(f"GET {route}", 2, t0, dur, {"http.request.method": "GET", "http.response.status_code": 503 if err else 200, "http.route": route, "url.path": route, "server.address": s["label"], "demo.scenario": self.name}, tid,
                            status_err="HTTP 503" if err else None, events=[{"name": "exception", "attrs": {"exception.type": "UpstreamError", "exception.message": reason}}] if err else None)
                spans.append(root)
                spans.append(span("SELECT products", 3, t0 + 1_000_000, dur * 0.7, {"db.system.name": "postgresql", "server.address": "postgres", "demo.scenario": self.name}, tid, parent=root["spanId"]))
                if err or random.random() < 0.3 * ph:
                    logs.append((s["container"], json.dumps({"level": 50 if err else 40, "msg": reason, "req": {"method": "GET", "url": route}, "res": {"statusCode": 503 if err else 200}, "responseTime": round(dur, 1), "trace_id": tid, "span_id": root["spanId"], "demo": self.name}), {"severity": "error" if err else "warn", "service.name": s["service"], "trace_id": tid}))
            send_spans(resource_for(s["service"], s["container"], s["ns"]), spans); self.sent["spans"] += len(spans)
        send_logs(logs); self.sent["logs"] += len(logs)


class Business(Scenario):
    """Business failures with calm infrastructure."""
    def tick(self):
        kind = self.params["case"]; ph = self.phase(); logs = []
        b = BUSINESS.get(kind)
        if not b:
            self.note(f"{kind}: not configured under business in the config"); self.stop = True; return
        s = SITE[b["site"]]
        if kind == "checkout":
            route = b.get("route", "/api/checkout"); gw = b.get("gateway", "api.stripe.com"); spans = []
            attempts = random.randint(2, 5)
            failed = sum(1 for _ in range(attempts) if random.random() < 0.9 * ph)
            for i in range(attempts):
                t0 = ns_now() - random.randint(0, 4000) * 1_000_000; tid = uuid.uuid4().hex; fail = i < failed
                root = span(f"POST {route}", 2, t0, random.gauss(30000 if fail else 420, 60), {"http.request.method": "POST", "http.response.status_code": 502 if fail else 200, "http.route": route, "url.path": route, "server.address": s["label"], "checkout.cart_value_usd": round(random.uniform(45, 320), 2), "demo.scenario": self.name}, tid,
                            status_err="HTTP 502" if fail else None, events=[{"name": "exception", "attrs": {"exception.type": "PaymentGatewayTimeout", "exception.message": f"{gw}: POST /v1/payment_intents timed out after 30000 ms"}}] if fail else None)
                spans.append(root)
                spans.append(span(f"POST {gw}/v1/payment_intents", 3, t0 + 5_000_000, 30000 if fail else 380, {"http.request.method": "POST", "server.address": gw, "http.response.status_code": 0 if fail else 200, "peer.service": "payment-gateway", "demo.scenario": self.name}, tid, parent=root["spanId"], status_err="timeout" if fail else None))
                logs.append((s["container"], json.dumps({"level": 50 if fail else 30, "msg": "checkout failed: payment gateway timeout" if fail else "checkout completed", "req": {"method": "POST", "url": route}, "res": {"statusCode": 502 if fail else 200}, "order_id": f"{b.get('order_prefix', 'ORD')}-{random.randint(10000,99999)}", "trace_id": tid, "span_id": root["spanId"], "demo": self.name}), {"severity": "error" if fail else "info", "service.name": s["service"], "trace_id": tid}))
                if fail:
                    rum_add(s["rum"], "jsErrors")
            send_spans(resource_for(s["service"], s["container"], s["ns"]), spans)
            ok = attempts - failed
            push_metrics(s["container"], "Demo_Business", s["label"], {"CheckoutAttemptsPerMin": attempts * 12, "CheckoutFailuresPerMin": failed * 12, "OrdersPerMin": ok * 12, "RevenuePerMinUsd": round(ok * 12 * random.uniform(60, 140), 2), "ConversionPct": round(100.0 * ok / attempts, 1), "CartAbandonmentPct": round(22 + 70 * ph, 1)}, "Demo: Business KPIs")
            self.sent["spans"] += len(spans)
        elif kind == "cta":
            cta = b.get("cta", "footer:github"); target = b.get("target", "https://github.com/"); spans = []
            for _ in range(random.randint(2, 6)):
                t0 = ns_now() - random.randint(0, 4000) * 1_000_000; tid = uuid.uuid4().hex
                spans.append(span(f"click {cta}", 3, t0, random.gauss(420, 80), {"rum.route": random.choice(s["routes"]), "rum.cta": cta, "http.url": target, "http.response.status_code": 404, "rum.country": random.choice(COUNTRIES)[0], "demo.scenario": self.name}, tid, status_err="HTTP 404"))
                spans.append(span("rum.js_error", 3, t0 + 500_000_000, 1, {"rum.error.message": f"Outbound link returned 404: {target}", "rum.route": "/", "rum.cta": cta}, tid, status_err="link broken"))
                rum_add(s["rum"], "ctaClicks"); rum_add(s["rum"], "jsErrors")
                logs.append((s["container"], json.dumps({"level": 40, "msg": "outbound CTA failed", "cta": cta, "target": target, "status": 404, "trace_id": tid, "demo": self.name}), {"severity": "warn", "service.name": s["rum"], "trace_id": tid}))
            send_spans(resource_for(s["rum"], s["container"], s["ns"], {"telemetry.sdk.language": "webjs"}), spans)
            push_metrics(s["container"], "Demo_Business", s["label"], {"CtaClicksPerMin": len(spans) // 2 * 12, "CtaFailuresPerMin": len(spans) // 2 * 12, "CtaSuccessPct": 0.0, "GithubReferralsPerMin": 0, "NewsletterSignupsPerMin": random.randint(0, 2)}, "Demo: Business KPIs")
            self.sent["spans"] += len(spans)
        elif kind == "signup":
            route = b.get("route", "/api/signup"); smtp = b.get("smtp", "smtp.example.com:587"); smtp_host = smtp.split(":")[0]; spans = []
            submits = random.randint(2, 5)
            for _ in range(submits):
                t0 = ns_now() - random.randint(0, 4000) * 1_000_000; tid = uuid.uuid4().hex
                root = span(f"POST {route}", 2, t0, random.gauss(180, 40), {"http.request.method": "POST", "http.response.status_code": 200, "http.route": route, "url.path": route, "server.address": s["label"], "demo.scenario": self.name}, tid)
                spans.append(root)
                spans.append(span(f"SMTP connect {smtp}", 3, t0 + 20_000_000, 120, {"server.address": smtp_host, "server.port": int(smtp.split(":")[1]) if ":" in smtp else 25, "peer.service": "smtp", "demo.scenario": self.name}, tid, parent=root["spanId"], status_err="ECONNREFUSED" if ph > 0.1 else None))
                logs.append((s["container"], json.dumps({"level": 40 if ph > 0.1 else 30, "msg": f"signup stored; confirmation email NOT sent: connect ECONNREFUSED {smtp}" if ph > 0.1 else "signup stored, confirmation sent", "req": {"method": "POST", "url": route}, "res": {"statusCode": 200}, "trace_id": tid, "span_id": root["spanId"], "demo": self.name}), {"severity": "warn" if ph > 0.1 else "info", "service.name": s["service"], "trace_id": tid}))
            send_spans(resource_for(s["service"], s["container"], s["ns"]), spans)
            confirmed = 0 if ph > 0.1 else submits
            push_metrics(s["container"], "Demo_Business", s["label"], {"WaitlistSubmitsPerMin": submits * 12, "WaitlistConfirmedPerMin": confirmed * 12, "ConfirmationRatePct": round(100.0 * confirmed / submits, 1), "LeadsLostPerMin": (submits - confirmed) * 12, "EmailQueueDepth": int(300 * ph)}, "Demo: Business KPIs")
            self.sent["spans"] += len(spans)
        send_logs(logs); self.sent["logs"] += len(logs); self.sent["metrics"] += 1


SCENARIOS = {}
LOCK = threading.Lock()


def start(kind, name, params):
    sc = {"web": WebTraffic, "infra": Infra, "business": Business}[kind](kind, name, params)
    with LOCK:
        # one web traffic generator at a time; infra/business cases may overlap
        for s in list(SCENARIOS.values()):
            if s.kind == kind and s.kind == "web" and not s.stop:
                s.stop = True
        SCENARIOS[sc.id] = sc
    sc.thread.start()
    return sc


# ------------------------------------------------------------- LM setup: thresholds on the demo datasources
THRESHOLDS = {
    "Demo_Memory": {"SwapUsedPct": "> 60 80 90", "MemoryAvailablePct": "< 15 10 5", "OOMKills": "> 0 0 0"},
    "Demo_Network": {"PacketLossPct": "> 2 5 10", "LatencyMs": "> 50 100 150", "TcpRetransmitsPerSec": "> 100 250 400"},
    "Demo_CPU": {"LoadAverage1m": "> 20 40 60", "ZombieProcesses": "> 50 150 300", "RunQueueLength": "> 20 40 60"},
    "Demo_Business": {"ConversionPct": "< 80 50 20", "CheckoutFailuresPerMin": "> 5 20 40", "CtaSuccessPct": "< 90 50 10", "ConfirmationRatePct": "< 90 50 10"},
}


SEED_HOST = [("Demo_Memory", HOST_LABEL, {"MemoryAvailablePct": 42, "SwapUsedPct": 5, "SwapInPerSec": 0, "SwapOutPerSec": 0, "OOMKills": 0, "PageFaultsPerSec": 1500}, "Demo: Memory pressure"),
             ("Demo_Network", IFACE, {"PacketLossPct": 0.1, "LatencyMs": 0.4, "TcpRetransmitsPerSec": 2, "InterfaceErrorsPerSec": 0, "DnsFailuresPerMin": 0}, "Demo: Network health"),
             ("Demo_CPU", HOST_LABEL, {"LoadAverage1m": 3, "ContextSwitchesPerSec": 20000, "ZombieProcesses": 2, "ThreadCount": 2200, "RunQueueLength": 2, "CpuStealPct": 0}, "Demo: CPU and scheduler")]
SEED_BUSINESS = {"checkout": {"CheckoutAttemptsPerMin": 36, "CheckoutFailuresPerMin": 1, "OrdersPerMin": 35, "RevenuePerMinUsd": 3100, "ConversionPct": 97.2, "CartAbandonmentPct": 22},
                 "cta": {"CtaClicksPerMin": 12, "CtaFailuresPerMin": 0, "CtaSuccessPct": 100, "GithubReferralsPerMin": 12, "NewsletterSignupsPerMin": 2},
                 "signup": {"WaitlistSubmitsPerMin": 24, "WaitlistConfirmedPerMin": 24, "ConfirmationRatePct": 100, "LeadsLostPerMin": 0, "EmailQueueDepth": 0}}
# (resource, datasource, instance, values, display name): one calm row per demo datasource
BASELINE = [(HOST_RESOURCE, ds, inst, vals, disp) for ds, inst, vals, disp in SEED_HOST if HOST_RESOURCE] + \
           [(SITE[b["site"]]["container"], "Demo_Business", SITE[b["site"]]["label"], SEED_BUSINESS[k], "Demo: Business KPIs") for k, b in BUSINESS.items() if k in SEED_BUSINESS]
# which (datasource, instance) a running scenario case owns; the baseline stays quiet for those
OWNS = {"memory": ("Demo_Memory", HOST_LABEL), "network": ("Demo_Network", IFACE), "cpu": ("Demo_CPU", HOST_LABEL)}
OWNS.update({k: ("Demo_Business", SITE[b["site"]]["label"]) for k, b in BUSINESS.items()})


def baseline():
    """Push calm values every minute for every demo datasource no scenario is driving, so dashboards never go empty."""
    while BASELINE_ON:
        try:
            with LOCK:
                busy = {OWNS.get(sc.params.get("case")) for sc in SCENARIOS.values() if not sc.stop}
            for host, ds, inst, vals, disp in BASELINE:
                if (ds, inst) in busy:
                    continue
                jit = {k: v if "Pct" in k or not v else round(v * random.uniform(0.95, 1.05), 1) for k, v in vals.items()}
                push_metrics(host, ds, inst, jit, disp)
        except Exception as e:
            print(f"baseline error {e!r}", flush=True)
        time.sleep(60)


def setup():
    """Seed one datapoint set per demo datasource (so LM creates them), then set alert thresholds."""
    out = []
    for host, ds, inst, vals, disp in BASELINE:
        st, txt = push_metrics(host, ds, inst, vals, disp)
        out.append(f"seed {ds}/{inst} on {host}: {st}")
    # thresholds (the datasource may take a few minutes to exist after the first push)
    for ds, dps in THRESHOLDS.items():
        st, j = lm_call("GET", "/setting/datasources", "size=5&fields=id,name,dataPoints&filter=name:\"" + ds + "\"")
        items = j.get("items") or []
        if not items:
            out.append(f"thresholds {ds}: datasource not created yet, run setup again in a few minutes"); continue
        d = items[0]
        for p in d["dataPoints"]:
            if p["name"] in dps and (p.get("alertExpr") != dps[p["name"]] or p.get("alertTransitionInterval") != 1):
                # alert on the first sample over the line (default is 5 consecutive polls, too slow for a live demo)
                p["alertExpr"] = dps[p["name"]]; p["alertTransitionInterval"] = 1; p["alertClearTransitionInterval"] = 1; p["alertSubject"] = f"{p['name']} on ##HOST## is ##VALUE##"; p["alertBody"] = f"Demo datasource {ds}: {p['name']} is ##VALUE## on ##HOST## (threshold {dps[p['name']]})."
        st, r = lm_call("PATCH", f"/setting/datasources/{d['id']}", "patchFields=dataPoints", {"dataPoints": d["dataPoints"]})
        out.append(f"thresholds {ds}: {st} {(r.get('errorMessage') or '')[:120]}")
    return out


# ------------------------------------------------------------------ HTTP
DASHBOARD_NAMES = {k: v for k, v in (CONFIG.get("dashboards") or {}).items() if v}
DASHBOARDS = {"traces": f"{PORTAL}/santaba/uiv4/traces", "logs": f"{PORTAL}/santaba/uiv4/logs", "alerts": f"{PORTAL}/santaba/uiv4/alerts", "resources": f"{PORTAL}/santaba/uiv4/resources"}
_DASH_AT = 0


def dashboards():
    """Dashboard links by name (the generators rebuild dashboards, which changes their ids); refreshed every 5 minutes."""
    global _DASH_AT
    if time.time() - _DASH_AT > 300:
        st, j = lm_call("GET", "/dashboard/dashboards", "size=300&fields=id,name")
        if st == 200:
            ids = {i["name"]: i["id"] for i in j.get("items") or []}
            for key, name in DASHBOARD_NAMES.items():
                if name in ids:
                    DASHBOARDS[key] = f"{PORTAL}/santaba/uiv4/dashboards/dashboards-{ids[name]}"
            _DASH_AT = time.time()
        else:
            _DASH_AT = time.time() - 240   # retry in a minute
    return DASHBOARDS


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            name = os.path.basename(u.path)
            ctype = {"css": "text/css", "js": "application/javascript", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
            return self._file(name, ctype)
        if u.path == "/api/status":
            with LOCK:
                scs = [s.to_json() for s in sorted(SCENARIOS.values(), key=lambda s: -s.started)][:12]
            return self._json(200, {"scenarios": scs, "sites": [{"id": s["id"], "label": s["label"], "container": s["container"]} for s in SITES], "dashboards": dashboards(), "dashboardNames": DASHBOARD_NAMES,
                                   "title": TITLE, "host": {"label": HOST_LABEL, "resource": HOST_RESOURCE, "interface": IFACE},
                                   "business": {k: dict(b, label=SITE[b["site"]]["label"]) for k, b in BUSINESS.items()}, "baseline": BASELINE_ON, "portal": PORTAL, "lmotel": LMOTEL, "defaultMinutes": DURATION // 60, "health": HEALTH})
        if u.path.startswith("/api/rum/"):
            return self._json(200, rum_summary(u.path.rsplit("/", 1)[-1]))
        if u.path == "/api/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if u.path == "/api/start":
            kind, name = body.get("kind"), body.get("name", "")
            params = body.get("params") or {}
            if kind == "web":
                params.setdefault("sites", [s["id"] for s in SITES]); params.setdefault("mode", "normal")
                name = name or f"web traffic: {params['mode']}"
            if kind not in ("web", "infra", "business"):
                return self._json(400, {"error": "bad kind"})
            sc = start(kind, name, params)
            return self._json(200, sc.to_json())
        if u.path == "/api/stop":
            with LOCK:
                for s in SCENARIOS.values():
                    if body.get("id") in (None, "", s.id):
                        s.stop = True
            return self._json(200, {"ok": True})
        if u.path == "/api/setup":
            return self._json(200, {"log": setup()})
        if u.path == "/api/rum/reset":
            RUM_OVERLAY.clear(); return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def _file(self, name, ctype):
        p = os.path.join(STATIC, name)
        if not os.path.isfile(p):
            return self._json(404, {"error": "no file"})
        with open(p, "rb") as f:
            b = f.read()
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-cache"); self.end_headers(); self.wfile.write(b)


def reaper():
    while True:
        time.sleep(60)
        with LOCK:
            for k in [k for k, s in SCENARIOS.items() if s.stop and time.time() - s.started > 3 * 3600]:
                del SCENARIOS[k]


if __name__ == "__main__":
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=baseline, daemon=True).start()
    if not (ACCOUNT and AID and AKEY):
        print("warning: LOGICMONITOR_ACCOUNT, LOGICMONITOR_ACCESS_ID and LOGICMONITOR_ACCESS_KEY must be set; logs, metrics and setup will fail", flush=True)
    print(f"lm-demo console on :{PORT}, config {CONFIG_PATH}, {len(SITES)} sites, traces -> {LMOTEL}, portal {PORTAL}, baseline {'on' if BASELINE_ON else 'off'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
