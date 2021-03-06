import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import ray
from ray.utils import binary_to_hex

_default_handler = None


def setup_logger(logging_level, logging_format):
    """Setup default logging for ray."""
    logger = logging.getLogger("ray")
    if type(logging_level) is str:
        logging_level = logging.getLevelName(logging_level.upper())
    logger.setLevel(logging_level)
    global _default_handler
    if _default_handler is None:
        _default_handler = logging.StreamHandler()
        logger.addHandler(_default_handler)
    _default_handler.setFormatter(logging.Formatter(logging_format))
    # Setting this will avoid the message
    # is propagated to the parent logger.
    logger.propagate = False


"""
All components underneath here is used specifically for the default_worker.py.
"""


class StandardStreamInterceptor:
    """Used to intercept stdout and stderr.

    Intercepted messages are handled by the given logger.

    NOTE: The logger passed to this method should always have
          logging.INFO severity level.

    Example:
        >>> from contextlib import redirect_stdout
        >>> logger = logging.getLogger("ray_logger")
        >>> hook = StandardStreamHook(logger)
        >>> with redirect_stdout(hook):
        >>>     print("a") # stdout will be delegated to logger.

    Args:
        logger: Python logger that will receive messages streamed to
                the standard out/err and delegate writes.
        intercept_stdout(bool): True if the class intercepts stdout. False
                         if stderr is intercepted.
    """

    def __init__(self, logger, intercept_stdout=True):
        self.logger = logger
        assert len(self.logger.handlers) == 1, (
            "Only one handler is allowed for the interceptor logger.")
        self.intercept_stdout = intercept_stdout

    def write(self, message):
        """Redirect the original message to the logger."""
        self.logger.info(message)
        return len(message)

    def flush(self):
        for handler in self.logger.handlers:
            handler.flush()

    def isatty(self):
        # Return the standard out isatty. This is used by colorful.
        fd = 1 if self.intercept_stdout else 2
        return os.isatty(fd)

    def close(self):
        handler = self.logger.handlers[0]
        handler.close()

    def fileno(self):
        handler = self.logger.handlers[0]
        return handler.stream.fileno()


class StandardFdRedirectionRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that redirects stdout and stderr to the log file.

    It is specifically used to default_worker.py.

    The only difference from this handler vs original RotatingFileHandler is
    that it actually duplicates the OS level fd using os.dup2.
    """

    def __init__(self,
                 filename,
                 mode="a",
                 maxBytes=0,
                 backupCount=0,
                 encoding=None,
                 delay=False,
                 is_for_stdout=True):
        super().__init__(
            filename,
            mode=mode,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay)
        self.is_for_stdout = is_for_stdout
        self.switch_os_fd()

    def doRollover(self):
        super().doRollover()
        self.switch_os_fd()

    def get_original_stream(self):
        if self.is_for_stdout:
            return sys.stdout
        else:
            return sys.stderr

    def switch_os_fd(self):
        # Old fd will automatically closed by dup2 when necessary.
        os.dup2(self.stream.fileno(), self.get_original_stream().fileno())


def setup_and_get_worker_interceptor_logger(args,
                                            max_bytes=0,
                                            backup_count=0,
                                            is_for_stdout: bool = True):
    """Setup a logger to be used to intercept worker log messages.

    NOTE: This method is only meant to be used within default_worker.py.

    Ray worker logs should be treated in a special way because
    there's a need to intercept stdout and stderr to support various
    ray features. For example, ray will prepend 0 or 1 in the beggining
    of each log message to decide if logs should be streamed to driveres.

    This logger will also setup the RotatingFileHandler for
    ray workers processes.

    If max_bytes and backup_count is not set, files will grow indefinitely.

    Args:
        args: args received from default_worker.py.
        max_bytes(int): maxBytes argument of RotatingFileHandler.
        backup_count(int): backupCount argument of RotatingFileHandler.
        is_for_stdout(bool): True if logger will be used to intercept stdout.
                             False otherwise.
    """
    file_extension = "out" if is_for_stdout else "err"
    logger = logging.getLogger(f"ray_default_worker_{file_extension}")
    if len(logger.handlers) == 1:
        return logger
    logger.setLevel(logging.INFO)
    # TODO(sang): This is how the job id is propagated to workers now.
    # But eventually, it will be clearer to just pass the job id.
    job_id = os.environ.get("RAY_JOB_ID")
    if args.worker_type == "WORKER":
        assert job_id is not None, (
            "RAY_JOB_ID should be set as an env "
            "variable within default_worker.py. If you see this error, "
            "please report it to Ray's Github issue.")
        worker_name = "worker"
    else:
        job_id = ray.JobID.nil()
        worker_name = "io_worker"

    # Make sure these values are set already.
    assert ray.worker._global_node is not None
    assert ray.worker.global_worker is not None
    filename = (f"{ray.worker._global_node.get_session_dir_path()}/logs/"
                f"{worker_name}-"
                f"{binary_to_hex(ray.worker.global_worker.worker_id)}-"
                f"{job_id}-{os.getpid()}.{file_extension}")
    handler = StandardFdRedirectionRotatingFileHandler(
        filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        is_for_stdout=is_for_stdout)
    logger.addHandler(handler)
    # TODO(sang): Add 0 or 1 to decide whether
    # or not logs are streamed to drivers.
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Avoid messages are propagated to parent loggers.
    logger.propagate = False
    # Remove the terminator. It is important because we don't want this
    # logger to add a newline at the end of string.
    handler.terminator = ""
    return logger
