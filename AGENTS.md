# Repository agent guidance

These instructions apply to the whole repository. Read `CONTRIBUTING.md` before changing code or
opening a pull request.

## Instruction precedence

Follow platform and explicit user instructions first. Within the permitted workflow, this file,
`CONTRIBUTING.md`, and repository configuration override generic tool or skill defaults. In
particular, use this repository's branch prefixes instead of a generic `agent/`, `codex/`, or similar
prefix. A more deeply nested `AGENTS.md` may add instructions for its subtree.

## Start safely

Before editing, inspect:

```bash
git status --short
git branch --show-current
git log -1 --oneline
```

Identify unrelated or concurrent work and preserve it. Do not restore, format, stage, stash, or move
another contributor's changes. If they block a branch switch, use a separate worktree or ask before
changing their state.

When a task calls for a branch, start from current `main` and use the conventions in
`CONTRIBUTING.md`. Feature work defaults to `feat/<short-topic>`.

## Make reviewable changes

- Keep the change scoped to the issue and avoid opportunistic rewrites.
- Inspect surrounding source, tests, migrations, documentation, and persisted protocols.
- Add or update tests for behavior changes.
- Stage explicit paths; never default to `git add .` in a shared or dirty worktree.
- Review `git diff` and `git diff --cached` before reporting completion.
- Report the exact validation commands run, their results, and anything not run.
- Treat each material retained logical commit as a portable, PR-grade change record. One large atomic
  commit is valid, headings are optional, and its body must preserve the applicable behavior,
  motivation, boundaries, rollout impact, validation, and useful repository-local investigation
  paths without relying on GitHub metadata.
- Before every push and before enabling auto-merge, compare each material commit body with the PR
  description for the same material facts. Wrap commit prose at 72 columns; format PR descriptions
  as natural Markdown without artificial hard wrapping.
- Before every push and before enabling auto-merge, fetch `origin` and inspect
  `git log --format=fuller origin/main..HEAD`.
- Before merging, verify both required checks are green: `Commit Messages` for retained history
  (or the PR title for its exact trusted Dependabot path) and `CI Gate` for the complete blocking
  matrix. A skipped package job or an enabled merge button is not sufficient evidence by itself.
- Fold fixup, CI-repair, review-repair, formatting follow-up, and other development-only commits into
  the logical commit they correct. Preserve genuinely independent commits with their own
  self-contained descriptive bodies and validation evidence.
- Validate the retained range with
  `uv run python scripts/check_conventional_commits.py --range origin/main..HEAD`.

Use Conventional Commit syntax for commit messages and PR titles. See `CONTRIBUTING.md` for examples
and the canonical `.gitmessage` template. Run the full local gate as `uv run make ci`; do not add nested
`uv run` wrappers inside that target because real-Ray workers inherit the outer environment.

Before handing off deployed-behavior changes, consult the trigger matrix in
`docs/deployment/local-kuberay-gate.md`. A required row must pass the guarded local KubeRay gate from
a clean checkout after `uv run make ci`. Record a concise semantic validation summary in every
applicable retained commit and PR: the exact gate command and result, the explicit cold-Ray decision,
the verified source-tree match, and the relevant workload, API/task-smoke, and preservation outcomes.
A recommended row needs either the same passing summary or a specific reason it was not run. Keep the
complete secret-free evidence block as runtime diagnostics; do not paste its image IDs, pod hashes,
cluster UIDs, checksums, or similar run-specific identifiers into durable Git history by default. If
an investigation needs one, retain it in a focused issue or PR comment or diagnostic artifact and
explain how it will be used. Never copy the API token or unbounded cluster logs into Git history.
After amending only the commit message with the summary, verify that the emitted `source_tree` still
equals `git rev-parse HEAD^{tree}` without recording the hash. Any tracked tree change invalidates the
evidence and requires a new run.

## Local heavy-resource coordination

Before starting or diagnosing heavy local validation, inspect the daemonless host-wide coordinator:

```bash
uv run make local-resources
```

Use `LOCAL_RESOURCES_FORMAT=json` when another tool needs the bounded schema. Phase 1 coordinates only
the fixed `ci-final`, `real-ray`, and `kuberay-final` profiles on one conservative `host-heavy` lane.
`uv run make ci`, a non-collection pytest session whose final selection contains `real_ray`, and the
full guarded KubeRay gate enter that lane through their supported wrappers. Do not invoke private Make
targets or bypass the coordinator. KubeRay preflight-only remains non-mutating and does not acquire the
lane. The public full-gate wrapper runs that direct check first, then the coordinator owns and
contains the repeated full gate before image or Kubernetes mutation. Only its final post-release
Make line is definitive success.
Contained coordinator runs are supported only on Windows, Linux, and macOS; on other POSIX hosts
they fail before lane acquisition because Phase 1 has no stable native process-birth identity, and
contributors must not bypass the coordinator.

Treat the reported safe action (`safe_action`) as the operator boundary. An OS-held lock is
authoritative; PID, heartbeat age, process name, ports, lock-file contents, Docker or Kubernetes
objects, and vault text are diagnostics only. Never kill, signal, delete, or take over another task's
process or stack from those observations. A live recorded child after its owner lock disappears is
`orphaned`, blocks the lane, and grants no termination authority. The registry is private from other
OS users, but it is a cooperative coordination mechanism among processes running as the same OS
user, not a security boundary against a malicious same-user process.

Phase 1 does not yet coordinate standalone PostgreSQL tests, Docker Compose, or manual Ray/Docker/
Kubernetes probes. Those commands still require an explicit live handoff and must not overlap an
active heavy lane. The ignored Obsidian vault may record narrative handoff context, but it is never
the queue, lock, cancellation capability, or process-termination authority.

## Optional Obsidian project memory

When a repository-local Obsidian vault is available (for example, `.vault/*/.obsidian/`), use it as a
retrieval and handoff layer. It supplements the repository; source, tests, migrations, configuration,
and observed behavior remain authoritative.

Use it efficiently:

1. Read the vault's agent entry point or Home note, then its current-workspace note.
2. Check the note's branch and source commit against Git before trusting synthesized claims.
3. Search for the task's symbols or invariants and read only the linked architecture/review notes you
   need. Do not ingest the whole vault by default.
4. Keep task-local hypotheses in one per-task or per-agent working note. Mark uncertainty and include
   a verification path.
5. At handoff, record the outcome, owned files, tests, unresolved risks, and next action. Promote only
   confirmed, reusable facts into durable architecture, decision, or review notes.
6. Run an unresolved-link check after structural changes when the CLI is available.

Typical CLI queries are:

```bash
obsidian files vault=<vault-name>
obsidian search vault=<vault-name> query="execution_generation"
obsidian unresolved vault=<vault-name> total
```

The executable may be named `obsidian`, `Obsidian.exe`, `Obsidian.com`, or be exposed through another
local wrapper.
If the vault or CLI is unavailable, continue from repository sources without installing or creating
anything unless the user asks. Never store credentials, tokens, customer data, or production secrets
in project memory, and do not commit an ignored local vault.
