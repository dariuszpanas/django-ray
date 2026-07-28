# Draft upstream report: Compiled Graph crashes on Windows

> Status: retained draft only. This report has not been filed upstream.

> Evidence limitation: the original Windows pass did not retain raw stdout/stderr or
> an exact dependency and runtime-profile inventory. Reproduce in a fresh disposable
> environment before filing; the current summary is not promotion-grade evidence.

> Follow-up ownership: issue #100 owns the fresh Windows/nightly evidence and upstream
> report, targeted for 2026-07-27. It is non-blocking for issue #86 / PR #92 and for
> Linux/Kubernetes promotion work in issue #99.

## Proposed title

`[core][compiled graph][windows] experimental_compile crashes in shared-memory writer registration`

## Summary

On Windows 11 build 26200 AMD64 with CPython 3.12.12, a one-actor CPU Compiled
Graph crashes while `experimental_compile()` registers its shared-memory writer. The
same failure reproduces on Ray 2.53.0, 2.56.0, and 2.56.1.

- In a directly connected Ray Core driver, Python exits with `3221225477`
  (`0xC0000005`) after `Windows fatal exception: access violation`.
- When compilation is owned by a `max_retries=0` outer Ray task, that worker crashes
  and the driver receives `WorkerCrashedError`.
- The captured stacks reach
  `ray.experimental.channel.shared_memory_channel.ensure_registered_as_writer`
  during `DAGNode.experimental_compile()`.

The retained historical Linux candidate observations are documented separately. This
report does not claim that every Windows, Python, Ray, or Compiled Graph topology is
affected.

## Environment

- Windows 11 build 26200, AMD64
- CPython 3.12.12
- Ray installed as `ray[cgraph]==2.53.0`, `2.56.0`, or `2.56.1` in the
  repository virtual environment; the reproducer itself imports only Ray
- CPU shared-memory transport; no GPU or NCCL path
- Direct virtual-environment interpreter, not an environment-manager wrapper

## Minimal reproduction

Use a fresh virtual environment for each Ray version. The
[standalone reproducer](reproduce_ray_compiled_graph_windows.py) imports Ray only and
supports one topology per process:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install "ray[cgraph]==2.56.1"

# Run each command from a disposable shell under an external 60-second bound.
.venv\Scripts\python.exe docs\investigations\reproduce_ray_compiled_graph_windows.py `
  --topology direct-driver
.venv\Scripts\python.exe docs\investigations\reproduce_ray_compiled_graph_windows.py `
  --topology nested-ray-task
```

Repeat in clean environments for 2.53.0 and 2.56.0. The direct process may terminate
before Python cleanup runs; do not run it inside an application or test worker.

## Expected behavior

The graph compiles, echoes the payload once, drains the result, and tears down. If
Windows is unsupported, compilation should fail with a normal, documented Python
exception before native memory access rather than terminating the process or worker.

## Observed results

| Ray | Owner topology | Observed result |
|---|---|---|
| 2.53.0 | Direct driver | Exit `3221225477` / `0xC0000005`; `Windows fatal exception: access violation` |
| 2.53.0 | Nested Ray task | `WorkerCrashedError`; owner worker crash |
| 2.56.0 | Direct driver | Exit `3221225477` / `0xC0000005`; `Windows fatal exception: access violation` |
| 2.56.0 | Nested Ray task | `WorkerCrashedError`; owner worker crash |
| 2.56.1 | Direct driver | Exit `3221225477` / `0xC0000005`; `Windows fatal exception: access violation` |
| 2.56.1 | Nested Ray task | `WorkerCrashedError`; owner worker crash |

The exact structured record, including the separately observed Linux control results,
is retained in
[`compiled-graph-platform-evidence-2026-07-19.json`](compiled-graph-platform-evidence-2026-07-19.json).

## Nightly and issue search

Nightly was not run. Its wheel URL and ABI are a separate, rotating tuple, while this
investigation was intended to classify exact published releases. A nightly result
would not verify or invalidate any of the six release tuples above. It may be useful
after maintainers identify a candidate fix.

Read-only searches of `ray-project/ray` on 2026-07-19 for
`ensure_registered_as_writer`, `Compiled Graph Windows access violation`, and
`SharedMemoryChannel Windows` returned no matching issue. Search results can change;
repeat the search immediately before filing.

## Issue #100 refresh, attachments, and maintainer questions

- Rerun the release and nightly tuples by 2026-07-27 in fresh disposable
  environments before deciding whether to file this draft. This is not a prerequisite
  for merging issue #86 / PR #92's fail-closed Linux/Kubernetes infrastructure.
- Attach the JSON record, exact dependency/runtime profile, and reviewed, redacted,
  bounded stdout/stderr tails from each disposable run. Never attach complete
  unredacted process output.
- Confirm whether CPU Compiled Graph is intended to support Windows.
- If not, can `experimental_compile()` fail before shared-memory channel setup?
- If yes, is writer registration missing a Windows-specific mapping or lifecycle step?
