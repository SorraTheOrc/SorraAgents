# Examples

## terminal_conversation.py

Simple terminal-based conversation client using the OpenCode Python SDK.

```sh
pip install --pre opencode-ai
python examples/terminal_conversation.py
```

Environment variables:

- `OPENCODE_BASE_URL` (optional): override the API base URL
- `OPENCODE_BASE_URL` is required if you are not running the API on the default host.
- `OPENCODE_PROVIDER_ID` (optional): default provider ID (defaults to `anthropic`)
- `OPENCODE_MODEL_ID` (optional): default model ID (defaults to `claude-3-5-sonnet-20241022`)

## waiting_for_input_notification.py

Trigger a waiting_for_input notification to Discord/AMPA.

```sh
AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." \
AMPA_RESPONDER_URL="http://localhost:8081/respond" \
python examples/waiting_for_input_notification.py
```

Environment variables:

- `AMPA_DISCORD_WEBHOOK` (required): Discord webhook for notifications.
- `AMPA_RESPONDER_URL` (required): responder endpoint URL shown in the notification.
- `AMPA_EXAMPLE_WORK_ITEM` (optional): work item id shown in the message (default `WL-EXAMPLE`).
- `AMPA_TOOL_OUTPUT_DIR` (optional): override tool-output directory for pending prompt files.

Verify in Discord/AMPA that the notification includes Session, Work item, Reason,
Pending prompt file path, and Responder endpoint.
