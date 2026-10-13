# Routing, run summaries, and silent memory: staging verification

The coordinated release at source `af914b4b41257f30dd7d6c1176fd6c6af755df33`
passed Cloud Build `f12b8faf-1db6-4c37-a250-dac8cbd96d6c` (all ten gates).
An API-only routing correction at `8c7b5d628b4e492bdb1a32d52b2c3993ceff3048`
then passed build `979142e4-6dd0-4e93-8458-242746d39964`, including headless checks
and compatibility with the deployed shared worker. The worker images remain from the
coordinated release. Final staging revision: `pistachio-staging-summary-8c7b5d628b4e`,
serving 100% of traffic on October 13, 2026.

Ruff and strict mypy passed; **760 tests passed, 6 skipped**.
[Final source CI](https://github.com/khushidesai1/pistachio/actions/runs/38477066111)
passed. Preview and live health returned 200; removed UI routes returned 404; unsigned
Slack ingress and unauthenticated internal endpoints returned 401. Promotions used idle
boundaries and resumed the task queue. IAM, secrets, grants, and other settings were preserved.

## Complete folder answers

Event `5b17e29fe1c4e3280f84613817b36c16` replayed the exact TMA56 question in a new
breast-alchemy DM thread. It answered for `raw_outputs/raw/` and `resegmentation/`,
listing missing numbers **9, 10, and 37–56** in order and noting ambiguous naming conventions.
Both matched direct `gcloud storage ls` results. No Batch job or worker manifest was created.

```text
inventory event=5b17e29fe1c4e3280f84613817b36c16 paths=2 entries=74 complete=true ms=904
```

The imported project guide was present in router context. Its two verified layout facts
were committed through the normal memory backend as source `import`, commit
`b4c585ffe57495442030488043a6cee112ba8b42` in `khushidesai1/pistachio-memory`.
The import preserved the existing working notes.

Live testing exposed a model returning plain text instead of its routing tool. The bounded
retry now gives a format correction. After that retry, complete, bounded final metadata
answers may normalize to a typed read-only reply. They cannot edit the guide or invoke tools.
Incomplete listings fall back to compute; truncated and mixed responses are not accepted
as completed metadata answers.

## Concise publication and stopped-run summary

Event `3507ae21171f6b9a684d0be88a48a4fc` copied the existing TMA1 `tables/` store into
`gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261013-summary-retry/tables/`.
All **287 objects / 61,900,890 bytes** matched source sizes and CRC32C values, with expected
per-event/grant metadata and no extra or missing files. A recursive CLI listing also found
287 objects. The actual `publish_gcs` call was recorded at 22:17:51 UTC.

Slack delivered the agent's short summary and exactly one receipt:

```text
Saved to GCS: gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261013-summary-retry/ — 287 files, 61.9 MB.
```

Both per-publication and aggregate audit manifests were verified. The first live copy found
that workers cannot write the controller audit prefix. Workers now stage reports in their
existing event transport area; the controller archives them during polling. No worker IAM
expansion was needed. The original `20261013-summary/` copy and successful retry copy remain
for Khushi to delete. Source data was unchanged.

Event `50924c0ecdbaa306a018c2c13fc2c347` used a worker explicitly pinned to
`AGENT_MAX_TURNS=3`. Its eight-step workspace check stopped after three steps, then produced
a coherent closing summary and the short turn-limit footer. Saved metadata records
`error_max_turns` and `closing_summary=true`. The default was restored in the same session;
no `AGENT_MAX_TURNS` override remains, so new workers use **500**. The helper briefly read
the job before creation finished; a subsequent describe verified the same worker's cap,
and its restoration block completed successfully.

## Memory and test scope

Automatic memory-save notifications are removed from guide initialization and post-turn saves.
Commit, retry, compaction, and future-context behavior remain covered by regression tests.
The live canary threads contain no “Remembered for this project” messages.

These checks posted labeled bot messages and submitted corresponding signed test events for
the authorized user. They exercised the deployed router, real model, workers, publisher,
and Slack delivery; they were not manually typed Slack messages. Budget-limit closing behavior,
failed closing calls, 1,073-file receipts, and large-upload limits were tested offline.
