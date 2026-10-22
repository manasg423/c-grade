import subprocess
import resource

def _set_limits(memory_mb: int | None):
    """Restricts the memory of the running process so it cannot crash the system."""
    if memory_mb is None or memory_mb <= 0:
        return
    bytes_limit = memory_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
    except Exception:
        pass

def run_safe(cmd: list, stdin_data: str = "", timeout_s: float = 1.0, memory_mb: int | None = 64) -> dict:
    """
    Runs a binary/command safely under timeout and memory caps.
    Returns: stdout, stderr, returncode, timed_out, error
    """
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            preexec_fn=lambda: _set_limits(memory_mb)
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