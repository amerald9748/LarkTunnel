---
title: Authentication
tags: [setup]
---

# Authentication

The wrappers shell out to **`lark-cli`**, which must be installed and logged in.

## Prerequisites

- Node.js (for the wrapper library and scripts).
- `lark-cli` on PATH. Check:
  ```bash
  lark-cli --version
  ```
- App credentials live in `config/secrets.txt` (gitignored). They identify the
  Lark app used for API access.

## Logging in

The wrappers run as `--as user`. Make sure the CLI has a valid **user** session:

```bash
lark-cli auth login
```

If you hit `permission denied` or scope errors, see the `lark-shared` skill
guidance — it covers `auth login`, switching identity with `--as`, and scope
fixes.

## How identity flows through the tool

- `config.js` sets `actAs: 'user'`.
- Every wrapper call appends `--as user`.
- Nothing in this repo stores or transmits your password; `lark-cli` owns the
  session.

> [!note]
> `config/secrets.txt` contains the App ID / App Secret. Keep it out of version
> control (already in `.gitignore`). Do not paste the secret into chat, docs, or
> commits.

Related: [[Configuration]] · [[Production Guardrails]]
