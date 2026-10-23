# Proposed bounded soak contract

Issue [#370](https://github.com/dariuszpanas/django-ray/issues/370) owns sustained
Linux/KubeRay/PostgreSQL certification. The repository now defines proposed
profiles and a versioned report validator in
[`qualification/soak/`](https://github.com/dariuszpanas/django-ray/tree/main/qualification/soak).
No soak runner, fault injector, resource collector, or execution provider is
included. No sustained run has been established by these contract tests.
The [1.0 readiness criterion](../one-zero-readiness.md#upgrade-and-operational-evidence)
remains pending.

The numeric parameters below are **uncalibrated proposed fixture policy**, chosen
before execution. They are not measured capacity, production defaults, an SLO,
or admission to shared infrastructure. A future implementation must review their
feasibility, freeze the exact profile and fault expectations before the first
observation, and retain failed attempts. Changing limits after a failed run
requires a reviewed profile change and a fresh run; never repeat until green.

## Ownership and execution prerequisites

The execution provider admits and enforces finite resources, provisions isolated
Linux services, binds source/archive/wheel/image identity, witnesses failure
injection, and proves removal of owned infrastructure. It must refuse capacity
it cannot enforce. This contract does not create an external scheduler or grant
Kubernetes authority. The [finite Linux stages](linux-test-plan.md) establish the
earlier regression checkpoint; the [affected deployment gate](../deployment/local-kuberay-gate.md)
remains independently applicable.

django-ray owns workload outcomes, durable task/attempt observations, retention
assertions, and the report. The catalogue's owner paths are reusable assertion
starting points, not a claim that each sustained fault adapter already exists.
Before execution, implement and review bounded collectors, complete task admission
accounting, the fixed fault schedule, every fault's expected task outcomes,
measurement scope, authenticated HTTP probes, and cleanup witnesses. A source-owned
observer must verify these facts against the candidate; caller-supplied booleans
and a valid JSON document cannot establish their truth.

Historical platform evidence may be reused only for an unchanged platform
contract. Product observations must match the candidate. Compiled Graph,
mixed-version rolling upgrades, external adopter evidence, and the separate
observation window remain outside this profile.

## Proposed profiles

The authoritative values are `qualification/soak/profiles_v1.json`, with
`schema_version: 1` and `status: proposed`. The profile digest binds its selected
parameters plus the workload/fault catalogue and owner paths.

| Parameter | Development | Release |
|---|---:|---:|
| Total observation duration | 3,600 s | 21,600 s |
| Submission window | 2,400 s | 19,200 s |
| Task submissions | 400 | 3,200 |
| Minimum time between submissions | 6 s | 6 s |
| Terminal settlement allowance after submission closes | 600 s | 600 s |
| Final quiescent observation window | 600 s | 1,800 s |
| Repetitions of every fault | 1 | 6 |
| Serialized report limit | 8 MiB | 32 MiB |

Both profiles allow 600 seconds for setup and 180 seconds for cleanup, outside
the observation duration. They admit at most four active tasks, sample every
15 seconds, and use a 300-second warmup with its final 120 seconds as the
resource baseline. Each fault has a 10-second injection deadline and a
120-second recovery deadline. Faults are serial, start after warmup, and recover
before submission closes. The entire maximum fault budget fits within that
window. Every task settles before the final observation window begins; no new
task may be submitted during settlement or final observation.

The ten-task mix is four short tasks and one each long, retrying, cancelling,
expiring, workflow, and RuntimeEnv task. Each family runs equally through Core
and Jobs. The exact totals are checked. These are synthetic qualification tasks;
business payloads must never enter the report.

The proposed final-state expectation is `SUCCEEDED` for short, long, retry,
workflow and RuntimeEnv tasks, `CANCELLED` for cancelling tasks, and `EXPIRED`
for expiring tasks. An `outcome_verified` flag cannot authorize a different
terminal state. Actual failures and `LOST` outcomes remain retainable in failed
reports. A future scenario that deliberately expects a fault-induced terminal
failure needs a reviewed association between that fault and its task outcomes
in the contract **before execution**; this initial profile includes no such
exception. It does not claim every future fault adapter can meet these defaults.

The fixed fault catalogue separates manager loss, lost leases, head loss, worker
loss, Client disconnect, pod eviction, same-version application replacement,
PostgreSQL interruption, result-storage interruption, cancellation/completion
races, uncertain completion, queue pressure, storage pressure, malformed
RuntimeEnv snapshots, and unavailable RuntimeEnv artifacts. Missing a fault or
repetition prevents eligibility. An unresolved remote outcome does not become
success, cancellation, or interruption merely because a fault was planned.

## Admission and measurements

The proposed **caller-enforced workload envelope** is 3,250 millicores,
12 GiB memory, 1,024 PIDs, 7 GiB scratch, 512 MiB shared memory, and exactly two
Ray nodes. Host/control-plane/registry overhead needs separate provider admission;
these values do not authorize allocation of an existing cluster. The report must
record verified enforced limits matching the profile. The current contract has
no enforcement implementation.

Measurement thresholds are separate from that envelope:

| Aggregate observation | Maximum | Allowed final growth over warmup baseline |
|---|---:|---:|
| Memory | 12 GiB | 256 MiB |
| Database storage | 512 MiB | 64 MiB |
| Open file descriptors | 4,096 | 16 |
| Node-local disk | 7 GiB | 64 MiB |
| Ray object store | 256 MiB | 16 MiB |
| RuntimeEnv storage | 512 MiB | 32 MiB |
| Prometheus series | 10,000 | 128 |
| Active tasks | 4 | 0; final count must be zero |
| Unhealthy expected scrape targets | 3 during a fault/recovery window | 0 |

Collectors must define the exact owned scope and units and avoid double-counting
processes, shared pages, storage, or series. PostgreSQL storage is measured
physically, not inferred from row counts or logical payload bytes. Missing or
stale expected Django/head/worker targets count as unhealthy; a missing target
must not shrink the expected set. Outside declared fault/recovery windows all
three targets must be healthy and series must be nonzero. Scrape health is a
separate observation from series cardinality.

Every category needs the complete `0, 15, ... duration` sample grid. The validator
recomputes maxima and compares the maximum of the final quiescent window against
the maximum of the declared warmup baseline. It rejects missing samples and
nonfinite, negative, boolean-as-number, or out-of-bound values. This finite
comparison is not proof of stability at arbitrary load or duration. Sampled
active counts cannot detect shorter peaks; the eventual producer must also
enforce the four-task admission limit and retain its admission evidence.

## Report v1

`parse_report(bytes)` applies serialized byte limits, rejects duplicate JSON keys,
and calls `validate_report`. `validate_report` checks exact fields, finite values,
bounded collections, unique task/fault identities, and known chronology. It
retains structurally valid failed or partial observations. Missing terminal,
injection, or recovery timestamps remain `null`; empty sample arrays express
unmeasured categories. Never insert zero measurements or successful outcomes to
fill gaps.
Unobserved admission limits likewise remain `null` with `verified: false`.

`require_eligible(report, expected_source=..., expected_profile=...)` applies the
stricter profile requirements. The caller must obtain expected identity
independently. Eligibility means the document satisfies this proposed contract;
it does not verify evidence authenticity, run a soak, satisfy issue #370, or
accept 1.0 readiness.

| Report field | Meaning |
|---|---|
| `schema_version`, `profile`, `profile_sha256` | Version 1 and exact proposed contract selection |
| `source` | Linux platform, commit/tree/archive/wheel identity, application/Ray/PostgreSQL/Prometheus/Grafana/operator image digests, and Python/Django/Ray/PostgreSQL/Kubernetes/KubeRay versions |
| `status`, `failure_codes`, `elapsed`, `cold_ray`, `admission` | Explicit passed/failed/partial outcome, fixed failure categories, phase durations, cold generation decision, and enforced resource envelope |
| `submitted_count`, `tasks` | Exact contiguous run-local numeric identities; family/runner; submission/terminal timing; state; attempts; reconciliation/ownership counters; outcome, quiescence, and uncertainty observations |
| Task `attempt_history` | At most 64 numbered summaries with state, started flag, and execution generation; no payload or traceback |
| `faults` | Exact kind/repetition, injection/recovery timestamps and outcome/quiescence/uncertainty observations |
| `resources` | Complete fixed-category arrays of `{at_seconds, value}` |
| `retention`, `cleanup` | Retention counts and preservation assertions; exact-owned remaining inventory and retained payload bytes |
| `http` | Separate unresolved historical attribution and supported-core disposition with bounded observations |

Eligible tasks have complete terminal histories numbered from one through their
observed attempt count, strictly increasing retry generations, and a final history
state matching the task. Retrying tasks need at least two attempts. Unstarted
expired/cancelled attempts still have a numbered terminal summary; unstarted
success/failure/lost entries are refused. The retained `TaskAttempt` count must
equal the sum of task attempt counts. This is compact per-attempt outcome evidence,
not a complete event ledger or proof of every ownership transfer.

## Retention, cleanup, and HTTP disposition

Both proposed fixtures use zero-day input/detail retention, a 60-second cleanup
cadence, batches of 100, and a final reclaimable-storage limit of 64 MiB.
The provider must actually run the existing bounded commands under deadlines.
The report needs successful repeated passes, observed input/detail deletion, zero
remaining eligible objects, and preserved terminal outcomes.

Input cleanup preserves `PURGED` tombstones. Workflow cleanup preserves task and
attempt summaries. The report therefore distinguishes reclaimable bytes from
retained history and requires every task/attempt summary to remain before final
infrastructure teardown. Deleting all executions is not a retention test. See the
[CLI retention contracts](../reference/cli.md#django_ray_purge_inputs).

After evidence collection, separately verify owned processes, listeners, leases,
Ray Jobs, temporary files, and external artifacts are gone. A stop request,
driver disconnect, or `ray.shutdown()` alone cannot establish cleanup. All six
remaining counts must be zero and cleanup must be verified within its deadline.
Retain only the bounded report, never task payload bytes or unbounded logs.

Issue #613's historical timeout attribution stays unresolved. The proposed
supported-core HTTP expectation is fixed in the profile: completed response,
status 200, no transport failure, no retry, and a configured ten-second timeout.
Observe after serving readiness; this does not promise uninterrupted availability
during replacement. Any recorded timeout or failure remains in a failed report,
even if its producer labels it expected. A different expectation requires a
reviewed profile change before execution. Each replacement repetition requires
caller/substage, phase/status/transport, configured/effective timeout and retry
policy, and remaining observer/outer budgets. Raw URLs, headers, bodies, exception
messages, credentials, and task contents are excluded by the fixed schema.

## Contract checks

These resource-free checks exercise synthetic reports and refusal behavior:

```bash
uv run pytest tests/unit/test_soak_contract.py -q
uv run make docs-build-strict
```

Actual certification still needs reviewed execution/collection adapters, admitted
Linux capacity, retained candidate-matched evidence, disposition of every failure,
and a separate readiness review. This first deliverable cannot close #370.
