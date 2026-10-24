# Isolated application startup profile

The `Application Startup Profile` workflow measures Django startup in fresh,
bounded Linux containers. It helps investigate a readiness diagnostic whose last
observed checkpoint precedes the worker lease command. It never establishes
worker readiness or satisfies `Qualification Gate`.

The workflow archives its exact candidate, builds the ordinary runtime image once,
and derives a second image that adds bytecode for `/app/src` and `/app/testproject`.
It verifies source correspondence and records the source and image identities.
The runtime Dockerfile already compiles dependencies in the virtual environment;
its final source copies exclude Python caches. The experiment checks the actual
images instead of assuming that source and dependency caches have the same state.

Four measured processes run sequentially in the fixed order baseline, precompiled,
precompiled, baseline. Separate bounded controls inventory source files and caches
before that sequence. Each container has 250m CPU, 768 MiB memory, a 64-PID bound,
no network, a read-only root filesystem and disabled image health checks. The
experiment preserves the full production qualification settings and installed
apps. It supplies synthetic credentials from a fixed environment, refuses Ray
imports and Django database connection attempts, and stops after Django setup.
It does not execute the lease command, start a database, or connect to Ray.

Each observation has a separate 20-second diagnostic limit. This does not change
the qualification probe's ten-second limit, the follow-up's inner timeout of up
to eight seconds, or the protected-log observer's 30-second deadline. A completed
20-second experiment cannot show that any of those tighter limits would pass.

The runner retains bounded, closed JSON containing timing, cache and identity
facts. Matching cache headers and import paths establish cache availability,
not direct evidence that the import loader used a cache. Ambient application
output and exception text are discarded. An attempted
forbidden operation, malformed output, timeout or unconfirmed cleanup cannot be
reported as a completed startup sample. Only containers with this experiment's
exact ownership label are eligible for cleanup.

Run it only on an admitted disposable Linux Docker host. The hosted workflow
admits its fresh VM before building; it creates no registry, Kubernetes cluster,
database service or Ray runtime. The parent controller is invoked as follows:

```sh
python scripts/investigate_application_startup.py \
  --image "$BASELINE_IMAGE_ID" \
  --source-dir "$EXPORTED_CANDIDATE_SOURCE" \
  --output "$NEW_EVIDENCE_DIRECTORY" \
  --owner "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
```

The baseline is a fresh build from the recorded source and lock. It is not the
retained image from an earlier failing Application run and omits the additional
qualification browser layer. All four processes share host page caches; their
fixed order does not eliminate warm-cache or scheduling effects. Guarding and
measuring startup also adds work absent from the ordinary probe. Differences
between the two variants provide directional evidence about source compilation
in these isolated containers. They do not reproduce load from a running manager,
Kubelet exec transport, database contention, or concurrent workloads, and cannot
establish the cause of an earlier readiness failure.

Any proposed product or image correction still needs focused regression checks,
the current Linux CI checkpoint and every affected deployed qualification.
