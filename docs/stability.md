# Stability and Deprecation Policy

django-ray 0.x releases are Beta releases. They document compatibility boundaries and
provide migrations, but they do not yet make the complete source-compatibility promise
described on this page. This page defines the **candidate contract for 1.0** so it can
be reviewed and tested before the Beta classifier is removed.

## Contract classes

Every adopter-facing surface belongs to one of these classes:

| Class | Promise at 1.0 |
|---|---|
| **Stable** | Preserved throughout 1.x, subject to the deprecation policy below |
| **Experimental** | Opt-in, fail-closed, and allowed to change or be removed in a minor release |
| **Private** | An implementation detail with no compatibility promise |

An undocumented import is private even when its name does not begin with an
underscore. Documentation examples do not make an import stable unless it also appears
in the checked public API inventory at `tests/contracts/public_api_v1.json`.

## Proposed stable 1.0 surface

The following behavior is intended to become stable at 1.0:

- the documented `RayTaskBackend` configuration and Django Tasks enqueue/result
  behavior;
- the documented `DJANGO_RAY` settings, queue declarations, and supported worker-mode
  selection;
- durable task states and the documented retry, cancellation, expiry, ownership, and
  reconciliation outcomes;
- the public lifecycle result objects and status values returned by authorized
  cancellation and retry adapters;
- the dynamic workflow constructors and their entry-point replay boundary;
- versioned observability services and bounded Prometheus metric names, labels, and
  meanings;
- documented result-storage backends, reference validation, and integrity behavior;
- documented management-command names, options, machine-readable formats, and exit
  meanings;
- additive database migrations and compatible readers for durable formats supported by
  the current release.

The checked inventory records stable Python import locations and parameter names. A
compatible release may add optional keyword parameters, result fields, enum values, or
new APIs when existing callers continue to work and documented exhaustive consumers
have an explicit forward-compatibility rule.

## Experimental surface

The following remains experimental unless a later capability-specific promotion says
otherwise:

- Ray Compiled Graph and every compiled execution strategy;
- native Windows Ray execution beyond best-effort local development;
- GPU and zero-copy transport;
- Ray Data, Train, Tune, RLlib, Serve, and Serve LLM integration beyond the documented
  application-owned boundary;
- research pilots, benchmarks, capability probes, and modules documented as
  experimental.

Experimental features must fail closed when their exact capability requirements are
not met. Their data must not weaken the stable task lifecycle or make stable work
unreadable.

## Private and example surface

Internal runner, reconciliation, storage-codec, workflow-progress, Admin rendering, and
test support modules are private unless named in the public inventory. Django model
fields not explicitly described as application query fields are storage details rather
than an ORM-level compatibility promise.

The bundled `testproject` HTTP API, HTML, CSS, JavaScript, Kubernetes development
overlays, and demonstration tasks are examples. They may provide migration notes, but
they are not package APIs. Applications own authorization, tenancy, request schemas,
and HTTP compatibility for adapters built from those examples.

## Deprecation policy

After 1.0, a stable surface may be removed or incompatibly changed only in the next
major release. Before removal, django-ray will normally:

1. document the replacement and migration;
2. emit an actionable warning when runtime detection is safe and bounded;
3. retain the deprecated surface for at least one feature release and at least 90
   days; and
4. record the removal in the changelog and migration documentation.

Security, integrity, or data-loss risks may require an accelerated change. Such a
release must document the reason, affected versions, safe replacement, and operational
action. A compatibility shim is not required when retaining it would preserve the
vulnerability.

Warnings must not include task arguments, results, credentials, RuntimeEnv contents,
storage references, or unredacted diagnostics. A warning must identify the deprecated
surface, its replacement, and the earliest eligible removal release.

## Versioning boundaries

Package SemVer describes the public application contract. It is not the durable
execution protocol, workflow progress schema, result-reference format, RuntimeEnv
identity, or observability schema version. Those formats have independent versions and
compatibility checks.

A package upgrade must never infer persisted-format compatibility from the package
version alone. Workers and readers must use the relevant protocol or schema version,
reject unsupported work before application invocation, and preserve a visible
operator recovery path.

Metrics follow the same public compatibility rules as code. Removing or renaming a
stable metric, label, or documented state meaning is breaking. Adding a bounded label
or state requires a forward-compatibility note when exhaustive consumers could reject
it.

Management commands follow compatibility rules for command names, documented options,
machine-readable output fields, and exit meanings. Human-readable prose may improve
without a deprecation cycle unless documentation promises an exact string.

## Release enforcement

The public inventory is a candidate while django-ray remains below 1.0. Contract tests
ensure every listed import and parameter remains available during the stabilization
cycle. Before publishing 1.0, release validation must additionally require this policy,
the accepted inventory, the final compatibility matrix, and the 1.0 graduation
evidence. That release-gate activation belongs to the final 1.0 release-candidate work;
this policy does not remove the Beta classifier by itself.
