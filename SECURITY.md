# Security Policy

## Supported versions

django-ray has not reached 1.0 and does not maintain parallel security-support
branches.

| Release | Security support |
|---|---|
| Latest version published on PyPI | Supported |
| Earlier versions | Not supported; upgrade to the latest release |
| `main` and other unreleased code | Development only; not a supported release |

Supported means that a release is eligible for a security fix. It does not
guarantee that every report will be accepted or that a fix will arrive within a
particular time. Fixes normally land on `main` and ship in the next release;
backports are at the maintainers' discretion.

## Report a vulnerability privately

Use GitHub's
[private vulnerability report](https://github.com/dariuszpanas/django-ray/security/advisories/new)
for a suspected security vulnerability. When in doubt, report privately.

Do **not** open a public issue, discussion, or pull request containing:

- exploit instructions, triggering payloads, or a proof of concept;
- credentials, tokens, private keys, customer data, or other secrets;
- unredacted logs, screenshots, task inputs, task results, or diagnostics that
  may contain sensitive data.

If the private form is unavailable, open a public issue that asks only for a
private security contact. Do not identify the vulnerable component or include
reproduction details in that issue.

Include only the information needed to investigate, such as the affected
django-ray version or commit, deployment context, potential impact, and a
minimal reproduction using synthetic data. State whether the problem is already
public or known to be exploited. Remove real credentials and private data even
from the private report.

## What happens after a report

Maintainers will handle reports as capacity permits; this project does not
promise a response, fix, or disclosure deadline. The intended process is to:

1. triage the report and ask follow-up questions in the private advisory;
2. determine affected releases and keep actionable evidence private while users
   may still be exposed;
3. develop and test a fix before publishing exploit-enabling details;
4. use only a sanitized, non-actionable public issue when public coordination is
   useful; and
5. disclose proportionately after a fix is available, using a GitHub security
   advisory, changelog entry, or release note as appropriate.

Please state any disclosure constraints in the private report so timing can be
coordinated. Reporter credit can be discussed there.

## Public hardening work

Ordinary bugs, defense-in-depth ideas, and non-sensitive security design can
remain in [public issues](https://github.com/dariuszpanas/django-ray/issues).
Move concrete exploit evidence or details that make an unresolved weakness
actionable to the private advisory.
