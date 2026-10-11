# Observed cancellation

The production cancellation contract is best effort. A durable `CANCELLED` row and a
`REQUESTED` backend outcome do not themselves prove that execution stopped.

The existing hosted live Core scenario now waits for actual callable progress, then calls
the production fenced lifecycle request and worker cancellation path. It independently
requires `TaskCancelledError` for the exact ObjectRef and a separate actor's observation
of the callable leaving its `finally` block. A no-cancel control must continue progressing
until explicitly released, then complete. The scenario retains its existing isolated
165-second TERM/180-second hard deadline and cleans up only its own tasks, actor and lease.

The separate Jobs fixture builds the candidate wheel with the existing PostgreSQL/tools
image and verifies its installed package against the exact source. It owns a local
socket-only PostgreSQL cluster, Ray runtime, dedicated queue and manager at concurrency one.
Both fixed callables publish a bounded local marker binding their durable identity and nonce
to their real PID, Linux process start time, UID and PID namespace. The observer reads only
those owned `/proc` entries, confirms live progress, and never sends a signal to a marker PID.

The control must remain alive without cancellation, then complete the same Job after an
explicit release and exit. The cancellation target is interrupted through the production
fenced lifecycle request after its driver starts. Passing evidence separately requires the
accepted request, durable `CANCELLED`/`REQUESTED` state, Ray Jobs `STOPPED`, and disappearance
or exit of that exact original OS process. PID reuse cannot revive the original identity;
a zombie is exited, with final runtime cleanup still required. Four seconds of stable
progress observations follow confirmed exit. The attempt, generation and request reference
must remain unchanged, with one cancelled archive and no result, completion or scheduled retry.
No marker claiming "stopped" can replace process evidence. Raw identities remain inside
the disposable fixture; exported receipts contain fixed facts and failure categories.

This proves termination of the controlled driver and callable. It does not prove that
arbitrary descendants or external effects stopped, add exactly-once execution, or change
cancellation policy. It is separate from the application and request-reference phases.

The container retains the admitted native bounds: 2 CPUs, 8 GiB memory, 1024 PIDs,
512 MiB shared memory, 3 GiB temporary storage, no network, a read-only root, UID 10001
and no extra capabilities. The child has a 150-second work alarm and a 180-second outer
process deadline, reserving cleanup time. The complete installed-wheel experiment has a
300-second hard command ceiling; its hosted wrapper interrupts at 280 seconds and permits
20 seconds for exit. Forced cleanup cannot produce a passing receipt. Cleanup failures
remain distinct from the first experiment failure. The outer workflow always removes its
owned Compose resources and retains only bounded evidence.

`Qualification Gate` explicitly selects this reusable workflow for affected paths and rejects
missing, failed, cancelled or skipped selected evidence. For a frozen candidate after its
exact-source Linux CI checkpoint:

```sh
gh workflow run cancellation-qualification.yml --ref main
```

Selection includes the consumed runner family: cancellation, lease ownership, polling,
reconciliation and retry decisions can all change the observed stop boundary. It also
includes worker recovery in the command, settings, enqueue, durable identity/protocol,
request/result storage, entrypoint and RuntimeEnv/serialization imports. Jobs recovery
is part of the selected worker command. Unrelated Admin, workflow presentation and
dormant target controls are outside this fixture's selected scope.

Verify the retained source commit/tree, installed package identity, fixed facts and cleanup
before accepting the result. Fixture definitions, local tests and earlier-source receipts
are not evidence of a passing native run on a changed candidate.
