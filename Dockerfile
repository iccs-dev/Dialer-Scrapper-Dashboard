# Dialer Scrapper - one image for both the dashboard and the scrapers.
#
# Based on Microsoft's Playwright image because it already carries Chromium and
# the ~40 shared libraries it links against. Building on a plain python image
# means installing those by hand and re-solving that list on every base bump.
#
# The tag must track the playwright version in requirements.txt: the Python
# package and the bundled browser are released as a matched pair, and a
# mismatch fails at launch with an unhelpful "executable doesn't exist".
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

# Bind-mounted Media/ is written by the container and read on the host, so the
# container user needs the host user's id or every output file lands as root.
# Override at build time when the host user is not 1000: --build-arg UID=$(id -u)
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Where the browsers live in this base image.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first: this layer is only rebuilt when requirements.txt changes,
# not on every source edit.
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

COPY . .

# The base image ships a "pwuser"; align it with the host owner of Media/.
RUN groupmod -o -g "$GID" pwuser 2>/dev/null || true && \
    usermod  -o -u "$UID" -g "$GID" pwuser 2>/dev/null || true && \
    mkdir -p /app/Media /app/webapp/staticfiles && \
    chown -R "$UID:$GID" /app

COPY --chown=${UID}:${GID} docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER pwuser

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Loopback inside the container; the port is published by compose.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "dialer_dashboard.asgi:application", \
     "--chdir", "/app/webapp", "--bind", "0.0.0.0:8001"]
