"""WebSocket consumer for the live dashboard.

Design note: when something changes, the server does NOT push a serialised
copy of the state. It pushes a small "refresh" signal and the browser
re-fetches /api/dashboard/state/ and /logs/. That means:

* one source of truth (the REST API) - the WebSocket cannot drift from it;
* a reconnect is handled by the same code path as any other update, so the
  screen can never show stale data after the link comes back (requirement 8);
* no serialisation logic duplicated between api.py and this file.

The cost is one extra HTTP round-trip per change, which on a LAN is a few
milliseconds - far cheaper than two code paths that can disagree.
"""

from channels.generic.websocket import AsyncJsonWebsocketConsumer

DASHBOARD_GROUP = "dashboard"


class DashboardConsumer(AsyncJsonWebsocketConsumer):
    """One connected browser tab."""

    async def connect(self):
        # Requirement 16: WebSocket authentication. AuthMiddlewareStack has
        # already resolved the session cookie into scope["user"]; anonymous
        # sockets are closed rather than silently fed data.
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)          # 4401 = unauthorised
            return

        await self.channel_layer.group_add(DASHBOARD_GROUP, self.channel_name)
        await self.accept()
        # Tell the client to pull a full state immediately, so a freshly opened
        # tab is correct without waiting for the next change.
        await self.send_json({"type": "refresh", "reason": "connected"})

    async def disconnect(self, code):
        await self.channel_layer.group_discard(DASHBOARD_GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # The client only ever pings, to keep intermediaries from idling the
        # connection out. Anything else is ignored rather than trusted.
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})

    # -- messages sent by the server -----------------------------------------

    async def dashboard_refresh(self, event):
        """Handler for group_send({"type": "dashboard.refresh", ...})."""
        await self.send_json({
            "type": "refresh",
            "reason": event.get("reason", "update"),
            "process": event.get("process"),
            "status": event.get("status"),
        })
