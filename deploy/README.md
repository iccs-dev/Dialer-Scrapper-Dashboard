# Deployment

## What runs where

```
browser / kiosk  ->  Nginx :80          (the only process on the network)
                       |  /static/      served from /var/www/dialer-dashboard/static
                       |  /ws/          WebSocket, upgraded and unbuffered
                       |  /             everything else
                       v
                     Gunicorn 127.0.0.1:8001   (loopback only - unreachable from the LAN)
                       |  worker class: uvicorn_worker.UvicornWorker (ASGI)
                       v
                     Django + Channels
                       |
                       +-- SQLite  (or PostgreSQL when DB_ENGINE=postgres)
                       +-- in-memory channel layer (or Redis when CHANNEL_LAYER=redis)

scrapers  ->  http://127.0.0.1:8001/api/monitor/...  with X-Monitor-Token
```

The development server is not used. Gunicorn binds to loopback, so even with
no firewall the application server cannot be reached from another machine;
Nginx is the single front door.

## Why an ASGI worker

Gunicorn's default worker speaks WSGI and cannot carry a WebSocket. Running
the dashboard on it produces a site that loads perfectly and then never
updates, because every `/ws/` handshake 404s. `gunicorn.conf.py` therefore
pins `worker_class = "uvicorn_worker.UvicornWorker"`.

## Why one worker, for now

Each Gunicorn worker is a separate process. The in-memory channel layer lives
inside one process, so a scraper callback arriving at worker 1 cannot reach a
browser connected to worker 2 - updates would be delivered or dropped at
random depending on which worker happened to take the request.

`gunicorn.conf.py` refuses to start extra workers while that layer is in use
and says so at startup. Install Redis and set `CHANNEL_LAYER=redis` to lift
the cap; `GUNICORN_WORKERS` is already set to 3 and takes effect then.

## Install (needs root)

    sudo bash deploy/install_stage7.sh              # Nginx
    sudo bash deploy/install_stage7.sh --with-redis # Nginx + Redis

Then, as the application user and without sudo:

    cd webapp
    ../venv/bin/python manage.py collectstatic --noinput
    ../venv/bin/gunicorn -c gunicorn.conf.py dialer_dashboard.asgi:application

Stage 8 replaces that last command with a systemd service.

## Static files

`collectstatic` writes to `DJANGO_STATIC_ROOT`. In deployment that is
`/var/www/dialer-dashboard/static`, owned by the application user and group
`www-data`. It is deliberately outside the home directory: `/home/<user>` is
mode 750 and Nginx, running as `www-data`, cannot traverse it.

Re-run `collectstatic` after any change to CSS or JavaScript. Filenames are
content-hashed, so a deploy invalidates the browser and Nginx caches by
itself and the kiosk never needs clearing.

## HTTPS

The site is HTTP-only on a private LAN; there is no certificate for the
server address. All the TLS-dependent settings are written out in
`settings.py` behind one flag. With a certificate in front of Nginx, set
`DJANGO_SECURE_SSL=true` and the redirect, HSTS and secure-cookie settings
all switch on together, along with the four system checks that are currently
silenced for a documented reason.
