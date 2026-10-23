"""Bounded diagnostics: never expose exception messages or dynamic class names."""

from google.api_core.exceptions import GoogleAPICallError
from paramiko import AuthenticationException, SSHException

STEPS = frozenset(
    {"config", "bootstrap", "sftp_auth", "sftp_listing", "staging", "upload", "verify", "markers"}
)
ERROR_TYPES = (
    AuthenticationException,
    SSHException,
    GoogleAPICallError,
    InterruptedError,
    TimeoutError,
    PermissionError,
    FileNotFoundError,
    OSError,
    KeyError,
    TypeError,
    ValueError,
    RuntimeError,
)


def reason(step: str, exc: Exception) -> dict[str, str]:
    if isinstance(exc, TransferFailure):
        return {"step": exc.step, "error_class": exc.error_class}
    return {
        "step": step if step in STEPS else "unknown",
        "error_class": next(
            (kind.__name__ for kind in ERROR_TYPES if isinstance(exc, kind)), "UnknownError"
        ),
    }


class TransferFailure(RuntimeError):
    def __init__(self, step: str, exc: Exception):
        detail = reason(step, exc)
        self.step, self.error_class = detail["step"], detail["error_class"]
        super().__init__(f"Transfer failed during {self.step} ({self.error_class})")
