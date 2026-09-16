# Oveo v2

Oveo is a private, two-account AI translation and professional writing adviser. It provides separate Translate and AlithyaGPT conversation modes, typed copyable deliverables, durable streaming generations, canonical document versions, and lifetime model-cost accounting.

## Local development

Requirements: Python 3.13 (3.12+ supported), Node.js 22+, `uv`, and SQLite.

```bash
cp .env.example .env
# Set development password hashes and, for real model calls, OVEO_OPENROUTER_API_KEY.
uv sync --extra dev
uv run alembic upgrade head
cd frontend && npm ci && npm run build && cd ..
uv run uvicorn oveo.main:app --reload
```

Open `http://127.0.0.1:8000`. The backend serves the compiled frontend. For frontend hot reload, run `npm run dev` in `frontend/`; Vite proxies API requests to port 8000.

Generate Argon2id password hashes without putting a plaintext password in shell history:

```bash
uv run oveo-admin hash-password
```

## Quality gate

```bash
uv run ruff check .
uv run mypy
uv run pytest
cd frontend
npm ci
npm run typecheck
npm test
npm run build
```

## Configuration

All settings use the `OVEO_` prefix. See `.env.example`. Production requires:

- the two existing role-mapped Argon2id hashes;
- the OpenRouter key;
- `OVEO_ENVIRONMENT=production`, the trusted public origin/host, secure cookies, and persistent data paths;
- the fixed `openai/gpt-5.6-luna` model and privacy-eligible provider routing implemented server-side.

Never commit `.env`, credentials, live databases, attachments, backup identities, or decrypted backups.

## Runtime

Production uses one slim image and one Uvicorn worker behind host Caddy. SQLite runs in WAL mode with foreign keys and a bounded busy timeout. Background model calls live in the application process but have durable generation rows and replayable snapshots, so navigation and browser disconnects do not cancel them. See [ARCHITECTURE.md](ARCHITECTURE.md) and [OPERATIONS.md](OPERATIONS.md).
