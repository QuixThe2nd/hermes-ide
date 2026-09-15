# Discord history plugin implementation rules

- Implement the approved contract in the Hermes discord-history recall plan.
- This source tree is isolated. Never modify the live Hermes checkout or live plugin directory from implementation workers.
- Model-facing code is read-only. No raw SQL, arbitrary columns, operators, tables, or fragments cross the tool boundary.
- Every retrieval path must authorize from direct bound Discord ContextVars before opening PostgreSQL and must filter `deleted_at IS NULL`.
- Never print or commit credentials. Tests use fakes or temporary PostgreSQL state.
- Use parameterized psycopg SQL. Bound result counts, context windows, snippets, and final UTF-8 JSON bytes.
- Add tests before or alongside code. Run the focused tests you own and report exact commands and exit status.
- Do not restart the Hermes gateway or add cron jobs.
