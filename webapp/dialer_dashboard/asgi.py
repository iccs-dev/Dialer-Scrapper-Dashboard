"""ASGI entry point - serves both HTTP and WebSocket.

Gunicorn speaks WSGI and cannot carry WebSockets, so the whole site is served
through this ASGI application (Stage 7 runs it under Daphne/Uvicorn behind
Nginx). HTTP requests still go to the ordinary Django view stack; only the
ws/ paths are handled by Channels.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dialer_dashboard.settings")

# The Django app must be built before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack          # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from monitoring.routing import websocket_urlpatterns   # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # AllowedHostsOriginValidator rejects WebSocket handshakes from origins
    # outside ALLOWED_HOSTS - the WebSocket equivalent of CSRF protection.
    # AuthMiddlewareStack resolves the session cookie into scope["user"].
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})
