from __future__ import annotations

import re
from collections import deque
from pathlib import Path


LOG_RECORD = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} [\d:,]+) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) "
    r"(?P<logger>\S+) (?P<message>.*)$"
)
LEVEL_VALUES = {
    "ALL": 0,
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def read_recent_logs(
    log_directory: Path,
    *,
    level: str = "ERROR",
    limit: int = 200,
    query: str = "",
) -> list[dict[str, str]]:
    """Read bounded, structured records from the Agent's rotating log files."""
    minimum = LEVEL_VALUES.get(level.upper())
    if minimum is None:
        raise ValueError(f"unsupported log level: {level}")
    if limit < 1:
        return []

    records: deque[dict[str, str]] = deque(maxlen=limit)
    search = query.casefold().strip()
    current: dict[str, str] | None = None

    def append_current() -> None:
        nonlocal current
        if current is None:
            return
        if (
            LEVEL_VALUES[current["level"]] >= minimum
            and (not search or search in current["raw"].casefold())
        ):
            records.append(current)
        current = None

    for path in _ordered_log_files(log_directory):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for raw_line in stream:
                    line = raw_line.rstrip("\r\n")
                    match = LOG_RECORD.match(line)
                    if match:
                        append_current()
                        current = {
                            "timestamp": match.group("timestamp"),
                            "level": match.group("level"),
                            "logger": match.group("logger"),
                            "message": match.group("message"),
                            "raw": line,
                        }
                    elif current is not None:
                        current["raw"] += f"\n{line}"
                        current["message"] += f"\n{line}"
        except OSError:
            # Rotation can rename a file between discovery and opening it.
            continue
        append_current()

    return list(records)


def _ordered_log_files(log_directory: Path) -> list[Path]:
    if not log_directory.exists():
        return []

    def chronological_key(path: Path) -> int:
        if path.name == "agent.log":
            return 0
        try:
            return -int(path.name.rsplit(".", 1)[1])
        except (IndexError, ValueError):
            return -1000

    return sorted(log_directory.glob("agent.log*"), key=chronological_key)
