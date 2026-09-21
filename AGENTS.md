# Repository setup

Before configuring or launching a new pipeline run, read `SETUP_COMPLETED` and
`RERUN_SETUP` in `src/config.py`.

- If setup is incomplete or a rerun is requested, follow
  [configuration/README.md](configuration/README.md) with the user. The coding
  assistant conducts this conversation; the pipeline and Studio do not.
- Otherwise reuse the saved setup without repeating its questions.
- After saving the agreed configuration and schema, set `SETUP_COMPLETED=True`
  and `RERUN_SETUP=False`. Do not mark setup complete while choices are missing.
- Existing runs use their saved configuration and schema, not a new setup.

Respect the user's existing authorization and verification restrictions.
