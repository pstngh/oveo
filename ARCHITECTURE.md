# Architecture

## Runtime and security boundaries

Caddy terminates HTTPS and proxies only to the app's loopback port. FastAPI serves
authenticated REST/SSE routes and the compiled React application. One application
process owns the generation manager, async OpenRouter client, SQLite database, and
private attachment directory.

Authentication uses opaque random cookies whose SHA-256 hashes are stored in
`sessions`. Sessions have a fixed deadline and credential version. Unsafe requests
require a per-session CSRF token and same-origin validation. Every thread route
resolves ownership on the server; the owner selector changes scope, never identity.
The true sending actor remains stored on every user message.

Operational logs exclude prompt, message, attachment, provider-payload, credential,
cookie, and authorization-header content.

## Three section model

Threads persist one current mode: `translate`, `revision`, or `internal_comms`.
Translate owns translation direction and target-locale selection. Revision owns
inference, preservation, or intentional same-language adaptation of the existing
locale. Internal communications owns draft-locale selection and alone applies the
`Comm internes` house style. No user-facing or model-active voice selector exists.

## Prompt composition and trust

For an ordinary visible chat generation, the system message contains exactly:

1. machine-generated trusted application context with canonical mode, generation
   purpose, instruction/data boundary, and explicit conflict policy;
2. the shared technical `protocol.md`;
3. exactly one active mode prompt;
4. exactly one shared `alithya_rules.md`.

Title and summary generations retain safe purpose-specific contracts but do not
load the visible response protocol. Prompt handoff compaction continues to use its
separate user-only extraction contract.

The conflict policy is explicit: runtime security/protocol invariants; mode scope,
semantics, and preservation; mandatory Alithya terminology/names/protected content;
permitted explicit user choices; then default brand/style. Prompt ordering is not
the conflict-resolution mechanism.

Conversation, source, attachment, quoted, summary, prior assistant, brief, and
canonical document text is serialized in one separately delimited JSON data
envelope. Raw angle brackets are escaped during serialization and decode to the
exact original text. Canonical provenance separates application-managed version,
word-count, and last-operation metadata from document data. A model may copy the
version for a state precondition, but neither that metadata nor document text can
redefine instructions.

The single shared rule file owns authorized terminology (including HCBP/PACH),
brand/naming, locale conventions, official names, protected URLs/placeholders,
and the common adviser posture. The three independent mode prompts own all task
behavior. The protocol owns only typed NDJSON structure and canonical-operation
mechanics. Future global rule edits affect future generations, not immutable saved
canonical versions.

## Data model and migrations

- `users` and `sessions` hold identities and revocable authentication state.
- `threads`, immutable `messages`, and one optional attachment per user message
  form the visible conversation.
- `generations` hold idempotency keys, frozen request context, status, replayable
  partial blocks, provider IDs, and safe errors.
- `work_items` use kinds `translation`, `revision`, or `draft`; immutable
  `work_versions` hold complete canonical source/output/brief snapshots.
- `usage_events` form an append-only micro-USD ledger and survive thread deletion.

SQLite foreign keys, check/unique constraints, and partial unique indexes protect
invariants. Connections enable WAL, foreign keys, normal synchronous mode, and a
bounded busy timeout. Model calls and streaming never hold transactions open.

## Generation and canonical state

The server atomically stores validated input and a queued generation, then starts
an independent task. It composes a frozen context, calls the fixed model with
privacy routing, incrementally validates typed NDJSON, and checkpoints replayable
visible blocks. Reconnecting clients receive an authoritative SSE snapshot.

Success atomically creates the assistant message, validates any state operation,
and completes the generation. The server enforces closed schemas, exact anchors,
non-overlap, source limits, and current `base_version`; stale operations fail.
Mode prompts decide when those valid operations semantically apply. Each mode maps
new canonical work to its corresponding work kind. Stop/failure discards partial
assistant output but retains the user turn; retry does not duplicate it.

## Deliberate omissions

Oveo has no dynamic rule-pack loader, prompt database/editor, unified-conversation
router, Redis, worker service, queue broker, model selector, mutable-message
branching, Office/OCR pipeline, analytics, signup/recovery flow, or linguistic
repair scanner. These are unnecessary for this private deployment and would make
the trust and maintenance paths less clear.
