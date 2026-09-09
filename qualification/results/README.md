# Installed-wheel historical result reads

`reads.yaml` runs `python -m qualification.results.scenario` through the existing
`external-evidence-v1` Linux command lane. Build and register the exact candidate with the
[existing wheel/image procedure](../kubernetes/README.md), registering
`qualification/results/reads.yaml` for that commit and adding its complete source archive
at `/workspace`. Use this definition's command, not the linked result-fold runbook.
The command accepts no arguments,
installs the archived candidate wheel offline, and checks its source/package digest and each
child process's actual package import location.

Three fresh Django processes each create a private SQLite fixture. They cover an unimported
row-selected module, a module attribute hook, and a removed task export. Each checks synchronous
and asynchronous result reads and refresh, historical identity and successful result data,
and absence of application effects. The removed-task case preserves matching-function identity
and rejects a different task. Each process also observes 28 refusals across result Task execution,
enqueue, direct backend/base enqueue, copying and pickle paths. Reading never creates work;
an explicitly declared current Task can still enqueue and retain its original identity.

The same probe runs in `tests/unit/test_inert_result_reads.py`. A known projection's `func` is
already trusted application code; this is not a Python sandbox. Existing guarded external
input/result loaders and their failure tests remain separate coverage.

Each child has a 45-second deadline and bounded process-group output. The whole registered
workload has a 420-second deadline and 180-second owned cleanup allowance. It starts no Ray
runtime, manager, HTTP server or external database. Cold-Ray decision: not applicable to this
Django read/enqueue boundary; each process is fresh. SQLite does not prove PostgreSQL locking,
and these fixtures do not replace the released-data upgrade/restore rehearsal.

Successful evidence requires all three exact cases and all declared observations. The manifest
binds the wheel, package, dependencies, child imports, observations, elapsed time and fixture
cleanup. Required artifacts are `command.log`, `junit.xml` and `execution-manifest.json`, under
the existing 1 MiB each/3 MiB aggregate envelope. Failure retains bounded diagnostic and available
candidate/partial-case evidence without passing the scenario. Existing evidence is never
overwritten. The external runner must separately prove owned resource removal, released capacity
and preservation of the control plane and pool contracts.
