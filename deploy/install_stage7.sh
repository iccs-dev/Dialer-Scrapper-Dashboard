#!/usr/bin/env bash
#
# Stage 7 - put the dashboard behind Nginx and take the development server out
# of service. Read this before running it; it is the only part of the stage
# that needs root.
#
#     sudo bash deploy/install_stage7.sh              # Nginx only
#     sudo bash deploy/install_stage7.sh --with-redis # also install Redis
#
# Safe to run more than once: every step checks its own result first.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATIC_DIR="/var/www/dialer-dashboard/static"
SITE="dialer-dashboard.conf"
WITH_REDIS=0

for arg in "$@"; do
    case "$arg" in
        --with-redis) WITH_REDIS=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root:  sudo bash $0 $*" >&2
    exit 1
fi

# The account that owns the code and will run Gunicorn. Taken from the project
# directory rather than hardcoded, so the script survives being moved.
APP_USER="$(stat -c '%U' "$PROJECT_DIR")"
echo "project : $PROJECT_DIR"
echo "app user: $APP_USER"
echo

echo "==> 1/6 installing nginx"
if ! dpkg -s nginx >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
else
    echo "    already installed"
fi

if [[ $WITH_REDIS -eq 1 ]]; then
    echo "==> 1b   installing redis-server"
    if ! dpkg -s redis-server >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y redis-server
    else
        echo "    already installed"
    fi
    systemctl enable --now redis-server
fi

echo "==> 2/6 creating $STATIC_DIR"
# Owned by the app user so collectstatic needs no sudo, group www-data so
# Nginx can read it. This is why the static files do not live in the home
# directory: /home/$APP_USER is mode 750 and Nginx cannot traverse it.
install -d -o "$APP_USER" -g www-data -m 2775 /var/www/dialer-dashboard
install -d -o "$APP_USER" -g www-data -m 2775 "$STATIC_DIR"
echo "    $(stat -c '%U:%G %a' "$STATIC_DIR") $STATIC_DIR"

echo "==> 3/6 enabling DJANGO_STATIC_ROOT in .env"
if grep -q '^#DJANGO_STATIC_ROOT=' "$PROJECT_DIR/.env"; then
    sed -i 's|^#DJANGO_STATIC_ROOT=|DJANGO_STATIC_ROOT=|' "$PROJECT_DIR/.env"
    echo "    uncommented"
else
    echo "    already set"
fi

echo "==> 4/6 installing the nginx site"
cp "$PROJECT_DIR/deploy/nginx/$SITE" "/etc/nginx/sites-available/$SITE"
ln -sfn "/etc/nginx/sites-available/$SITE" "/etc/nginx/sites-enabled/$SITE"
# Ubuntu's stock site also claims default_server on port 80; leaving it in
# place makes nginx refuse to start with a duplicate-default error.
if [[ -e /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
    echo "    removed the stock default site"
fi

echo "==> 5/6 testing the nginx configuration"
nginx -t

echo "==> 6/6 reloading nginx"
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx

echo
echo "Done. Now, as $APP_USER and WITHOUT sudo:"
echo "    cd '$PROJECT_DIR/webapp'"
echo "    ../venv/bin/python manage.py collectstatic --noinput"
echo "    ../venv/bin/gunicorn -c gunicorn.conf.py dialer_dashboard.asgi:application"
echo "Then open http://10.140.28.15/dashboard/"
if [[ $WITH_REDIS -eq 1 ]]; then
    echo
    echo "Redis installed. Set CHANNEL_LAYER=redis in .env to use multiple workers."
fi
