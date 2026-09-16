# Architecture

## Boundaries

Caddy terminates HTTPS and proxies only to the app's loopback port. FastAPI serves authenticated REST/SSE routes and the compiled React application. The one application process owns an in-process generation manager, an async OpenRouter client, SQLite, and a private attachment directory. Uploaded/source text and model output are content, never trusted control instructions.

Authentication uses opaque random cookies whose SHA-256 hashes are stored in `sessions`. Sessions have a fixed 30-day deadline and carry a credential version. Unsafe cookie-authenticated requests require a per-session CSRF token and same-origin validation. Every thread route resolves ownership on the server; the owner account selector changes scope, never identity. The true sending actor is stored on each user message.

Operational logs contain opaque IDs, durations, sizes, route/status information, and error classifications only. They exclude prompt/message/attachment content, provider payloads, credentials, cookies, and authorization headers.

## Data model

- `users` and `sessions` hold the two permanent identities and revocable authentication state.
- `threads`, immutable `messages`, and one optional `attachment` per user message form the visible conversation.
- `generations` hold idempotency keys, frozen request context, status, replayable partial blocks, provider IDs, and safe errors.
- `work_items` and immutable `work_versions` hold complete canonical source/output/brief snapshots independently of chat presentation.
- `usage_events` form an append-only micro-USD ledger and deliberately survive thread deletion.

SQLite foreign keys, check/unique constraints, and a partial unique active-generation index protect invariants. Connections enable WAL, foreign keys, normal synchronous mode, and a bounded busy timeout. Model calls and streaming never hold database transactions open.

## Generation lifecycle

The server validates input and atomically stores the user message plus queued generation. It then starts an independent task. The task freezes the prompt/context manifest, calls OpenRouter with the fixed model and verified privacy routing, incrementally decodes the versioned typed-block protocol, and checkpoints replayable blocks without exposing wire markers. Reconnecting SSE clients receive an authoritative snapshot.

Success atomically creates the assistant message, validates and stores any complete work version, and completes the generation. Stop or terminal failure discards partial assistant output but retains the user message. Retry creates no duplicate user message and reuses the saved message, attachment, context, and intent. On startup, orphaned queued/running generations become failed and retryable.

At most one active foreground generation exists per thread; different threads and accounts may run concurrently. The provider uses bounded cancellable retries only for transient network, 429, and 5xx failures.

## Typed responses and canonical work

Assistant messages store versioned blocks: `conversation`, plain-text `deliverable`, and `advice`. Deliverables appear first and each has an exact-text Copy action. Markdown is limited to safe advisory blocks and raw HTML is disabled.

Translate and AlithyaGPT use separate version-controlled prompts. Translate canonical state supports validated establish, append, exact-anchor replacement, and full-version operations. Every committed version is a complete immutable snapshot. The application validates anchors, grounding, base-version freshness, and the cumulative 25,000-word ceiling; it does not perform linguistic QA or silently repair invalid model operations.

Recent discussion and replaceable summaries are context policy. Canonical source/output/brief state is always injected directly and is never reconstructed from or summarized out of the transcript.

## Cost accounting

Every provider call creates a deduplicated append-only usage event. Actual provider cost is stored in integer micro-USD; missing cost remains pending and is reconciled by provider request ID. Stopped and failed calls count when the provider reports a charge. Thread deletion cannot cascade into the ledger.

## Deliberate omissions

Oveo has no Redis, worker service, queue broker, Supervisor, watchdog, lease renewal, Office/OCR pipeline, prompt database/editor, model selector, analytics, live-data encryption, linguistic repair scanner, signup/recovery flow, or mutable-message/branching machinery. These systems are unnecessary for this private two-user deployment and would weaken its simplicity.
