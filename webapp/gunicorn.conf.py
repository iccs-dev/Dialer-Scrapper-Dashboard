"""Gunicorn configuration for the dialer dashboard.

Run it with:

    cd webapp && ../venv/bin/gunicorn -c gunicorn.conf.py dialer_dashboard.asgi:application

Two things about this stack are worth stating plainly, because getting either
one wrong produces a dashboard that looks fine and silently stops updating:

1. The worker class is an ASGI worker, not Gunicorn's default. Django Channels
   serves WebSockets over ASGI; Gunicorn's default worker speaks WSGI only and
   would answer every /ws/ request with a 404 while the HTTP pages kept
   working. That failure is invisible until you notice the live tiles have
   gone stale.

2. More than one worker requires Redis. Each worker is a separate process with
   its own memory, so the in-memory channel layer cannot carry a message from
   the worker a scraper posted to over to the worker a browser is connected
   to. This file refuses to start extra workers while that layer is in use
   rather than letting the dashboard drop updates at random.
"""

import multiprocessing
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
except ImportError:              # python-dotenv is optional
    load_dotenv = None
if load_dotenv is not None:
    # Gunicorn reads this file before Django loads its settings, so the shared
    # .env has to be read here as well or GUNICORN_* would never be seen.
    load_dotenv(BASE_DIR.parent / ".env")


def _int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


chdir = str(BASE_DIR)

# Loopback only. Nginx is the sole front door; nothing outside this machine can
# reach Gunicorn directly even if the firewall is wide open, which is what
# keeps the application server off the public network.
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8001")

worker_class = "uvicorn_worker.UvicornWorker"

_redis_channels = os.getenv("CHANNEL_LAYER", "memory").lower().startswith("redis")
_requested = _int("GUNICORN_WORKERS", min(multiprocessing.cpu_count(), 4))
workers = _requested if _redis_channels else 1

# A blocked event loop, not a slow request, is what this catches: an ASGI
# worker pings the arbiter on a timer rather than per request, so a long-lived
# WebSocket does not count against it.
timeout = _int("GUNICORN_TIMEOUT", 120)
graceful_timeout = 30

# How long a kept-alive connection from Nginx stays open between requests.
keepalive = 5

# Nginx sets X-Forwarded-For and X-Forwarded-Proto. Trust them from loopback
# only, so a client cannot forge its own source address or scheme.
forwarded_allow_ips = os.getenv("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")

# Logs go to stdout/stderr so systemd captures them in the journal in stage 8;
# no log files to rotate, and `journalctl -u dialer-dashboard` shows everything.
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'

proc_name = "dialer-dashboard"

# Left off deliberately. Preloading builds the app before forking, which hands
# every worker a copy of an event loop that was created in the parent.
preload_app = False


def on_starting(server):
    """Say which layer is in use, and why the worker count is what it is."""
    layer = "redis" if _redis_channels else "in-memory"
    server.log.info("channel layer: %s | workers: %d", layer, workers)
    if not _redis_channels and _requested > 1:
        server.log.warning(
            "GUNICORN_WORKERS=%d ignored: the in-memory channel layer cannot "
            "share messages between processes, so extra workers would drop "
            "live updates. Install Redis and set CHANNEL_LAYER=redis first.",
            _requested,
        )
