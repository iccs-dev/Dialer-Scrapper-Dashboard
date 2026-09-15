# Running with Docker

```bash
cp .env.example .env        # then fill in the blanks
docker compose up -d --build
```

The dashboard comes up on `http://127.0.0.1:8001`. Create a login once:

```bash
docker compose exec web python webapp/manage.py createsuperuser
```

Run a scraper:

```bash
docker compose run --rm scraper python Scrapper/Smart_Dial/Imagine/script.py 2026-09-14
docker compose run --rm scraper python Scrapper/Smart_Dial/DMI/script.py
docker compose run --rm scraper python combine.py 2026-09-14
```

## What runs where

```
web      dashboard, Gunicorn + Uvicorn worker, published on 127.0.0.1:8001
db       postgres:16          persistent volume postgres-data
redis    redis:7              channel layer for live updates
scraper  one-off runs only    profile "tools", never started by `up`
```

`web` and `scraper` share one image. Building the scrapers separately would
mean maintaining two Chromium installs for the same code.

## Why the base image is Microsoft's

`mcr.microsoft.com/playwright/python` already carries Chromium and the shared
libraries it links against. On a plain `python:` base those have to be
installed by hand, and the list changes with each Chromium bump.

**The tag must match `playwright` in requirements.txt.** The Python package and
its browser ship as a matched pair; a mismatch fails at launch with
"executable doesn't exist". Both are on 1.62.0 - change them together.

## Reaching the dialers

The scrapers talk to hosts on the internal network:

```
172.20.122.100   Smart Dial (Imagine, DMI)
172.20.122.35    Smart Dial (ICAI)
192.168.1.45     HRMS
onexvoice.k8stech.site
```

Default bridge networking reaches those through the host's routing table, so
no special configuration is needed. If a dialer is unreachable from inside a
container but fine from the host, the cause is usually a VPN or a route that
exists only in the host namespace - check with:

```bash
docker compose run --rm scraper python -c \
  "import socket; socket.create_connection(('172.20.122.100', 80), 5); print('reachable')"
```

`network_mode: host` is the fallback, at the cost of losing port publishing
and service DNS.

## Things the compose file deliberately overrides

`.env` is written for running directly on the host, so three values would be
wrong inside a container:

| Setting | Host | Container | Why |
|---|---|---|---|
| `DB_ENGINE` | sqlite | postgres | the `db` service exists here |
| `CHANNEL_LAYER` | memory | redis | shared layer, so >1 worker is safe |
| `MONITOR_API_URL` | `http://127.0.0.1:8001` | `http://web:8001` | loopback in a container is the container |

That last one matters: left alone, every scraper callback would go to the
scraper's own container and silently vanish - `monitor.py` never raises.

## Media/ and file ownership

`./Media` is bind-mounted, so scraper output, logs and error evidence stay on
the host and are readable without docker. The image aligns its user with the
host's uid so those files do not arrive owned by root. On a host where the
user is not uid 1000:

```bash
UID=$(id -u) GID=$(id -g) docker compose up -d --build
```

## Relationship to the systemd deployment

These are alternatives, not layers - pick one. Both publish the dashboard on
`127.0.0.1:8001`, so `deploy/nginx/dialer-dashboard.conf` works unchanged
either way, and Nginx stays on the host.

Not containerised: the Chromium kiosk display, which needs the desktop
session, and the nightly timers, which stay as host cron or systemd calling
`docker compose run --rm scraper ...`.
