# Repository Security and Data Policy

## Never commit

- `.env`, API keys, access tokens, passwords, cookies, private certificates
- local or production databases and backups
- real customer/order/procurement data
- internal enterprise knowledge files unless explicitly sanitized and approved for publication
- local virtual environments, caches, logs, temporary reports

## Allowed portfolio data

- deterministic synthetic/mock product data
- anonymized evaluation cases
- generated demo screenshots after manual privacy review
- `.env.example` with blank secret fields

## Secret incident rule

If a secret is committed even once, treat it as exposed: rotate/revoke it first, then remove it from Git history. A later deletion commit does not remove the old secret from history.
