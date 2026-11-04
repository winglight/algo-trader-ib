# ATI Algotrading System · Local Agent client

The one-click installer installs this checksummed wheel only inside an isolated
Docker image, alongside the selected Codex or Claude CLI. Docker is required;
Git, trading, research and access to the private orchestration repository are not.
Only `httpx` and its public dependencies are resolved by pip inside the image.
No host Agent virtualenv, model CLI, LaunchAgent or systemd service is installed.

This distribution contains exactly the native outbound `local_connector.py`,
`codex_agent_adapter.py`, `claude_agent_adapter.py`, and the task lease guard `local_task_process.py`, with
an inert package initializer. It contains no orchestration server, Studio service,
SDK, platform prompt library, configuration, or credentials.
`manifest.json` records the source revision, source hashes, and wheel checksum.
The connector and agent execution logic are preserved without reimplementation.

Maintainers rebuild from clean, reviewed native sources with
`python3 scripts/build_agent_client.py --source ../ati-worker-orchestrator --output agent-runtime`.
Review the four source files and inventory before publishing a new client.
