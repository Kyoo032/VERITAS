# Security Policy

## Scope

This policy covers the `supgate` CLI in this repository. The security-critical
guarantees of the tool itself:

- API keys are **env-only** — never accepted on the command line, never
  persisted. Artifacts carry only the env-var name and a one-way SHA-256
  fingerprint (`key_env` / `key_fingerprint`).
- Every persisted artifact (run bundles, evidence exchanges, baseline records,
  SQLite history rows) passes through the redaction choke point in
  `supgate/evidence.py` before it is written.

If you find a way to make a key, token, or other secret survive into a run
bundle, evidence file, baseline record, history row, or CLI log output, that is
a security report — please tell us privately.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/Kyoo032/VERITAS/security/advisories/new)
rather than opening a public issue. Include:

1. The commit or release you tested.
2. A minimal reproduction (endpoint behavior, command, or input).
3. Which artifact the secret reached (bundle JSON, evidence file, history DB,
   terminal output) and what should have redacted it.

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | yes |
