# Terminal progress transport experiment

Issue #573 isolates one assumption in the proposed #261 admission design:
an admitted logical leaf could return its application value and one bounded
metadata object as separate Ray task results. The coordinator would pass the
first reference to downstream tasks and own the second until consumption and
collector acknowledgement. This experiment is not imported by package runtime.

`workflow_terminal_progress_prototype.py` uses a fixture-only callback receiving
a `report` function. This is not a proposed user task signature. The prototype
retains one latest byte string, with a fixed experimental 1 KiB ceiling. These
are synthetic bytes, not the production progress protocol or its chosen limit.
An unadmitted invocation returns only its ordinary value. An admitted invocation
returns two values; encoder failure omits metadata without replacing success.
Original callback exceptions propagate to Ray's configured retry policy.

The test suite covers local outcome preservation and adds bounded real-Ray cases
for ordinary value-reference chaining, finite/unlimited configured retries,
metadata failure after a successful retry, and exceptions affecting both result
references. Each remote task uses 0.25 logical CPU. The attempt-count actor uses
zero logical CPU and no restart. Result waits are at most 30 seconds, and owned
tasks/actors are cancelled or killed on every exit.

Resource-free checks may run on the development host:

```sh
python -m pytest tests/unit/test_workflow_terminal_progress_prototype.py \
  tests/unit/test_workflow_terminal_admission_budget.py \
  -m 'not real_ray' -q -o addopts=''
```

Run real-Ray checks only in the repository's bounded Linux test environment or
hosted Linux CI:

```sh
python -m pytest tests/unit/test_workflow_terminal_progress_prototype.py \
  tests/unit/test_workflow_terminal_admission_budget.py \
  -m real_ray -q -o addopts=''
```

`workflow_terminal_admission_budget.py` models coordinator-owned reservations.
Each reservation is charged before dispatch and remains charged through result
collection and collector acknowledgement. A known omitted result or an explicit
positive/negative receiver acknowledgement can release it. A timeout, failed
submission or unavailable actor cannot. A later acknowledgement for the same
live reservation can resolve uncertainty. Foreign, copied and retired tickets
cannot recycle capacity. No tombstone or producer-identity history is retained.

With at most K charged reservations and total reserved capacity B, the candidate
permits at most K logical metadata results and K collector calls, each channel
bounded by B encoded bytes when the producer honors its reserved limit. These
are separate channels, not a claim that their combined payload is only B. The
model rejects oversized metadata but cannot retroactively bound an oversized
value already transported by a broken producer. It does not bound physical Ray
copies, reconstruction buffers or process memory.

Resource-free model tests cover competing reservations, byte/count boundaries,
uncertain slots, stale/foreign tickets, fixed saturating counters and independent
counter reconciliation. Two additional real-Ray cases hold an actor behind a
bounded barrier, prove downstream application values can complete while the
reservation remains charged, then either acknowledge or lose the actor.

Even a passing result does not establish production aggregate admission. Still
required: integration with actual strict nested requests,
chains/maps/scatter and progress context; lineage reconstruction and lost
acknowledgements; reporting-benchmark evidence; and source-matched cold-Ray proof.
No package default, public task API, retry default or graph policy changes here.
Do not close #261 or enable #507 on the basis of this transport experiment.
