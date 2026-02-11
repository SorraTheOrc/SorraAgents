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
