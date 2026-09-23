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
- [`prompts/docx_protocol.md`](prompts/docx_protocol.md) is loaded only for a DOCX
  attachment or DOCX-backed canonical version and defines the fixed block-map and
  protected-hyperlink contract.

A normal global terminology or language-rule change is one edit to
`alithya_rules.md`, plus focused tests and deployment. It does not require a schema
or UI change. New prompt rules apply when a future generation is composed,
including a future turn in an existing conversation. They never rewrite an
already-saved canonical work version.

## Source attachments and DOCX export

Each user message accepts one standard `.docx` file, selected as either the source
document to transform or a style reference, subject to the configured byte and
word limits. DOCX uploads are validated as bounded, non-encrypted, non-macro OOXML
packages and extracted as stable paragraph and table-cell blocks. Tracked changes
and field-code hyperlinks are rejected because they cannot be rewritten safely in
v1. The latest style reference remains available after conversation compaction but
never becomes the canonical document or export template.

For DOCX-backed canonical work, Oveo preserves the uploaded package as the
template and stores a complete validated block replacement map on every committed
version. **Download DOCX** reproduces only the latest committed version by copying
the template package and patching `word/document.xml`; styles, numbering, tables,
images, headers, footers, relationships, margins, and section settings remain in
the original package. Paragraph order and table/list structure cannot change in
v1. Mixed inline formatting inside replaced ordinary text may collapse to the
formatting of its first original run, while protected hyperlinks keep their
original targets and may change display text.

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

Migration verification additionally performs a fresh upgrade, validates all three
current modes and work kinds, and runs an Alembic schema drift check.

## Configuration and runtime

All settings use the `OVEO_` prefix. See `.env.example`. Production requires the
two account-specific Argon2id hashes, the OpenRouter key,
`OVEO_ENVIRONMENT=production`, the trusted public origin/host, secure cookies,
persistent data paths, and the fixed `openai/gpt-6-luna` privacy-eligible routing.

Conversation and handoff inputs are estimated with `o200k_base` while `tiktoken`
does not map GPT-6 Luna by model name. Oveo compacts at 240,000 input tokens by
default, leaving a 32,000-token margin below Luna's
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

## Privacy and diagnostics

Charles and Yousra are equal accounts with strictly separate conversation,
generation, and attachment views. Neither account can access the other's conversation
data. The sidebar lifetime-cost figure is intentionally one overall Oveo total shown to
both accounts.
Requests for another account's conversation are returned as not found so record
identifiers cannot be used for discovery.

Browser traffic is encrypted in transit with HTTPS, passwords are stored as
Argon2id hashes, session and CSRF tokens are stored as hashes, and nightly backups
are encrypted with `age`. The live SQLite database and attachment files are not
application-level encrypted at rest; they rely on private host permissions and
server access controls.

Content-free error diagnostics are written to
`/data/logs/oveo-errors.log` in production. The private log rotates at 5 MB and
retains five previous files. It contains error identifiers, codes, components,
and code locations, never prompts, chat text, attachments, cookies, or provider
bodies.

Never commit `.env`, credentials, live databases, attachments, backup identities,
or decrypted backups.
