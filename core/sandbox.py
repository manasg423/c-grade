import subprocess
import sys

try:
    import resource as _resource
    _HAS_RESOURCE = True
except ImportError:
    _resource = None  # type: ignore[assignment]
    _HAS_RESOURCE = False


def _set_limits(memory_mb: int | None):
    if not _HAS_RESOURCE or memory_mb is None or memory_mb <= 0:
        return
    bytes_limit = memory_mb * 1024 * 1024
    try:
        _resource.setrlimit(_resource.RLIMIT_AS, (bytes_limit, bytes_limit))
    except Exception:
        pass


def run_safe(cmd: list, stdin_data: str = "", timeout_s: float = 1.0, memory_mb: int | None = 64) -> dict:
    preexec = (lambda: _set_limits(memory_mb)) if sys.platform != "win32" else None

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            preexec_fn=preexec,
        )
        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
            "timed_out": False,
            "error": None
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Process timed out after {timeout_s} seconds.",
            "returncode": -1,
            "timed_out": True,
            "error": "TIMEOUT"
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
            "timed_out": False,
            "error": str(e)
        }
