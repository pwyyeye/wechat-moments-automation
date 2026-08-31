# Windows Agent deployment and rollback

## Build

Run `powershell -ExecutionPolicy Bypass -File scripts/build-agent.ps1`. The build is a PyInstaller `onedir` directory at `dist/WechatPublisherAgent`; it keeps native DLLs and OCR assets beside the executable instead of unpacking them on every launch.

Compile `packaging/installer.iss` with Inno Setup after the onedir build. The installer targets the current user, registers a Task Scheduler `ONLOGON` task, and starts the Agent with `--agent`. It deliberately does not install a Windows Service because Session 0 cannot operate the signed-in user's WeChat desktop.

## Data and credentials

- Non-sensitive configuration: `%LOCALAPPDATA%\WechatPublisherAgent\config.yaml`
- SQLite ledger and Outbox: `%LOCALAPPDATA%\WechatPublisherAgent\data\agent.db`
- DPAPI-encrypted source credentials: `%LOCALAPPDATA%\WechatPublisherAgent\credentials`
- Rotating logs: `%LOCALAPPDATA%\WechatPublisherAgent\logs`

For unattended deployment, place a protected `agent-bootstrap.json` beside the
setup EXE. The installer copies it to the data directory; the Agent validates
the bundle, saves every source credential with current-user DPAPI, updates
`config.yaml`, and removes the copied plaintext file. The original sidecar next
to the installer remains the deployer's responsibility and must be distributed
through a secret-capable channel and removed after deployment. Never commit it.
Use `packaging/agent-bootstrap.example.json` as the schema template.

Without a bootstrap bundle, the default source is created as `unconfigured`.
This is intentional: the Agent never invents a random Bearer token. Enter a
valid key in the native Windows control panel before enabling production polling.

The uninstaller removes the executable and login task but preserves the data directory. Check the native control panel and make sure the Outbox backlog is zero before intentionally deleting that directory.

## Upgrade

1. Confirm the local Outbox backlog is zero or the source credentials remain available.
2. Keep the entire `%LOCALAPPDATA%\WechatPublisherAgent` directory.
3. Install the newer onedir build over the prior version.
4. Open **Wechat Publisher Agent** from the Start menu, verify Agent and WeChat versions, then test each source.
5. Run the no-click environment preflight before allowing a production task.

Protocol v1 permits additive optional fields. An Agent must reject an unknown required protocol version but ignore unknown optional fields.

## Local shutdown

Agent 0.3.4 and later distinguish a real claimed publish task from local
preflight or WeChat identity recognition. A claimed publish task blocks safe
shutdown so the final-click ledger cannot be interrupted. Local recognition
and preflight do not block shutdown. The native control panel immediately stops
new polling, asks Uvicorn to drain, and uses an eight-second watchdog to terminate
the process if a native OCR/UIA call cannot return. The watchdog does not stop
or log out WeChat and does not delete the local ledger or DPAPI credentials.

Agent 0.4.0 and later use a native Windows GUI for all operator actions. The
loopback HTTP endpoint remains bound to `127.0.0.1` for internal GUI-to-Agent
communication and diagnostics only; normal startup never opens a browser or
embeds a web view.

## Local logs

Agent 0.4.1 and later expose **View error logs** in the native control panel.
The viewer defaults to ERROR and CRITICAL records and supports severity and
keyword filters, refresh, copy, and opening the local log directory. It reads
only `agent.log` and its rotating backups, groups multiline tracebacks with the
originating record, and returns at most 1,000 records from the loopback API.
No arbitrary filesystem path is accepted from the client.

## Rollback

1. Stop the Agent from Task Manager or delete the login task with `scripts/remove-startup.ps1`.
2. Reinstall the last known-good onedir build.
3. Do not restore an older copy of `agent.db`; use the current ledger so final-click intent and pending Outbox events are retained.
4. Re-register startup and verify that any task found after `final_click_intent` enters manual review without another click.

The supported operating-system floor is Windows 10/11 x64. Window discovery is
based on top-level windows and the running WeChat process rather than a fixed
WeChat version number. UI changes can still affect controls and OCR, so every
new WeChat build must pass the no-click preflight and one explicitly authorized
test post before broad rollout.
