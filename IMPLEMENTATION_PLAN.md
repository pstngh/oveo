# Oveo v2.1 three-section implementation plan

1. Start from verified clean commit `433d39f3ee3d495c330e0ef5141639aedfcc55d1`
   without importing discarded implementation history.
2. Replace the two-section prompt composition with one compact shared Alithya rule
   file, three independent mode prompts, the existing technical protocol, and an
   explicit machine-owned trust/conflict policy.
3. Persist `translate`, `revision`, and `internal_comms`; alias legacy API input
   `alithyagpt`; keep `voice_key` only as dormant compatibility data; map canonical
   work to `translation`, `revision`, and `draft`.
4. Add a reversible Alembic migration that preserves thread IDs, messages,
   attachments, summaries, actors, and canonical work, with documented downgrade
   collapse where the legacy schema cannot represent Revision.
5. Present three even desktop section cards and a clean narrow single-column stack;
   update badges, types, API calls, and the visible WebMCP schema consistently.
6. Add focused tests for prompt composition, non-converging mode boundaries, locale
   isolation, inert delimiter-looking data, stale canonical versions, compatibility,
   dormant voice data, and migration upgrade/downgrade preservation.
7. Run backend formatting/lint/type/full-test gates, frontend install/typecheck/
   lint/test/build gates, disposable Alembic fresh/current/downgrade/re-upgrade/
   drift gates, and local desktop/mobile browser QA with disposable credentials.
8. Stop local servers, inspect the final diff for unrelated changes, and commit the
   verified implementation locally. Do not push, deploy, touch production, or
   trigger external workflows.
