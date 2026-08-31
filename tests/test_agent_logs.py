from src.agent.admin.logs import read_recent_logs


def test_log_reader_filters_errors_and_keeps_tracebacks(tmp_path):
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "agent.log").write_text(
        "2026-08-31 10:00:00,000 INFO source claim started\n"
        "2026-08-31 10:00:01,000 ERROR source SOURCE_AUTH_FAILED\n"
        "Traceback (most recent call last):\n"
        "  RuntimeError: unauthorized\n"
        "2026-08-31 10:00:02,000 WARNING worker retrying\n",
        encoding="utf-8",
    )

    entries = read_recent_logs(log_directory, level="ERROR")

    assert len(entries) == 1
    assert entries[0]["level"] == "ERROR"
    assert "SOURCE_AUTH_FAILED" in entries[0]["message"]
    assert "RuntimeError: unauthorized" in entries[0]["raw"]


def test_log_reader_searches_rotated_files_and_applies_limit(tmp_path):
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (log_directory / "agent.log.1").write_text(
        "2026-08-30 10:00:00,000 ERROR source old failure\n",
        encoding="utf-8",
    )
    (log_directory / "agent.log").write_text(
        "2026-08-31 10:00:00,000 WARNING worker target retry\n"
        "2026-08-31 10:00:01,000 ERROR source target failure\n",
        encoding="utf-8",
    )

    entries = read_recent_logs(
        log_directory,
        level="WARNING",
        limit=1,
        query="target",
    )

    assert [entry["message"] for entry in entries] == ["target failure"]
