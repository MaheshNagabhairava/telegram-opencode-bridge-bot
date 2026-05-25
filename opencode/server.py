"""OpenCode server process manager.

Handles starting, stopping, and restarting the `opencode serve` process
deterministically — when the user switches projects via /project, the server
is restarted from the new project directory so the AI agent is fully scoped.
"""

import asyncio
import logging
import os
import signal
import subprocess

logger = logging.getLogger(__name__)

_server_process: subprocess.Popen | None = None
_server_port: int = 4096


def get_opencode_binary() -> str:
    """Find the opencode binary on the system."""
    path = os.environ.get("OPENCODE_BINARY", "")
    if path and os.path.isfile(path):
        return path
    import shutil
    found = shutil.which("opencode")
    if found:
        return found
    return "opencode"


async def restart_server(directory: str, port: int = 4096, hostname: str = "127.0.0.1") -> bool:
    """Stop the running opencode serve process and restart it from *directory*.

    Returns True if the server came up successfully, False otherwise.
    """
    global _server_process, _server_port
    _server_port = port

    # 1. Stop the existing server
    await stop_server()
    await asyncio.sleep(1)

    # 2. Start a new one from the target directory
    binary = get_opencode_binary()
    cmd = [binary, "serve", "--port", str(port), "--hostname", hostname]

    logger.info(f"Starting opencode serve from {directory}: {' '.join(cmd)}")

    try:
        _server_process = subprocess.Popen(
            cmd,
            cwd=directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.error(f"Failed to start opencode serve: {e}")
        return False

    # 3. Wait until the server is reachable (max 30s)
    import aiohttp
    for attempt in range(30):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{hostname}:{port}/session", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status < 500:
                        logger.info(f"opencode serve is up after {attempt + 1}s (pid={_server_process.pid})")
                        return True
        except Exception:
            pass

    logger.error("opencode serve did not become reachable within 30s")
    return False


async def stop_server() -> None:
    """Stop the running opencode serve process (if any)."""
    global _server_process

    if _server_process is not None and _server_process.poll() is None:
        logger.info(f"Stopping opencode serve (pid={_server_process.pid})")
        try:
            _server_process.terminate()
            try:
                _server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _server_process.kill()
                _server_process.wait(timeout=3)
        except Exception as e:
            logger.warning(f"Error stopping opencode serve: {e}")
        _server_process = None
    else:
        # Also kill any stray opencode serve processes on our port
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-ti", f":{_server_port}", "-sTCP:LISTEN",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        pids = stdout.decode().strip().split()
        for pid in pids:
            if pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    logger.info(f"Killed stray opencode serve pid={pid}")
                except ProcessLookupError:
                    pass
        await asyncio.sleep(0.5)
