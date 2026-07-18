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

Use Conventional Commit syntax for commit messages and PR titles. See `CONTRIBUTING.md` for examples
and the CI-equivalent validation sequence. Run the full local gate as `uv run make ci`; do not add
nested `uv run` wrappers inside that target because real-Ray workers inherit the outer environment.

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
