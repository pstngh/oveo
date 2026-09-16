# Oveo v2.1

Oveo is a private, two-account professional language adviser with three persistent
sections:

- **Translate** translates between French and US English, Canadian French,
  France French, or International French.
- **Revision** proofreads, copyedits, revises, or rewrites text that already
  exists without translating it.
- **Internal communications** drafts new employee-facing communications from a
  brief, notes, facts, and constraints in Alithya's `Comm internes` house style.

Each section is a professional copilot with its own scope and locale semantics.
The interface has no voice selector; section choice determines behavior.

## Local development

Requirements: Python 3.13 (3.12+ supported), Node.js 22+, `uv`, and SQLite.

```bash
cp .env.example .env
# Set development password hashes and, for real model calls, OVEO_OPENROUTER_API_KEY.
uv sync --frozen --extra dev
uv run --frozen alembic upgrade head
cd frontend && npm ci && npm run build && cd ..
uv run --frozen uvicorn oveo.main:app --reload
```

Open `http://127.0.0.1:8000`. The backend serves the compiled frontend. For
frontend hot reload, run `npm run dev` in `frontend/`; Vite proxies API requests
to port 8000.

Generate Argon2id password hashes without putting a plaintext password in shell
history:

```bash
uv run --frozen oveo-admin hash-password
```

## Prompt maintenance

Prompt ownership is intentionally explicit:

- [`prompts/translate.md`](prompts/translate.md),
  [`prompts/revision.md`](prompts/revision.md), and
  [`prompts/internal_communications.md`](prompts/internal_communications.md)
  independently own their section's scope, intake, locale selection, preservation,
  redirects, canonical meanings, and advisory triggers.
- [`prompts/alithya_rules.md`](prompts/alithya_rules.md) is the single shared place
  for authorized terminology, brand/naming, locale conventions, official names,
  protected content, and the compact professional-adviser posture.
- [`prompts/protocol.md`](prompts/protocol.md) owns only the typed NDJSON grammar,
  block mechanics, state schemas, validation rules, and operation preconditions.

A normal global terminology or language-rule change is one edit to
`alithya_rules.md`, plus focused tests and deployment. It does not require a schema
or UI change. New prompt rules apply when a future generation is composed,
including a future turn in an existing conversation. They never rewrite an
already-saved canonical work version.

## Compatibility

Persisted current modes are `translate`, `revision`, and `internal_comms`. Legacy
API input mode `alithyagpt` is accepted as an alias for `internal_comms`. The
obsolete `voice_key` field remains only as dormant database/API compatibility data
for older clients; it is not exposed as a choice and never selects prompt behavior.

Migration `20260916_0002` maps existing `alithyagpt` conversations to
`internal_comms` without changing IDs, messages, attachments, summaries, actors,
or canonical work. Downgrading maps both `revision` and `internal_comms` to the
legacy `alithyagpt`/`draft` representation because the old schema cannot represent
Revision independently. Rows and content survive, but that semantic distinction
cannot be recovered by a later re-upgrade.

## Quality gate

```bash
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy
uv run --frozen pytest
cd frontend
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

Migration verification additionally performs a fresh upgrade, `current`,
downgrade/re-upgrade preservation exercise, and an Alembic schema drift check.

## Configuration and runtime

All settings use the `OVEO_` prefix. See `.env.example`. Production requires the
two role-mapped Argon2id hashes, the OpenRouter key,
`OVEO_ENVIRONMENT=production`, the trusted public origin/host, secure cookies,
persistent data paths, and the fixed `openai/gpt-5.6-luna` privacy-eligible routing.

Conversation and handoff inputs are counted with Luna's tokenizer. Oveo compacts
at 240,000 input tokens by default, leaving a 32,000-token margin below Luna's
long-context pricing boundary. Oversized handoffs are reduced in user-only chunks;
assistant turns, internal summaries, canonical work, and application prompts never
enter those chunks.

Production uses one slim image and one Uvicorn worker behind host Caddy. SQLite
runs in WAL mode with foreign keys and a bounded busy timeout. Background calls
have durable generation rows and replayable snapshots, so navigation and browser
disconnects do not cancel them. See [ARCHITECTURE.md](ARCHITECTURE.md) and
[OPERATIONS.md](OPERATIONS.md).

The production application origin is `https://oveo.duckdns.org`. The direct VPS
IP is retained only as a secondary operations check, not as the user-facing URL.

Never commit `.env`, credentials, live databases, attachments, backup identities,
or decrypted backups.
