"""Module for the WebSocket server class"""

import logging
import json


# from .events import SystemEvent

try:
    from autobahn.twisted.websocket import WebSocketServerProtocol
except ImportError:
    WebSocketServerProtocol = object


def websocket_client_handler_factory(config, clients, event_store, server_status):
    """Factory method for webhook request handler class"""

    class WebSocketClientHandler(WebSocketServerProtocol):
        """WebSocket client handler class"""

        def __init__(self, *args, **kwargs):
            self._config = config
            self.clients = clients
            self._event_store = event_store
            self._server_status = server_status
            self.logger = logging.getLogger()
            super(WebSocketClientHandler, self).__init__(*args, **kwargs)

        def onConnect(self, request):
            """Called when a client connects to the WebSocket server"""
            self.logger.info("Client connecting: %s", request.peer)

            # Web UI needs to be enabled
            if not self.validate_web_ui_enabled():
                return

            # Client needs to be whitelisted
            if not self.validate_web_ui_whitelist():
                return

        def onOpen(self):
            """Called when the WebSocket connection is opened"""
            self.logger.info("WebSocket connection open.")

        def onMessage(self, payload, isBinary):
            """Called when a message is recieved from the client"""

            if isBinary:
                return

            try:
                data = json.loads(payload)

                # Handle authentication requests
                if "type" in data and data["type"] == "authenticate":

                    # Verify auth key
                    if (
                        "auth-key" in data
                        and data["auth-key"] == self._server_status["auth-key"]
                    ):
                        self.clients.append(self)

                        # Let the client know that they are authenticated
                        self.sendMessage(json.dumps({"type": "authenticated"}))
                        return

                    self.logger.error("Recieved bad auth key.")

                    self.sendMessage(json.dumps({"type": "bad-auth-key"}))
                    return

            except Exception as e:
                self.logger.error("Unable to interpret incoming message: %s", e)

            if self not in self.clients:
                self.logger.error(
                    "Recieved message form unauthenticated client, closing connection."
                )
                self.sendClose()
                return

            # self.logger.info("WebSocket connection open.")
            # if isBinary:
            #    self.logger.info("Binary message received: {0} bytes".format(len(payload)))
            # else:
            #    self.logger.info("Text message received: {0}".format(payload.decode('utf8')))

            # for client in self.clients:
            #    client.sendMessage(payload, isBinary)

            # echo back message verbatim
            # self.sendMessage(payload, isBinary)

        def onClose(self, wasClean, code, reason):
            """Called when the WebSocket connection is closed"""
            self.logger.info("WebSocket connection closed: %s", reason)

            if self in self.clients:
                self.clients.remove(self)

        def validate_web_ui_enabled(self):
            """Verify that the Web UI is enabled"""

            if self._config["web-ui-enabled"]:
                return True

            self.sendClose()
            return False

        def validate_web_ui_whitelist(self):
            """Verify that the client address is whitelisted"""

            # Allow all if whitelist is empty
            if len(self._config["web-ui-whitelist"]) == 0:
                return True

            # Verify that client IP is whitelisted
            if self.peer.host in self._config["web-ui-whitelist"]:
                return True

            self.sendClose()
            self.logger.info("Unautorized connection attempt from %s", self.peer.host)
            return False

    return WebSocketClientHandler
