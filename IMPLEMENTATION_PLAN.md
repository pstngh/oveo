# Oveo v2 implementation plan

1. Recover only the approved v1 inputs: role-mapped Argon2 hashes, OpenRouter routing policy/key, Oveo brand assets, prompt/rule history, Caddy endpoint, deployment facts, and backup encryption material.
2. Build one FastAPI/SQLAlchemy/SQLite application with a React/Vite frontend, immutable messages, durable background generations, canonical work versions, append-only usage accounting, and strict two-account authorization.
3. Implement reconnectable typed-block streaming, exact retry, cancellation, bounded provider retries, canonical-state validation, title/summary/handoff calls, and privacy-safe logging.
4. Add the dark desktop-first conversation UI, owner account switcher, mode/voice creation flow, long-paste attachment conversion, exact deliverable copying, and generation activity controls.
5. Consolidate separate Translate and AlithyaGPT prompts from the verified v1 rule baseline, keeping source content explicitly untrusted.
6. Add migrations, focused backend/frontend/browser tests, encrypted backup/restore tooling, a slim multi-stage image, Compose, CI, and exact-image deployment automation.
7. Pass local quality gates and fresh-database/restore rehearsals before the first commit and push.
8. Publish a private `pstngh/oveo` repository and immutable GHCR image, inventory and precisely remove only the authorized v1 deployment, install v2 behind the preserved Caddy endpoint, and verify login, streaming, cost retention, backups, health, security headers, and automatic deployment.

Production deletion is gated on all of: verified exact targets, staged credentials and Caddy/backup material, a successful clean test suite, a built immutable image, a disposable migration/restore rehearsal, and an available fix-forward deployment path.
