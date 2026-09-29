# Contributing

## Development setup

Use Python 3.10 or newer. Install the test runner and run the same offline gate
used by GitHub Actions:

```bash
python -m pip install -r requirements.txt
python tools/verify_offline.py
```

The offline suite uses committed synthetic fixtures. It does not need Slay the
Spire, a game save, or model-provider credentials. Keep new unit tests independent
of local game installs and external services.

## Changes

- Include a focused test or fixture when changing behavior.
- Update the relevant guide when a command, configuration setting, or user workflow changes.
- Do not commit `.env` files, API keys, game saves, logs containing personal data, or game binaries.
- Keep runtime behavior deterministic when provider or game integrations are unavailable.

## Reporting issues

Include the operating system, Python version, command used, expected behavior,
actual behavior, and a short sanitized error excerpt. Remove API keys, tokens,
save data, and private paths before posting logs.
