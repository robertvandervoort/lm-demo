# LogicMonitor demo console

A small web console that turns a calm LogicMonitor portal into a live incident demo. Each button simulates a realistic problem and sends the matching **traces, logs and metrics** to LogicMonitor. The dashboards, alerts, traces and logs then tell the story by themselves:

| Use case | Audience | What the buttons do |
| --- | --- | --- |
| 1. Website traffic | SRE, app teams | Normal, Peak (6x surge) or Error (5xx with exceptions) traffic on every site: server spans, browser RUM (page views, Core Web Vitals, JS errors) and request logs that carry the trace id |
| 2. Infrastructure | Ops, SRE | Memory pressure ending in an OOM kill, network degradation, or CPU saturation on the shared host. Every site slows down with it, and kernel logs explain why |
| 3. Business impact | Executives, marketing | Checkout fails at the payment gateway, a call-to-action link returns 404, or signup confirmation emails silently stop. Infrastructure stays green while revenue, conversion or leads fall |

Everything is synthetic and goes straight to LogicMonitor. Nothing passes through your own OpenTelemetry collectors, so production telemetry and other backends (Grafana, Datadog and so on) never see it.

It's one Python file using only the standard library, runs in one small container, and needs no database.

## How it works

```
 browser ──> demo console (:8089) ──┬── traces  ──> LogicMonitor OTel collector (lmotel) ──> portal
                                    ├── logs    ──> LM Logs ingest API   (/rest/log/ingest)
                                    └── metrics ──> LM Push Metrics API  (/rest/metric/ingest)
```

- **Traces** are OTLP/HTTP JSON sent to `LMOTEL_URL`. Each span carries `host.name` and `system.hostname` set to the site's `resource`, so LogicMonitor attaches it to that resource and its APM service.
- **Logs** are matched to a resource by `system.hostname` and carry the same `trace_id` as their span.
- **Metrics** are pushed as four datasources the console creates on first use: `Demo_Memory`, `Demo_Network` and `Demo_CPU` on the host resource, and `Demo_Business` on each business site's resource. **Set up LM datasources** adds alert thresholds that fire on the first sample over the line.
- **Baseline:** between scenarios the console sends calm values for those datasources every minute (`DEMO_BASELINE=true`), so dashboards never show "no data". A running scenario takes over its own datasource, and the baseline comes back when it ends.

## Before you start

1. **A LogicMonitor portal** and an **LMv1 API token** with these permissions: manage DataSources (for the alert thresholds), view Dashboards, Alerts and Resources, and ingest Logs and Push Metrics.
2. **A LogicMonitor OTel collector (lmotel)** the console can reach over OTLP/HTTP, usually on port 4318. Without it you still get logs and metrics, but no traces.
3. **Resources that already exist** in LogicMonitor, one per site plus one for the host, each with a `system.hostname` property equal to the `resource` value in your config. These are normally created by a LogicMonitor collector or your OTel pipeline. LogicMonitor never creates a resource from spans, so if one is missing its traces and logs have nowhere to land. `LM_CREATE_RESOURCES=true` lets Push Metrics create missing resources, but that path hasn't been tested yet, so check the result with `lm_check.py`.
4. **Docker** with the compose plugin.

## Quick start

```bash
cp .env.example .env      # portal, token, lmotel address, port, and your sites (see below)
docker compose up -d --build
```

Open `http://<BIND_ADDR>:<CONSOLE_PORT>/` and then:

1. Click **Set up LM datasources**. Run it again after a few minutes: the first run creates the datasources, and thresholds can only be set once they exist. Only run it while nothing is going, because it pushes calm values.
2. Set Minutes high (for example 240) and click **Normal**. That gives every dashboard a calm baseline of traffic.
3. Start an incident about 4 minutes before you want to show it (see [Timing](#timing)).

The console has no login. Keep `BIND_ADDR` on localhost or a private LAN address, and never publish it to the internet.

## .env reference

Docker compose reads `.env` from this folder. It holds everything specific to your deployment and is ignored by git, so nothing about your environment ends up in the repo. Missing required values stop `docker compose up` with a message naming the variable.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOGICMONITOR_ACCOUNT` | required | Portal name, the part before `.logicmonitor.com` |
| `LOGICMONITOR_ACCESS_ID`, `LOGICMONITOR_ACCESS_KEY` | required | LMv1 token |
| `LM_ENV_FILE` | `.env` | Read the two credentials from another file instead, for example a root-only `/etc/lm-demo/credentials.env` |
| `LMOTEL_URL` | required | lmotel OTLP/HTTP base URL, for example `http://192.168.1.10:4318`. Use the host's address, not `localhost` |
| `BIND_ADDR` | `127.0.0.1` | Address the page is published on |
| `CONSOLE_PORT` | `8089` | Port the page is published on |
| `CONTAINER_NAME` | `lm-demo` | Docker container name |
| `DEMO_CONFIG_JSON` | unset | Your sites, host and business scenarios as one line of JSON in single quotes (see below). Wins over `config/demo.json` |
| `CONFIG_DIR` | `./config` | Folder that holds `demo.json` when `DEMO_CONFIG_JSON` isn't set, mounted read-only |
| `DEMO_DURATION_MIN` | `10` | Default scenario length; the page's Minutes box overrides it |
| `DEMO_BASELINE` | `true` | Send calm `Demo_*` values every minute between scenarios |
| `LM_CREATE_RESOURCES` | `false` | Let Push Metrics create missing resources (untested, see above) |

## Site config reference

The config says which sites exist, which host they share, and which business scenarios to offer. Put it in one of two places, both ignored by git:

- **In `.env`** as `DEMO_CONFIG_JSON='{...}'`, on one line in single quotes. This keeps every deployment detail in one file. To turn a pretty-printed file into that line, run `python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1])), separators=(',',':')))" my-config.json`.
- **In `config/demo.json`**, copied from `config/demo.example.json`.

`config/demo.example.json` is a complete example for a fictional company with three sites. The console refuses to start without a config, so it never sends data to the example's resources by accident.

```jsonc
{
  "title": "Acme",                      // shown in the page header
  "host": {                             // the shared host for use case 2; leave out to hide that section
    "resource": "10.0.0.5",             // system.hostname of the host's LM resource
    "label": "web-01",                  // instance name of Demo_Memory / Demo_CPU, shown on the page
    "interface": "eth0"                 // instance name of Demo_Network
  },
  "sites": [{
    "id": "shop",                       // internal key, referenced from "business"
    "label": "shop.acme.com",           // shown on the page; also the Demo_Business instance name
    "url": "https://shop.acme.com",     // used in browser RUM spans
    "service": "shop-web",              // service.name of the server spans (the APM service)
    "rum_service": "shop-web-rum",      // service.name of the browser spans
    "resource": "shop-web",             // system.hostname of the LM resource the site's data lands on
    "namespace": "acme",                // service.namespace (LM APM namespace)
    "routes": ["/", "/cart", "/checkout", "/api/checkout"]
  }],
  "business": {                         // each scenario is optional; leave one out to hide its button
    "checkout": {"site": "shop", "name": "Acme Shop", "route": "/api/checkout", "gateway": "api.stripe.com", "order_prefix": "AC"},
    "cta":      {"site": "shop", "name": "Acme Shop", "cta": "footer:github", "target": "https://github.com/acme"},
    "signup":   {"site": "shop", "name": "Acme Shop", "route": "/api/signup", "smtp": "smtp.sendgrid.net:587"}
  },
  "dashboards": {                       // optional: dashboard names the page links to as "Start here"
    "web": "SRE - Services (APM)", "infra": "SRE - Infrastructure RCA", "business": "Executive - Business Impact",
    "marketing": "Marketing - Web Traffic & RUM", "exec": "Executive - Digital Experience"
  }
}
```

The page looks dashboards up by name every 5 minutes, so rebuilding a dashboard (which changes its id) doesn't break the links. A name that isn't found simply hides its button. The Traces, Logs and Alerts links always work.

## What lands in LogicMonitor

| Datasource | Resource / instance | Datapoints with demo alert thresholds |
| --- | --- | --- |
| `Demo_Memory` | host / `host.label` | `SwapUsedPct > 60 80 90`, `MemoryAvailablePct < 15 10 5`, `OOMKills > 0` |
| `Demo_Network` | host / `host.interface` | `PacketLossPct > 2 5 10`, `LatencyMs > 50 100 150`, `TcpRetransmitsPerSec > 100 250 400` |
| `Demo_CPU` | host / `host.label` | `LoadAverage1m > 20 40 60`, `ZombieProcesses > 50 150 300`, `RunQueueLength > 20 40 60` |
| `Demo_Business` | each business site / its `label` | `ConversionPct < 80 50 20`, `CheckoutFailuresPerMin > 5 20 40`, `CtaSuccessPct < 90 50 10`, `ConfirmationRatePct < 90 50 10` |

`Demo_Business` also carries `RevenuePerMinUsd`, `OrdersPerMin`, `CartAbandonmentPct`, `CtaClicksPerMin`, `WaitlistSubmitsPerMin`, `LeadsLostPerMin` and a few others for dashboards. The calm checkout baseline is 35 orders a minute and about $3,100 a minute in revenue. If you quote business impact numbers in a presentation, use those so they match the dashboards.

Useful queries during a demo:

- **Traces:** Services, then Trace Analysis, filtered on `namespace="<namespace>" and service="<service>"`. Sort by duration and open a 5xx span.
- **Logs:** `kswapd OR "Out of memory"` for the memory scenario, `"payment gateway timeout"` for checkout. The trace id column joins each log back to its trace.

## Timing

- Metrics show up **2 to 3 minutes** after they're sent, at one point per minute. Traces and logs show up within about 30 seconds.
- Demo alerts fire on the first sample over the line, so the first alert appears **3 to 4 minutes** after you click.
- Every scenario ramps up over the first 15 % of its run, holds, then recovers over the last 20 %. With 10 minutes, the worst stretch is minutes 2 to 8.
- **Start a scenario 4 to 5 minutes before you show it**, and talk through the setup while it ramps.
- Only one website traffic run goes at a time. Clicking Error or Peak replaces Normal, so click Normal again afterwards.
- Rebuilding or restarting the container stops every running scenario.

## Health and smoke checks

- The Activity panel shows each delivery channel. A red pill means LogicMonitor rejected something; hover it for the last HTTP status: `401` is a token problem, `0` means no network or a wrong `LOGICMONITOR_ACCOUNT`.
- `docker logs lm-demo` prints the config file, site count, lmotel URL and baseline state at startup, plus a warning if credentials are missing.
- `docker exec lm-demo python3 -I /app/lm_check.py` lists the newest point per Demo datasource from the last 30 minutes, and every open alert.
- `docker exec lm-demo python3 -I /app/lm_export.py 45 > export.json` exports the configured dashboards' widget data and the open alerts from the last 45 minutes. It's useful for drawing backup slides when the portal can't be screenshotted.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Container exits with "no config" | Set `DEMO_CONFIG_JSON` in `.env`, or copy `config/demo.example.json` to `config/demo.json` |
| Container exits with a JSON error | `DEMO_CONFIG_JSON` must be one line, wrapped in single quotes |
| Traces pill stays at 0 or red | `LMOTEL_URL` isn't reachable from inside the container. Use the host's LAN address, and check that lmotel listens on OTLP/HTTP |
| APM services show "No member resources" | No resource has `system.hostname` equal to the site's `resource` value. Create the resource or fix the config |
| Logs land on the collector instead of the site | The resource's `system.hostname` doesn't match. LM Logs ignores `host.name` and `system.displayname` |
| Host or business graphs empty | `DEMO_BASELINE=false` and nothing is running. Turn the baseline on, or run **Set up LM datasources** |
| No alert after 5 minutes | Thresholds weren't set because the datasource didn't exist yet. Run **Set up LM datasources** again while nothing is running |
| "Start here" buttons missing | The dashboard names in the config don't match the portal, or the token can't read dashboards |

## Files

| Path | What it is |
| --- | --- |
| `app/server.py` | The console: HTTP server, scenario generators and LogicMonitor clients |
| `app/static/index.html` | The page; it reads every name from `/api/status` |
| `app/lm_check.py`, `app/lm_export.py` | Smoke check and dashboard data export |
| `config/demo.example.json` | Example config: three sites of a fictional company on one Docker host |
| `.env.example` | Every setting, with comments; copy it to `.env` |
