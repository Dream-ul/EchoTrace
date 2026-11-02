# Contributing

Thanks for helping improve EchoTrace. Keep pull requests focused and include a test or a clear manual verification note for behavior changes.

## Development setup

1. Follow the local development setup in [README.md](README.md).
2. Run `python -m pytest -q` and `python -m compileall -q echotrace app` for backend changes.
3. Run `npm ci` and `npm run build` from `frontend/` for frontend changes.

Tests must not depend on real API keys, private datasets, or external investigation records. Never commit `.env`, logs, reports containing private data, credentials, or local planning documents from `docs/`.

## Pull requests

- Describe the user-visible behavior and the reason for the change.
- Include focused tests for bug fixes and new behavior.
- Update user documentation when setup or configuration changes.
- Keep generated output and unrelated formatting changes out of the diff.
