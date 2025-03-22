"""Process wrapper module for gitautodeploy."""

import logging
from subprocess import PIPE, Popen


class ProcessWrapper:
    """Wraps the subprocess popen method and provides logging."""

    def __init__(self):
        pass

    @staticmethod
    def call(*popenargs, **kwargs):
        """Run command with arguments. Wait for command to complete. Sends
        output to logging module. The arguments are the same as for the Popen
        constructor."""

        logger = logging.getLogger()

        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE

        suppress_stderr = None
        if "supressStderr" in kwargs:
            suppress_stderr = kwargs["supressStderr"]
            del kwargs["supressStderr"]

        p = Popen(*popenargs, **kwargs)
        stdout, stderr = p.communicate()

        # Decode bytes to string (assume utf-8 encoding)
        stdout = stdout.decode("utf-8")
        stderr = stderr.decode("utf-8")

        if stdout:
            for line in stdout.strip().split("\n"):
                logger.info(line)

        if stderr:
            for line in stderr.strip().split("\n"):
                if suppress_stderr:
                    logger.info(line)
                else:
                    logger.error(line)

        return p.returncode
