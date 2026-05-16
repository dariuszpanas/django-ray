# Handle Compatibility (Ray Core)

This document defines compatibility and migration guidance for Ray Core task handles
stored in `RayTaskExecution.ray_job_id`.

## Supported Formats

`django-ray` currently supports two Ray Core handle formats:

1. Canonical format: `<job_id>:<task_id>`
2. Legacy format: `ray_core:<task_pk>`

Both formats are accepted for status and cancellation lookups.

## Canonical Format

Preferred format for new Ray Core submissions:

```text
02000000:67a2e8cfa5a06db3ffff0001000000000000000001000000
```

Benefits:

- can be used for Ray Dashboard deep links,
- aligns with Ray job/task identifiers,
- avoids coupling to local database primary keys.

## Legacy Format

Legacy format from earlier versions:

```text
ray_core:12345
```

Notes:

- still supported for backward compatibility,
- may appear in older database rows,
- may still be used as an internal fallback when composite IDs are unavailable.

## Deprecation Policy

Policy introduced on **February 23, 2026**:

1. `v0.1.x` and `v0.2.x`: read compatibility for both formats is maintained.
2. `v0.3.0` target: keep read compatibility; avoid introducing new legacy writes except fallback cases.
3. `v0.4.0` earliest: legacy format removal can be considered only after explicit release notes and migration guidance.

No hard removal date is set yet.

## Migration Guidance

For adopters integrating with `ray_job_id`:

1. Treat `ray_job_id` as an opaque string in application code.
2. If parsing is required, support both formats.
3. Prefer dashboard links from canonical `job_id:task_id` values.
4. Do not assume legacy values can be losslessly backfilled to canonical IDs.

## Operational Query

To find legacy-format rows:

```sql
SELECT id, task_id, ray_job_id, state, created_at
FROM django_ray_raytaskexecution
WHERE ray_job_id LIKE 'ray_core:%'
ORDER BY created_at DESC;
```

Use this to estimate migration impact before tightening compatibility rules.
