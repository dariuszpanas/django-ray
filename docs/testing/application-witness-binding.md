# Application witness binding diagnostics

The host-side checker binds a historical Application witness to GitHub metadata,
the two original artifact uploads, and local immutable source objects. It is a
read-only diagnostic for [#469](https://github.com/dariuszpanas/django-ray/issues/469).
It does not perform product receipt validation or admit qualification reuse.

Its positive result is `metadata_artifact_binding_verified`, with
`metadata_binding_verified=true`. Every result states:

```json
{
  "semantic_validation": "not_performed",
  "qualification_admission": false,
  "reuse_enabled": false,
  "complete_application_gate": false,
  "self_authenticating": false,
  "current_head_execution": false,
  "authorizes_workflow_review_or_merge": false
}
```

**Exit 3 means binding verified, with product semantics still unverified.** Exit 1
means rejected data and exit 2 means unavailable or unsupported evidence. There is
no qualification-success exit code. Help exits normally. This checker creates no
check, review, approval, issue, workflow execution, merge, or replacement origin
witness. Current-head CI, selected fresh recipes, reviews and rulesets remain
authoritative. Verification is not a security, release, adopter or coordinated
upgrade certificate.

## Run a trusted copy

Use a separately reviewed, immutable checker copy and trusted Python 3.12 or later,
Git and `gh` executables. Its private modules and fixed definition inventories are
part of that reviewed copy. Do not execute the checker, run an installer, invoke
`uv run`, or import a validator from an arbitrary candidate checkout. `python -I`
is mandatory; it excludes the caller's current directory and `PYTHONPATH` from
Python module search. The entrypoint loads only its own private package by an
explicit trusted path. The source repository argument supplies object data.

For example, set the opaque identities from a reviewed historical run, then use:

```sh
/trusted/python -I /trusted/django-ray/scripts/verify_application_witness.py \
  --repository-id 790856348 \
  --pr "$PR" --run "$RUN" --attempt 1 \
  --source "$ORIGINAL_HEAD" \
  --receipt-artifact "$RECEIPT_ARTIFACT_ID" \
  --witness-artifact "$WITNESS_ARTIFACT_ID" \
  --git-repository /local/original-git-data \
  --comparison-source "$COMPARISON_HEAD" \
  --output /outside-git/new-binding-report.json
```

The optional comparison reports only whole-tree equality. Archive reproduction,
runtime labels, source and image observations stay associated with the original
head and original run. Equal trees do not mean that a new head was built or
executed, and do not freeze Python images, APT, browser delivery, external
configuration, secrets or other mutable build inputs.

The output parent must already exist, reside outside Git repositories, and contain
no symbolic links or junctions. The checker exclusively creates a new report and
prints bounded JSON. It never overwrites an earlier attempt. Preserve rejected
attempts alongside any later binding result. Existing GitHub authentication is
read through `gh auth token --hostname github.com`; no credential is created or
included in a command argument, output, exception, or report.

## Supported historical profile

Version 1 supports owner-authored, same-repository `pull_request` callers, attempt
1, standard resources and acceptance intent. It checks numeric repository,
owner, author, actor and triggering-actor IDs. The original caller's authenticated
referenced workflow must name the exact PR merge ref, path and immutable merge
commit. Its two ordered parents must match the historical base and original
source head. The original merge tree and source tree are separate identities.

GitHub can return an empty run `pull_requests` array after a PR merges. That case
is supported only when the authenticated called-workflow ref still identifies
the exact PR, and the current owner/same-repository PR head remains the original
source head. A missing or wrong caller ref, a contradictory run association, or
an empty association with a changed head refuses verification. A witness or
saved local context cannot supply missing GitHub authority. An old run never
authorizes workflow execution, review, reuse or merge for an external new head.

The fixed definition inventories identify 100 source blobs for each of two known
versions: merged witness source `b98fa1b` and reviewed dependency source
`784da1a`. Full commit and blob IDs in `qualification/witness_binding/profiles.json`
are essential trust data, rather than runtime diagnostics. The checker verifies
that baked-in inventory's tracked-LF digest (normalizing CRLF checkout line
endings) and matches the caller/called workflows,
producer/profile/validator files, Application fixture, build files and Kubernetes
definitions without importing their Python code. Unknown versions, additional
Application/Kubernetes definitions, and changed definitions are unsupported.
For example, a later Gunicorn lock update needs separate definition admission.
There is no arbitrary policy-file or revision argument that adds a trust root.

This byte inventory is **not a complete executable semantic closure**. The
existing receipt validators import runnable workload dependencies. Extracting and
reviewing a small trusted pure semantic bundle is separate work. Version 1 checks
the presence and binding of 16 layer receipts plus two protected-log receipts; it
does not claim those 18 product contracts passed its own semantic validation.

## Binding checks and limits

The checker obtains fresh repository, PR, workflow, run/attempt, job, artifact and
Git-object metadata through fixed GitHub GET routes. It requires the completed
Application job's nine ordered successful source steps and successful producer
and witness-upload steps. Both explicit immutable upload IDs must have the exact
names, run/head, sizes, digests and matching successful upload time windows.
Replaced, expired, missing or rerun evidence is refused; no newer upload is chosen
by name. Before returning, the checker rereads metadata and bounded histories to
reject observed context changes and newer exact-head failed or pending runs.

Both raw ZIPs are size- and SHA-256-checked against the API before parsing. ZIP
topology is validated before member access, including duplicate/case aliases,
unsafe paths, links, encryption, unsupported methods, overlapping local records
and inconsistent sizes. Only the exact witness member and 25 allowlisted receipt
and build members are opened in memory. Diagnostic log/event members remain
unopened. Image metadata contributes only the required digest/layer fields;
image environment values are never projected into diagnostics.

The original source commit must be available in the supplied local object store.
An exclusively owned temporary bare reader references that store through a local
alternates pointer. It creates no source refs/index, inherits no templates or
original repository configuration, suppresses system/global configuration and
inherited `GIT_*` overrides, disables replacement objects and lazy fetch, and
denies transport protocols. Git tools run with the owned reader as their current
directory. A binding-positive report is emitted only after owned-reader cleanup succeeds.
Cleanup failure produces unavailable evidence and cannot retain a binding-positive
report.

The reader supports Git 2.43 without the newer global `--no-lazy-fetch` option.
Its private configuration contains no remote or partial-clone authority, and
`GIT_ALLOW_PROTOCOL` is an empty allowlist that denies every transport even if a
per-protocol configuration permits it. `protocol.allow=never` remains an additional
guard. `GIT_NO_LAZY_FETCH=1` is retained for newer Git versions; Git 2.43 does not
implement that environment guard. Candidate promisor configuration is never read.
Missing local objects remain unavailable evidence; no fetch fallback is attempted.

Commit, tree and blob contents are checked against their Git object header/body
hashes, including reconstructed tree edges. Object filenames alone are not
trusted. Only the requested source's bounded object graph is checked; no full
history scan or implicit fetch occurs. The Linux-format Git tar is reproduced
from the **original commit**, whose identity is embedded in tar metadata. Source
files and the deterministic runtime ZIP are compared without executing them.

The invocation admits operations for 233 seconds and reserves seven seconds of
its 240-second budget for owned tool cleanup. Each HTTP request, including its
redirect chain, has an absolute 30-second shutdown guard. Progress reads avoid
resetting that deadline for a dribbling peer. Connection setup shares that
deadline. Initial headers and chunked trailers share limits of 8 KiB per line,
32 KiB total and 64 lines; body payload does not consume those limits. Fixed
limits also include:

| Input | Limit |
|---|---|
| GET attempts, including redirects | 48; no polling or retry loop |
| Response bodies | 14 MiB total; 2 MiB per metadata response |
| Original ZIPs | 1 MiB each; 128 members and 4 MiB declared expansion |
| Selected receipt files | 256 KiB each; 2 MiB total |
| Canonical witness | 32 KiB |
| Git objects | 4,096 requested objects; 64 MiB payload total |
| Original source tar | 64 MiB; 4,096 members |
| Relevant history | 32 runs each for the original and current PR heads |
| Jobs and steps | 64 jobs; 32 steps per job |
| JSON structure | Depth 32; 40,000 values/keys; finite bounded numbers |
| Report | 64 KiB; fixed failure categories |

Credentials are sent only to `api.github.com`. Artifact redirects allow at most
three HTTPS transitions to the explicitly reviewed Azure artifact host pattern,
without credential forwarding, userinfo, private addresses or deadline resets.
DNS-selected public addresses are pinned for each TLS connection. Signed URLs,
headers, raw error bodies, raw logs and events are not retained or printed.
Ambient proxy routing and other storage origins are unsupported.

These are bounded reader and host-tool controls, not measured physical memory
peaks, enforced OS resource certification or validation savings. Per-invocation
GET/body/time observations belong to outside-Git diagnostics. Missing local
objects, unavailable API provenance, unsupported definitions or required truncated
inventories produce unavailable evidence rather than a weaker match.

The history check does not prove absence of failures across all prior
equivalent-tree PR heads. No freshness TTL is adopted: original workload
completion and verification times remain distinct, and rereading never resets
origin age. Trusted semantic extraction, complete failure-history policy, TTL,
reuse admission and immutable platform/material retention remain open under #469
and [ADR-0008](https://github.com/dariuszpanas/django-ray/blob/main/adrs/adr-0008-qualification-execution-witnesses.md).
