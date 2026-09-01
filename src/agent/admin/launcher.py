from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence


def ensure_agent_backend(
    client,
    *,
    expected_version: str,
    command: Sequence[str],
    working_directory: str,
    popen: Callable[..., object] = subprocess.Popen,
    initial_wait: float = 1.5,
    shutdown_wait: float = 15.0,
) -> bool:
    """Reuse the current backend or replace an older version before opening the UI."""
    ready = client.wait_until_ready(initial_wait)
    if ready:
        status = client.status()
        running_version = status.get("agent", {}).get("version")
        if not running_version or running_version == expected_version:
            return False

        try:
            client.shutdown()
        except Exception as error:
            raise RuntimeError(
                f"检测到旧版 Agent {running_version}，但无法安全退出：{error}"
            ) from error
        if not client.wait_until_stopped(shutdown_wait):
            raise RuntimeError(
                f"旧版 Agent {running_version} 未能在 {shutdown_wait:g} 秒内退出，请先结束发布任务后重试。"
            )

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    popen(
        list(command),
        cwd=working_directory,
        creationflags=creation_flags,
        close_fds=True,
    )
    return True
