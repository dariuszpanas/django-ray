# Maintenance controls

`django_ray_maintenance` inspects or changes persisted admission pauses, requests exact worker
retirement, and quarantines or releases an exact task generation. Run it from a trusted
operator shell with the intended Django settings and database credentials. The command does not
connect to Ray, change target activation or drain policy, cancel tasks, or certify remote cleanup.

```bash
python manage.py django_ray_maintenance
python manage.py django_ray_maintenance --json
```

Status is the default and performs no writes. It reports the current policy revision,
deployment-wide flags, and at most 64 exact scopes. `--database` selects the Django database alias.
A missing or inconsistent policy is an error; the command never recreates it automatically.

`django_ray_protocol_status` can also inspect a coherent preactivation protocol-1 policy.
It reports that policy verbatim with a `historical_write_policy` blocker for the current
package's protocol 3. This read-only diagnostic does not permit legacy execution or reopen
admission. A current protocol-3 policy must have legacy admission closed and no legacy token.

## Review and apply a pause

Choose an explicit scope and a pause or resume action. Mutation requires `--expected-revision`,
`--actor`, `--reason`, and `--authorized`, even for a dry run. Actor and reason are bounded audit
labels without spaces or secrets. Actor attribution is not authentication: `--authorized`
acknowledges that the person invoking this trusted command already has deployment authority.
There is no interactive confirmation prompt.

For example, after status reports revision 1, preview a deployment-wide enqueue pause:

```bash
python manage.py django_ray_maintenance --dry-run --all --pause-enqueues \
  --expected-revision 1 --actor operator --reason planned-maintenance --authorized --json
```

The preview reports the proposed revision but publishes no audit or policy revision. Apply the
same change against the same reviewed revision:

```bash
python manage.py django_ray_maintenance --apply --all --pause-enqueues \
  --expected-revision 1 --actor operator --reason planned-maintenance --authorized --json
```

A concurrent policy change causes a revision conflict. Read status and review the new state; the
command does not retry or overwrite it automatically. An already-satisfied change is a no-op.

## Exact scopes and preserved controls

`--all` changes only deployment-wide flags, covering future or previously unseen queues. It
preserves existing exact scopes. Otherwise, supply one or more selectors:

| Selector | Match | Supported controls |
| --- | --- | --- |
| `--queue NAME` | Exact queue spelling, including Unicode and spaces | Enqueues and claims |
| `--protocol N` | Exact execution protocol version | Enqueues and claims |
| `--target KEY` | Existing immutable Ray target key | Claims only |

Selectors can be repeated, with at most 64 distinct scopes in the resulting policy. Queue and
protocol selectors may be combined. Target selectors cannot accompany enqueue controls. There is
no address-to-target inference or implicit target enumeration.

Actions are `--pause-enqueues`, `--resume-enqueues`, `--pause-claims`, and `--resume-claims`.
Use both enqueue and claim actions in one command when both apply to the selected scopes. Each
action changes only its selected flags. Other flags and scopes remain as they were. Resuming the
last paused flag removes that scope from the current policy; prior audit revisions remain.

```bash
# Pause new generations for one exact queue and protocol; replace 2 with current status.
python manage.py django_ray_maintenance --apply --queue batch --protocol 3 --pause-claims \
  --expected-revision 2 --actor operator --reason queue-maintenance --authorized

# Resume only the deployment-wide enqueue flag; replace 3 with current status.
python manage.py django_ray_maintenance --apply --all --resume-enqueues \
  --expected-revision 3 --actor operator --reason maintenance-finished --authorized
```

An immutable target pause also blocks new generations of previously bound work on a DRAINING
target. Resuming this admission scope preserves the target's separate drained, retired, or disabled
state. It does not make that target eligible for new work.

## A pause does not prove drain

Enqueue pauses fence new queue admission and re-entry, including retries. Claim pauses fence new
execution generations while allowing already queued work to remain visible. They preserve
same-generation ownership, authentic completion, cancellation and heartbeat updates. A completion
path must retain an authentic failure without bypassing an active enqueue pause to replay work.

The JSON report always contains `drain_verified: false`. Admission flags, an empty SQL queue, or an
expired worker lease do not establish remote cleanup or stopped writers. The command does not
perform a coordinated upgrade rehearsal. Follow the
[coordinated upgrade requirements](../deployment/local-kuberay-gate.md) for the applicable release
evidence and preservation checks.

## Request retirement of one worker incarnation

Supply all four fields from the retained lease: worker ID, hostname, process ID, and its aware ISO
8601 start time. A reused worker ID with another start time is a different incarnation. These exact
selectors alone read status; the command does not enumerate or select workers implicitly.

```bash
python manage.py django_ray_maintenance --worker-id "$WORKER_ID" \
  --worker-hostname "$WORKER_HOSTNAME" --worker-pid "$WORKER_PID" \
  --worker-started-at "$WORKER_STARTED_AT" --json

# Replace revision 0 with the exact entity revision from status.
python manage.py django_ray_maintenance --worker-id "$WORKER_ID" \
  --worker-hostname "$WORKER_HOSTNAME" --worker-pid "$WORKER_PID" \
  --worker-started-at "$WORKER_STARTED_AT" --retire-worker --dry-run \
  --expected-revision 0 --actor operator --reason planned-maintenance --authorized
```

After review, replace `--dry-run` with `--apply`. `REQUESTED` stops that incarnation from acquiring
new claims or adopting another owner's work. Heartbeats, authentic completion, cancellation, and
cleanup of its existing work remain available. Requesting retirement does not expire its lease or
assert that its callbacks, probes, or remote Jobs stopped.

Final `RETIRED` requires the trusted worker control path to independently confirm its owned
cleanup outside database locks, then atomically record the bounded cleanup receipt and mark the
exact lease inactive. The service also refuses finalization while owned running/cancelling tasks,
unresolved claims, open Jobs cleanup obligations, or capabilities remain. There is no CLI flag that turns an operator assertion
or zero SQL rows into that proof. A retained `REQUESTED` audit whose lease was deleted remains
unverified; a later incarnation does not inherit it. Retirement history does not add a permanent
foreign-key blocker to ordinary lease retention.

An authenticated Jobs task result can arrive before its driver exits. The separate cleanup
obligation survives manager restarts and queued retries; a new generation remains blocked until
a fresh inspection corroborates terminal cleanup of that exact original Job. A qualified successor
may take over cleanup without changing the original result or claim. If the retained request
reference is unavailable, the result stays truthful and the obligation stays openly uninspectable.
Neither a terminal task nor an unavailable Job record closes it automatically.

Manual retry starts a new generation, so both enqueue and claim admission must allow it,
including any pause for the original bound target. A paused policy, active quarantine or
pending cleanup refuses retry before reading the stored RuntimeEnv. Historical protocol-1/2
terminal results remain readable, but retry does not translate them into current work.

Retention protects the exact request reference retained by an open cleanup obligation,
even after a retry clears the task's mutable request fields. An uninspectable open obligation
blocks request-payload purge throughout that database because its request cannot be attributed
safely. Resolve that cleanup uncertainty before retrying request-payload purge; it does not
prevent unrelated task-input retention.

## Quarantine or release one task generation

Supply the execution's database primary key, task ID, attempt number, and execution generation.
Generation zero is valid for an execution that has never been claimed. Exact selectors alone read
status; add one action and an explicit change mode to mutate it.

```bash
python manage.py django_ray_maintenance --task-pk "$TASK_PK" --task-id "$TASK_ID" \
  --attempt "$ATTEMPT" --generation "$GENERATION" --json

python manage.py django_ray_maintenance --task-pk "$TASK_PK" --task-id "$TASK_ID" \
  --attempt "$ATTEMPT" --generation "$GENERATION" --quarantine-task --apply \
  --expected-revision 0 --actor operator --reason investigate-execution --authorized

# Release only after reviewing the exact current identity and control revision.
python manage.py django_ray_maintenance --task-pk "$TASK_PK" --task-id "$TASK_ID" \
  --attempt "$ATTEMPT" --generation "$GENERATION" --release-task --apply \
  --expected-revision 1 --actor operator --reason investigation-complete --authorized
```

Quarantine preserves the execution state, original claim, remote handle, and any `HELD` uncertainty.
It blocks new claims and retry/requeue, including attempts to escape the fence by incrementing the
attempt or generation. Authentic same-generation completion and cancellation remain allowed. A
failed completion can become terminal while quarantine prevents its retry. Completion does not
release quarantine automatically; the explicit CAS release appends a separate audit event.

Revisions increase across this execution's control history, including later generations after a
release. JSON `control_identity` identifies the generation recorded by the latest control event;
`identity` identifies the requested execution generation. A release removes the control fence but
does not retry, resume, or otherwise execute the task. Active quarantine blocks task deletion;
released audit snapshots can survive ordinary task retention without a permanent foreign key.

Entity controls cannot be mixed with global, queue, protocol, or target scope changes. All changes
use the same explicit actor, reason, authorization acknowledgment, dry-run, and revision-CAS rules.
Neither entity state certifies deployment-wide `DRAINED`.
