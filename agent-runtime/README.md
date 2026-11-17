# ATI Trading System · Local Agent client

The one-click installer installs this checksummed wheel only inside an isolated
Docker image, alongside the selected Codex or Claude CLI. Docker is required;
Git, trading, research and access to the private orchestration repository are not.
Only `httpx` and its public dependencies are resolved by pip inside the image.
No host Agent virtualenv, model CLI, LaunchAgent or systemd service is installed.

This distribution contains the reviewed outbound connector, tools-off inference
adapters, native session transports/journal, Codex and Claude session adapters,
and the Agent-container task scheduler. The scheduler stores its own jobs and
native turn receipts in the Agent journal; it never creates trading-system
strategy entities or uses a Copilot scheduler. Saving a task leaves it paused.
Unknown native send outcomes are not automatically replayed.
It contains no orchestration server, Studio service, SDK, platform prompt
library, configuration, or credentials.
`manifest.json` records the source revision, source hashes, and wheel checksum.
The connector and agent execution logic are preserved without reimplementation.

Maintainers rebuild from clean, reviewed native sources with
`python3 scripts/build_agent_client.py --source ../ati-worker-orchestrator --output agent-runtime`.
Review every source file in the explicit inventory before publishing a new client.

For an operator-verified origin behind an unreliable WebSocket proxy, set
`ATI_AGENT_REVERSE_ORIGIN_IP` when running the Agent installer or upgrade.
This changes only the TCP destination: the original WSS URL, HTTP Host, TLS
hostname/certificate verification, device proof and product grants stay in use.
The installer retains this setting in `.agent.env`; an explicitly empty value
restores normal DNS routing. No insecure TLS fallback is provided.
