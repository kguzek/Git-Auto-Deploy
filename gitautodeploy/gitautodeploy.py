"""Application entry point for Git Auto Deploy."""

import os
import logging
import sys
import socket
import ssl
import re
import errno
import json
import base64
import getpass
from http.server import HTTPServer
import threading
import signal

from .events import SystemEvent, EventStore, StartupEvent
from .lock import Lock
from .wrappers import GitWrapper, ProcessWrapper

from .wsserver import websocket_client_handler_factory
from .httpserver import webhook_request_handler_factory

from .cli.config import get_config_defaults, get_config_from_environment
from .cli.config import get_config_from_argv
from .cli.config import get_config_from_file, get_repo_config_from_environment
from .cli.config import (
    init_config,
    get_config_file_path,
    rename_legacy_attribute_names,
)
from .cli.config import ConfigFileNotFoundException, ConfigFileInvalidException


# This solves https://github.com/olipo186/Git-Auto-Deploy/issues/118
try:
    from logging import NullHandler
except ImportError:
    from logging import Handler

    class NullHandler(Handler):
        """No-op"""

        def emit(self, record):
            pass


if __name__ == "__main__":
    print(
        "Critical - GAD must be started as a python module, "
        "for example using python -m gitautodeploy"
    )
    sys.exit()


class LogInterface:
    """Interface that functions as a stdout and stderr handler and directs the
    output to the logging module, which in turn will output to either console,
    file or both."""

    def __init__(self, level=None):
        self.level = level if level else logging.getLogger().info

    def write(self, msg):
        """Write a message to the log"""
        for line in msg.strip().split("\n"):
            self.level(line)

    def flush(self):
        """Flush the output buffer (no-op)"""


class GitAutoDeploy:
    """Main app instance"""

    _instance = None
    _http_server = None
    _https_server = None
    _https_server_unwrapped_socket = None
    _config = {}
    _server_status = {}
    _pid = None
    _event_store = None
    _default_stdout = None
    _default_stderr = None
    _startup_event = None
    _ws_clients = []
    _http_port = None

    def __new__(cls, *args, **kwargs):
        """Overload constructor to enable singleton access"""
        if not cls._instance:
            cls._instance = super(GitAutoDeploy, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):

        # Setup an event store instance that can keep a global record of events
        self._event_store = EventStore()
        self._event_store.register_observer(self)

        # Create a startup event that can hold status and any error messages
        # from the startup process
        self._startup_event = StartupEvent()
        self._event_store.register_action(self._startup_event)
        self._ws_server_port = None

    def clone_all_repos(self):
        """Iterates over all configured repositories and clones them to their
        configured paths."""

        logger = logging.getLogger()

        if "repositories" not in self._config:
            return

        # Iterate over all configured repositories
        for repo_config in self._config["repositories"]:

            # Only clone repositories with a configured path
            if "url" not in repo_config:
                logger.critical("Repository has no configured URL")
                self.exit()
                return

            # Only clone repositories with a configured path
            if "path" not in repo_config:
                logger.debug(
                    "Repository %s will not be cloned (no path configured)",
                    repo_config["url"],
                )
                continue

            if repo_config.get("skip-init"):
                logger.debug(
                    "Skipping repository %s (skip-init option)", repo_config["url"]
                )
                continue

            if os.path.isdir(repo_config["path"]) and os.path.isdir(
                repo_config["path"] + "/.git"
            ):
                GitWrapper.init(repo_config)
            else:
                GitWrapper.clone(repo_config)

    def ssh_key_scan(self):
        """Scans for ssh keys in the repositories and adds them to the known_hosts file"""
        logger = logging.getLogger()

        for repository in self._config["repositories"]:

            if "url" not in repository:
                continue

            logger.info("Scanning repository: %s", repository["url"])
            m = re.match(r"[^\@]+\@([^\:\/]+)(:(\d+))?", repository["url"])

            if m is not None:
                host = m.group(1)
                port = m.group(3)
                port_arg = "" if port is None else f"-p {port} "
                cmd = f"ssh-keyscan {port_arg}{host} >> $HOME/.ssh/known_hosts"
                ProcessWrapper().call([cmd], shell=True)

            else:
                logger.error(
                    "Could not find regexp match in path: %s", repository["url"]
                )

    def create_pid_file(self):
        """Creates the lockfile"""
        with open(self._config["pid-file"], "w", encoding="utf8") as f:
            f.write(str(os.getpid()))

    def read_pid_file(self):
        """Returns the previously written pid"""
        with open(self._config["pid-file"], "r", encoding="utf8") as f:
            return f.readlines()

    def remove_pid_file(self):
        """Removes the pid file"""

        if "pid-file" in self._config and self._config["pid-file"]:
            try:
                os.remove(self._config["pid-file"])
            except OSError as e:
                # errno.ENOENT = no such file or directory
                if e.errno != errno.ENOENT:
                    raise

    @staticmethod
    def create_daemon():
        """Daemonize the process"""
        try:
            # Spawn first child. Returns 0 in the child and pid in the parent.
            pid = os.fork()
        except OSError as e:
            raise Exception(f"{e.strerror} [{e.errno}]") from e

        # First child
        if pid == 0:
            os.setsid()

            try:
                # Spawn second child
                pid = os.fork()

            except OSError as e:
                raise Exception(f"{e.strerror} [{e.errno}]") from e

            if pid == 0:
                os.umask(0)
            else:
                # Kill first child
                os._exit(0)
        else:
            # Kill parent of first child
            os._exit(0)

        return 0

    def update(self, *args, **kwargs):
        """Update all connected web socket clients with the provided data."""
        data = json.dumps(kwargs).encode("utf-8")
        for client in self._ws_clients:
            client.sendMessage(data)

    def get_log_formatter(self):
        """Returns a log formatter that includes the timestamp and log level."""

        return logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s")

    def setup_console_logger(self):
        """Set up a console logger that outputs to stdout."""
        logger = logging.getLogger()

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(self.get_log_formatter())

        # Check if a stream handler is already present (will be if GAD is started by test script)
        handler_present = False
        for handler in logger.handlers:
            if isinstance(handler, type(console_handler)):
                handler_present = True
                break

        if not handler_present:
            logger.addHandler(console_handler)

    def setup(self, config):
        """Setup an instance of GAD based on the provided config object."""

        # Attatch config values to this instance
        self._config = config

        # Set up logging
        logger = logging.getLogger()
        log_formatter = self.get_log_formatter()

        # Enable console output?
        if ("quiet" in self._config and self._config["quiet"]) or (
            "daemon-mode" in self._config and self._config["daemon-mode"]
        ):

            # Add a default null handler that suppresses any console output
            logger.addHandler(NullHandler())

        else:

            # Set up console logger if not already present
            self.setup_console_logger()

        # Set logging level
        if "log-level" in self._config:
            level = logging.getLevelName(self._config["log-level"])
            logger.setLevel(level)

        if "log-file" in self._config and self._config["log-file"]:
            # Translate any ~ in the path into /home/<user>
            file_handler = logging.FileHandler(self._config["log-file"])
            file_handler.setFormatter(log_formatter)
            logger.addHandler(file_handler)

        # Display a warning when trying to run as root
        if not self._config["allow-root-user"] and getpass.getuser() == "root":
            logger.critical(
                "Refusing to start as root. This application shouldn't run as a privileged user. "
                "Please run it as a different user. To disregard this warning and start anyway, "
                "set the config option 'allow-root-user' to true, or use the command line argument "
                "--allow-root-user"
            )
            sys.exit()

        if "ssh-keyscan" in self._config and self._config["ssh-keyscan"]:
            self._startup_event.log_info("Scanning repository hosts for ssh keys...")
            self.ssh_key_scan()

        # Clone all repos once initially
        self.clone_all_repos()

        # Set default stdout and stderr to our logging interface (that writes
        # to file and console depending on user preference)
        if "intercept-stdout" in self._config and self._config["intercept-stdout"]:
            self._default_stdout = sys.stdout
            self._default_stderr = sys.stderr
            sys.stdout = LogInterface(logger.info)
            sys.stderr = LogInterface(logger.error)

        if "daemon-mode" in self._config and self._config["daemon-mode"]:
            self._startup_event.log_info("Starting Git Auto Deploy in daemon mode")
            GitAutoDeploy.create_daemon()

        self._pid = os.getpid()
        self.create_pid_file()

        # Generate auth key to protect the web socket server
        self._server_status["auth-key"] = base64.b64encode(os.urandom(32)).decode(
            "utf-8"
        )

        # Clear any existing lock files, with no regard to possible ongoing processes
        for repo_config in self._config["repositories"]:

            # Do we have a physical repository?
            if "path" in repo_config:
                Lock(os.path.join(repo_config["path"], "status_running")).clear()
                Lock(os.path.join(repo_config["path"], "status_waiting")).clear()

        # if 'daemon-mode' not in self._config or not self._config['daemon-mode']:
        #    self._startup_event.log_info('Git Auto Deploy started')

    def serve_http(self, serve_forever=True):
        """Starts a HTTP server that listens for webhook requests and serves the web ui."""

        if not self._config["http-enabled"]:
            return

        # Setup
        try:

            # Create web hook request handler class
            WebhookRequestHandler = webhook_request_handler_factory(
                self._config, self._event_store, self._server_status, is_https=False
            )

            # Create HTTP server
            self._http_server = HTTPServer(
                (self._config["http-host"], self._config["http-port"]),
                WebhookRequestHandler,
            )

            # Setup SSL for HTTP server
            sa = self._http_server.socket.getsockname()
            self._http_port = sa[1]
            http_local_uri = f"http://{self._config["http-host"]}:{sa[1]}"
            self._server_status["http-uri"] = (
                self._config["http-public-uri"] or http_local_uri
            )
            self._startup_event.log_info(
                f"Listening for connections on {self._server_status["http-uri"]}"
            )
            self._startup_event.http_address = sa[0]
            self._startup_event.http_port = sa[1]
            self._startup_event.set_http_started(True)

        except socket.error as e:
            self._startup_event.log_critical(f"Unable to start HTTP server: {e}")
            return

        if not serve_forever:
            return

        # Run forever
        try:
            self._http_server.serve_forever()

        except socket.error as e:
            event = SystemEvent()
            self._event_store.register_action(event)
            event.log_critical(f"Error on socket: {e}")
            sys.exit(1)

        except KeyboardInterrupt as e:
            event = SystemEvent()
            self._event_store.register_action(event)
            event.log_info("Requested close by keyboard interrupt signal")
            self.stop()
            self.exit()

        event = SystemEvent()
        self._event_store.register_action(event)
        event.log_info("HTTP server did quit")

    def serve_https(self):
        """Starts a HTTPS server that listens for webhook requests and serves the web ui."""

        if not self._config["https-enabled"]:
            return

        if not os.path.isfile(self._config["ssl-cert"]):
            self._startup_event.log_critical(
                f"Unable to activate SSL: File does not exist: {self._config["ssl-cert"]}"
            )
            return

        # Setup
        try:

            # Create web hook request handler class
            WebhookRequestHandler = webhook_request_handler_factory(
                self._config, self._event_store, self._server_status, is_https=True
            )

            # Create HTTP server
            self._https_server = HTTPServer(
                (self._config["https-host"], self._config["https-port"]),
                WebhookRequestHandler,
            )

            # Setup SSL for HTTP server
            self._https_server_unwrapped_socket = self._https_server.socket
            self._https_server.socket = ssl.wrap_socket(
                self._https_server.socket,
                keyfile=self._config["ssl-key"],
                certfile=self._config["ssl-cert"],
                server_side=True,
            )

            sa = self._https_server.socket.getsockname()
            self._http_port = sa[1]
            self._server_status["https-uri"] = (
                f"https://{self._config["https-host"]}:{sa[1]}"
            )

            self._startup_event.log_info(
                f"Listening for connections on {self._server_status["https-uri"]}"
            )
            self._startup_event.http_address = sa[0]
            self._startup_event.http_port = sa[1]
            self._startup_event.set_http_started(True)

        except socket.error as e:
            self._startup_event.log_critical(f"Unable to start HTTPS server: {e}")
            return

        # Run forever
        try:
            self._https_server.serve_forever()

        except socket.error as e:
            event = SystemEvent()
            self._event_store.register_action(event)
            event.log_critical(f"Error on socket: {e}")
            sys.exit(1)

        except KeyboardInterrupt as e:
            event = SystemEvent()
            self._event_store.register_action(event)
            event.log_info("Requested close by keyboard interrupt signal")
            self.stop()
            self.exit()

        event = SystemEvent()
        self._event_store.register_action(event)
        event.log_info("HTTPS server did quit")

    def serve_wss(self):
        """Start a web socket server over SSL.
        Used by the web UI to get notifications about updates."""
        # Start a web socket server if the web UI is enabled
        if not self._config["web-ui-enabled"]:
            return

        if not self._config["wss-enabled"]:
            return

        try:
            from autobahn.twisted.websocket import (
                WebSocketServerFactory,
            )
            from twisted.internet import reactor, ssl as twisted_ssl
            from twisted.internet.error import BindError

            # Create a WebSocketClientHandler instance
            WebSocketClientHandler = websocket_client_handler_factory(
                self._config, self._ws_clients, self._event_store, self._server_status
            )

            uri = f"ws://{self._config["wss-host"]}:{self._config["wss-port"]}"
            local_uri = f"wss://{self._config["wss-host"]}:{self._config["wss-port"]}"
            factory = WebSocketServerFactory(uri)
            factory.protocol = WebSocketClientHandler
            # factory.setProtocolOptions(maxConnections=2)
            public_uri = self._config["ws-public-uri"]
            if self._config["ws-always-ssl"]:
                if not os.path.isfile(self._config["ssl-cert"]):
                    self._startup_event.log_critical(
                        f"Unable to activate SSL: File does not exist: {self._config["ssl-cert"]}"
                    )
                    return

                # note to self: if using putChild, the child must be bytes...
                if self._config["ssl-key"] and self._config["ssl-cert"]:
                    context_factory = twisted_ssl.DefaultOpenSSLContextFactory(
                        privateKeyFileName=self._config["ssl-key"],
                        certificateFileName=self._config["ssl-cert"],
                    )
                else:
                    context_factory = twisted_ssl.DefaultOpenSSLContextFactory(
                        privateKeyFileName=self._config["ssl-cert"],
                        certificateFileName=self._config["ssl-cert"],
                    )

                self._ws_server_port = reactor.listenSSL(
                    self._config["wss-port"], factory, context_factory
                )
            else:
                self._ws_server_port = reactor.listenTCP(
                    self._config["wss-port"], factory
                )
                local_uri = local_uri.replace("wss://", "ws://")

            self._server_status["wss-uri"] = public_uri or local_uri
            self._startup_event.log_info(
                f"Listening for connections on {self._server_status["wss-uri"]}"
            )
            self._startup_event.ws_address = self._config["wss-host"]
            self._startup_event.ws_port = self._config["wss-port"]
            self._startup_event.set_ws_started(True)

            # Serve forever (until reactor.stop())
            reactor.run(installSignalHandlers=False)

        except ImportError as import_error:
            self._startup_event.log_error(
                "Unable to start web socket server due to missing dependency: "
                + import_error.msg
            )

        except BindError as e:
            self._startup_event.log_critical(f"Unable to start web socket server: {e}")

        event = SystemEvent()
        self._event_store.register_action(event)
        event.log_info("WSS server did quit")

    def serve_forever(self):
        """Start HTTP and web socket servers."""

        # Notify the event that we expect the http server to be started
        self._startup_event.http_started = False

        # Add script dir to sys path, allowing us to import sub modules even after changing cwd
        sys.path.insert(1, os.path.dirname(os.path.realpath(__file__)))

        # Set CWD to public www folder.
        # This makes the http server serve files from the wwwroot directory.
        wwwroot = os.path.join(os.path.dirname(os.path.realpath(__file__)), "wwwroot")
        os.chdir(wwwroot)

        threads = [
            # HTTP server
            threading.Thread(target=self.serve_http),
            # HTTPS server
            threading.Thread(target=self.serve_https),
            # Web socket SSL server
            threading.Thread(target=self.serve_wss),
        ]

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for each thread to finish
        for thread in threads:

            # Wait for thread to finish without blocking main thread
            while thread.is_alive():
                thread.join(5)

    def signal_handler(self, signum, _frame):
        """Signal handler for SIGHUP and SIGINT signals"""

        self.stop()

        event = SystemEvent()
        self._event_store.register_action(event)

        match signum:
            case signal.SIGHUP:
                # Reload configuration on SIGHUP events (conventional for daemon processes)
                self.setup(self._config)
                self.serve_forever()
                return
            case signal.SIGINT:
                event.log_info(
                    f"Recieved keyboard interrupt signal ({signum}) from the OS, shutting down."
                )
            case signal.SIGTERM:
                event.log_info(
                    f"Received termination signal ({signum}) from the OS, shutting down."
                )
            case _:
                event.log_info(
                    f"Recieved signal ({signum}) from the OS, shutting down."
                )

        self.exit()

    def stop(self):
        """Stop all running TCP servers (HTTP and web socket servers)"""

        # Stop HTTP server if running
        if self._http_server is not None:

            # Shut down the underlying TCP server
            self._http_server.shutdown()

            # Close the socket
            self._http_server.socket.close()

        # Stop HTTPS server if running
        if self._https_server is not None:

            # Shut down the underlying TCP server
            self._https_server.shutdown()

            # Close the socket
            self._https_server.socket.close()

        if self._https_server_unwrapped_socket is not None:

            self._https_server_unwrapped_socket.close()

        # Stop web socket server if running
        try:
            from twisted.internet import reactor

            reactor.callFromThread(reactor.stop)
        except ImportError:
            pass

    def exit(self):
        """Exit the application"""
        logger = logging.getLogger()
        logger.info("Goodbye")

        # Delete PID file
        self.remove_pid_file()

        # Restore stdin and stdout
        if "intercept-stdout" in self._config and self._config["intercept-stdout"]:
            sys.stdout = self._default_stdout
            sys.stderr = self._default_stderr


def main():
    """Entry point"""
    logger = logging.getLogger()

    app = GitAutoDeploy()

    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, app.signal_handler)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, app.signal_handler)
    if hasattr(signal, "SIGABRT"):
        signal.signal(signal.SIGABRT, app.signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, app.signal_handler)
    if hasattr(signal, "SIGPIPE") and hasattr(signal, "SIG_IGN"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    # Get default config values
    config = get_config_defaults()

    # Get config values from environment variables and commadn line arguments
    environment_config = get_config_from_environment()
    argv_config = get_config_from_argv(sys.argv[1:])

    # Merge config values from environment variables
    config.update(environment_config)

    search_target = os.path.dirname(os.path.realpath(__file__))
    config_file_path = get_config_file_path(
        environment_config, argv_config, search_target
    )

    # Config file path provided or found?
    if config_file_path:

        try:
            file_config = get_config_from_file(config_file_path)
        except ConfigFileNotFoundException as e:
            app.setup_console_logger()
            logger.critical("No config file not found at '%s'", e)
            return
        except ConfigFileInvalidException as e:
            app.setup_console_logger()
            logger.critical(
                "Unable to read config file due to invalid JSON format in '%s'", e
            )
            return

        # Merge config values from config file (overrides environment variables)
        config.update(file_config)

    # Merge config value from command line (overrides environment variables and config file)
    config.update(argv_config)

    # Rename legacy config option names
    config = rename_legacy_attribute_names(config)

    # Extend config data with any repository defined by environment variables
    repo_config = get_repo_config_from_environment()

    if repo_config:

        if "repositories" not in config:
            config["repositories"] = []

        config["repositories"].append(repo_config)

    # Initialize config by expanding with missing values
    init_config(config)

    app.setup(config)
    app.serve_forever()
