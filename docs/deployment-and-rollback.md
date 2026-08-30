# Windows Agent deployment and rollback

## Build

Run `powershell -ExecutionPolicy Bypass -File scripts/build-agent.ps1`. The build is a PyInstaller `onedir` directory at `dist/WechatPublisherAgent`; it keeps native DLLs and OCR assets beside the executable instead of unpacking them on every launch.

Compile `packaging/installer.iss` with Inno Setup after the onedir build. The installer targets the current user, registers a Task Scheduler `ONLOGON` task, and starts the Agent with `--agent`. It deliberately does not install a Windows Service because Session 0 cannot operate the signed-in user's WeChat desktop.

## Data and credentials

- Non-sensitive configuration: `%LOCALAPPDATA%\WechatPublisherAgent\config.yaml`
- SQLite ledger and Outbox: `%LOCALAPPDATA%\WechatPublisherAgent\data\agent.db`
- DPAPI-encrypted source credentials: `%LOCALAPPDATA%\WechatPublisherAgent\credentials`
- Rotating logs: `%LOCALAPPDATA%\WechatPublisherAgent\logs`

The uninstaller removes the executable and login task but preserves the data directory. Check the local admin page and make sure the Outbox backlog is zero before intentionally deleting that directory.

## Upgrade

1. Confirm the local Outbox backlog is zero or the source credentials remain available.
2. Keep the entire `%LOCALAPPDATA%\WechatPublisherAgent` directory.
3. Install the newer onedir build over the prior version.
4. Open `http://127.0.0.1:17821`, verify Agent and WeChat versions, then test each source.
5. Run the no-click environment preflight before allowing a production task.

Protocol v1 permits additive optional fields. An Agent must reject an unknown required protocol version but ignore unknown optional fields.

## Rollback

1. Stop the Agent from Task Manager or delete the login task with `scripts/remove-startup.ps1`.
2. Reinstall the last known-good onedir build.
3. Do not restore an older copy of `agent.db`; use the current ledger so final-click intent and pending Outbox events are retained.
4. Re-register startup and verify that any task found after `final_click_intent` enters manual review without another click.

The compatibility floor for the first release is Windows 11 x64 and WeChat Desktop 4.1.13.12. Any new WeChat build must pass the no-click preflight and one explicitly authorized test post before broad rollout.
