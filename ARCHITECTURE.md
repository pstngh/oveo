# Architecture

## Runtime and security boundaries

Caddy terminates HTTPS and proxies only to the app's loopback port. FastAPI serves
authenticated REST/SSE routes and the compiled React application. One application
process owns the generation manager, async OpenRouter client, SQLite database, and
private attachment directory.

Authentication uses opaque random cookies whose SHA-256 hashes are stored in
`sessions`. Sessions have a fixed deadline and credential version. Unsafe requests
require a per-session CSRF token and same-origin validation. Every thread route
scopes ownership to the authenticated account on the server. There are no account
roles or cross-account selectors. The true sending actor remains stored on every
user message.

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
   for DOCX-backed work, the conditional `docx_protocol.md` extension;
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

DOCX attachments have a persisted `source` or `reference` role selected at upload.
The latest reference remains an explicit `active_reference_document` across
conversation compaction. Revision and translation use it as terminology and style
precedent when it matches the current document family or context; it never becomes
canonical source or a DOCX template. Current canonical output and unsummarized
conversation material provide additional grounded precedent. The model must not
claim a historical wording choice unless that wording is present in supplied
canonical, transcript, or reference data.

The single shared rule file owns authorized terminology (including HCBP/PACH),
brand/naming, locale conventions, official names, protected URLs/placeholders,
and the common adviser posture. The three independent mode prompts own all task
behavior. The protocol owns only typed NDJSON structure and canonical-operation
mechanics. Future global rule edits affect future generations, not immutable saved
canonical versions.

## Data model and migrations

- `users` and `sessions` hold identities and revocable authentication state.
- `threads`, immutable `messages`, and one optional role-tagged `.docx` attachment
  per user message form the visible conversation.
- `generations` hold idempotency keys, frozen request context, status, replayable
  partial blocks, provider IDs, and safe errors.
- `work_items` use kinds `translation`, `revision`, or `draft`; immutable
  `work_versions` hold complete canonical source/output/brief snapshots plus,
  when applicable, a DOCX template attachment reference and block replacements.
- `usage_events` form an append-only micro-USD ledger, survive thread deletion, and
  produce one overall lifetime-cost total visible to either authenticated account.

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

## DOCX preservation path

DOCX input stays inside the existing attachment directory and backup/deletion
lifecycle. Upload validation bounds ZIP members, expanded size, compression ratio,
and XML part size; rejects unsafe paths, encryption, macros, malformed OOXML,
tracked changes, and complex field hyperlinks; and extracts only main-document
paragraph and table-cell text. The immutable extracted block map is stored with
the attachment so transcript reconstruction does not repeatedly parse the OOXML
package. Headers, footers, fields that are not safely editable, and non-text
drawing content remain untouched.

For a source DOCX, the model receives application-generated block IDs and protected
hyperlink tokens. The server requires every expected working-document block and
hyperlink exactly once and in order, derives clean canonical output from the
validated map, and commits the map in the same transaction as the assistant message
and `work_version`. Reference block IDs are never accepted as the working map.
Export is an authenticated, private/no-store GET that selects the latest active
canonical version and patches the original OOXML package in memory. Generated
exports are not persisted.

## Deliberate omissions

Oveo has no dynamic rule-pack loader, prompt database/editor, unified-conversation
router, Redis, worker service, queue broker, model selector, mutable-message
branching, office conversion suite, OCR pipeline, analytics, signup/recovery flow,
or linguistic repair scanner. These are unnecessary for this private deployment
and would make the trust and maintenance paths less clear.
