# Compatibility and Version Policy

## Supported Versions

| Component | Supported |
|---|---|
| Python | 3.12, 3.13, 3.14 |
| Django | 6.0 or newer compatible release |
| Ray | 2.53.0 or newer compatible release |
| Production operating system | Linux recommended |

Python 3.12 is the floor because Django 6.0 requires Python 3.12+, not because Ray does.
Current [Ray releases support a wider Python range](https://pypi.org/project/ray/).

Ray Client and cluster execution are most predictable when the task manager and Ray
cluster use the same Ray version and Python minor version. Patch-version differences
may produce a warning; different minor versions should not be treated as compatible.

## Dependency Policy

`pyproject.toml` uses lower bounds so applications can resolve compatible updates
instead of being locked to the versions used for one django-ray release. The committed
`uv.lock` gives contributors and CI a reproducible current environment.

CI covers:

- the committed lock on every supported Python minor;
- minimum direct dependencies on the oldest supported Python;
- the newest resolvable dependencies on the newest supported Python;
- package installation from the built wheel on every supported Python minor.

Updating the lock is therefore separate from raising a package's minimum supported
version. A lower bound should move only when django-ray uses a newer API or the older
dependency is no longer supportable.

## Platforms

Ray publishes platform-specific wheels. A pure-Python django-ray wheel does not imply
that Ray is available on every Python/platform combination.

- Linux is the production target for clusters and Kubernetes.
- Ray's Windows multi-node support remains experimental; Windows is useful for local
  development and tests.
- Ray publishes Linux aarch64 wheels for supported Python versions, but users must
  confirm their OS, architecture, and Python ABI match an available Ray wheel.

## piwheels

[piwheels automatically processes PyPI releases](https://www.piwheels.org/faq.html);
projects do not upload or configure build rows themselves. Its project table shows the
Python versions associated with supported Debian releases. Those columns are not
django-ray's support declaration.

The [piwheels JSON metadata for django-ray](https://www.piwheels.org/project/django-ray/json/)
correctly carries
`requires_python: ">=3.12"`. Pip will use that metadata when resolving an install.
The displayed django-ray wheel is `py3-none-any`, while the Ray dependency still needs
a compatible platform wheel.

If the project should disappear from piwheels entirely because no useful Ray
installation exists for the platform, the available mechanism is to contact the
piwheels maintainers and request a skip flag. There is no setting in
`pyproject.toml` that customizes their version grid.
