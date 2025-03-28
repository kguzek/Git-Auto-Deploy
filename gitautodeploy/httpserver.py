"""HTTP Server"""

from __future__ import absolute_import

import logging
import json
import threading
import base64
import time
import os

from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs

from .events import WebhookAction
from .parsers import get_service_handler


def webhook_request_handler_factory(config, event_store, server_status, is_https=False):
    """Factory method for webhook request handler class"""

    class WebhookRequestHandler(SimpleHTTPRequestHandler):
        """Extends the BaseHTTPRequestHandler class and handles the incoming
        HTTP requests."""

        def __init__(self, *args, **kwargs):
            self._config = config
            self._event_store = event_store
            self._server_status = server_status
            self._is_https = is_https
            super(WebhookRequestHandler, self).__init__(*args, **kwargs)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            SimpleHTTPRequestHandler.end_headers(self)

        def do_HEAD(self):

            # Web UI needs to be enabled
            if not self.validate_web_ui_enabled():
                return

            # Web UI might require HTTPS
            if not self.validate_web_ui_https():
                return

            # Client needs to be whitelisted
            if not self.validate_web_ui_whitelist():
                return

            # Client needs to authenticate
            if not self.validate_web_ui_basic_auth():
                return

            return SimpleHTTPRequestHandler.do_HEAD(self)

        def do_GET(self):

            # Web UI needs to be enabled
            if not self.validate_web_ui_enabled():
                return

            # Web UI might require HTTPS
            if not self.validate_web_ui_https():
                return

            # Client needs to be whitelisted
            if not self.validate_web_ui_whitelist():
                return

            # Client needs to authenticate
            if not self.validate_web_ui_basic_auth():
                return

            # Handle status API call
            if self.path == "/api/status":
                self.handle_status_api()
                return

            # Serve static file
            return SimpleHTTPRequestHandler.do_GET(self)

        def handle_status_api(self):
            """Handle the status API call"""

            data = {
                "events": self._event_store.dict_repr(),
                "auth-key": self._server_status["auth-key"],
            }

            data.update(self.get_server_status())

            self.send_response(200, "OK")
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        def do_POST(self):
            """Invoked on incoming POST requests"""

            content_length = int(self.headers.get("content-length"))
            request_body_bytes = self.rfile.read(content_length)
            request_body = request_body_bytes.decode("utf-8")

            # Extract request headers and make all keys to lowercase (makes them easier to compare)
            request_headers = dict(self.headers)
            request_headers = dict((k.lower(), v) for k, v in request_headers.items())

            action = WebhookAction(self.client_address, request_headers, request_body)
            self._event_store.register_action(action)
            action.set_waiting(True)

            action.log_info(
                f"Incoming request from {self.client_address[0]}:{self.client_address[1]}"
            )

            # Payloads from GitHub can be delivered as form data.
            # Test the request for this pattern and extract json payload
            if request_headers["content-type"] == "application/x-www-form-urlencoded":
                res = parse_qs(request_body)
                if "payload" in res and len(res["payload"]) == 1:
                    request_body = res["payload"][0]

            # Test case debug data
            test_case = {
                "headers": dict(self.headers),
                "payload": json.loads(request_body),
                "config": {},
                "expected": {"status": 200, "data": [{"deploy": 0}]},
            }

            try:
                # Will raise a ValueError exception if it fails
                service_handler = get_service_handler(
                    request_headers, request_body, action, self._config
                )

                # Unable to identify the source of the request
                if service_handler is None:
                    self.send_error(400, "Unrecognized service")
                    test_case["expected"]["status"] = 400
                    action.log_error(
                        "Unable to find appropriate handler for request. "
                        "The source service is not supported"
                    )
                    action.set_waiting(False)
                    action.set_success(False)
                    return

                action.log_info(
                    f"Handling the request with {service_handler.__class__.__name__}"
                )

                # Could be GitHubParser, GitLabParser or other
                projects = service_handler.get_matching_projects(
                    request_headers, request_body, action
                )

                action.log_info(f"{len(projects)} candidates matches the request")

                # request_filter = WebhookRequestFilter()

                if len(projects) == 0:
                    self.send_error(400, "Bad request")
                    test_case["expected"]["status"] = 400
                    action.log_error("No matching projects")
                    action.set_waiting(False)
                    action.set_success(False)
                    return

                # Apply filters
                matching_projects = []
                for project in projects:
                    if project.apply_filters(request_headers, request_body, action):
                        matching_projects.append(project)

                # Only keep projects that matches
                projects = matching_projects

                action.log_info(
                    f"{len(projects)} candidates matches after applying filters"
                )

                if not service_handler.validate_request(
                    request_headers, request_body_bytes, projects, action
                ):
                    self.send_error(400, "Bad request")
                    test_case["expected"]["status"] = 400
                    action.log_warning(
                        "Request was rejected due to a secret token mismatch"
                    )
                    action.set_waiting(False)
                    action.set_success(False)
                    return

                test_case["expected"]["status"] = 200

                self.send_response(200, "OK")
                self.send_header("Content-type", "text/plain")
                self.end_headers()

                if len(projects) == 0:
                    action.set_waiting(False)
                    action.set_success(False)
                    return

                action.log_info(f"Proceeding with {len(projects)} candidates")
                action.set_waiting(False)
                action.set_success(True)

                for project in projects:

                    # Schedule the execution of the webhook (git pull and trigger deploy etc)
                    thread = threading.Thread(
                        target=project.execute_webhook, args=[self._event_store]
                    )
                    thread.start()

                    # Add additional test case data
                    test_case["config"] = {
                        "url": "url" in project and project["url"],
                        "branch": "branch" in project and project["branch"],
                        "remote": "remote" in project and project["remote"],
                        "deploy": "echo test!",
                    }

            except ValueError:
                self.send_error(400, "Unprocessable request")
                action.log_warning(
                    f"Unable to process incoming request from "
                    f"{self.client_address[0]}:{self.client_address[1]}"
                )
                test_case["expected"]["status"] = 400
                action.set_waiting(False)
                action.set_success(False)
                return

            except Exception as e:
                self.send_error(500, "Unable to process request")
                test_case["expected"]["status"] = 500
                action.log_warning("Unable to process request")
                action.set_waiting(False)
                action.set_success(False)

                raise e

            finally:

                # Save the request as a test case
                if "log-test-case" in self._config and self._config["log-test-case"]:
                    self.save_test_case(test_case)

        def log_message(self, log_format, *args):
            """Overloads the default message logging method to allow messages to
            go through our custom logger instead."""

            logger = logging.getLogger()
            formatted = log_format % args
            logger.info("%s - %s", self.client_address[0], formatted)

        def save_test_case(self, test_case):
            """Log request information in a way it can be used as a test case."""

            # Mask some header values
            masked_headers = ["x-github-delivery", "x-hub-signature"]
            for key in test_case["headers"]:
                if key in masked_headers:
                    test_case["headers"][key] = "xxx"

            target = f"{self.client_address[0]}-{time.strftime("%Y%m%d%H%M%S")}.tc.json"
            if (
                "log-test-case-dir" in self._config
                and self._config["log-test-case-dir"]
            ):
                target = os.path.join(self._config["log-test-case-dir"], target)

            file = open(target, "w", encoding="utf8")
            file.write(json.dumps(test_case, sort_keys=True, indent=4))
            file.close()

        def get_server_status(self):
            """Generate a copy of the server status object containing the public IP or hostname."""

            server_status = {}
            for item in self._server_status.items():
                key, value = item
                public_host = self.headers.get("host").split(":")[0]

                if key == "http-uri":
                    server_status[key] = value.replace(
                        self._config["http-host"], public_host
                    )

                if key == "https-uri":
                    server_status[key] = value.replace(
                        self._config["https-host"], public_host
                    )

                if key == "wss-uri":
                    server_status[key] = value.replace(
                        self._config["wss-host"], public_host
                    )

            return server_status

        def validate_web_ui_enabled(self):
            """Verify that the Web UI is enabled"""

            if self._config["web-ui-enabled"]:
                return True

            self.send_error(403, "Web UI is not enabled")
            return False

        def validate_web_ui_https(self):
            """Verify that the request is made over HTTPS"""

            if self._is_https:
                return True

            if not self._config["web-ui-require-https"]:
                return True

            # Attempt to redirect the request to HTTPS
            server_status = self.get_server_status()
            if "https-uri" in server_status:
                self.send_response(307)
                self.send_header("Location", f"{server_status["https-uri"]}{self.path}")
                self.end_headers()
                return False

            self.send_error(403, "Web UI is only accessible through HTTPS")
            return False

        def validate_web_ui_whitelist(self):
            """Verify that the client address is whitelisted"""

            # Allow all if whitelist is empty
            if len(self._config["web-ui-whitelist"]) == 0:
                return True

            # Verify that client IP is whitelisted
            if self.client_address[0] in self._config["web-ui-whitelist"]:
                return True

            self.send_error(403, f"{self.client_address[0]} is not allowed access")
            return False

        def validate_web_ui_basic_auth(self):
            """Authenticate the user"""

            if not self._config["web-ui-auth-enabled"]:
                return True

            # Verify that a username and password is specified in the config
            if (
                self._config["web-ui-username"] is None
                or self._config["web-ui-password"] is None
            ):
                self.send_error(403, "Authentication credentials missing in config")
                return False

            # Verify that the provided username and password matches the ones in the config
            credentials = (
                f"{self._config["web-ui-username"]}:{self._config["web-ui-password"]}"
            )
            key = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
            if self.headers.get("Authorization") == "Basic " + key:
                return True

            # Let the client know that authentication is required
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="GAD"')
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write("Not authenticated".encode("utf-8"))
            return False

    return WebhookRequestHandler
