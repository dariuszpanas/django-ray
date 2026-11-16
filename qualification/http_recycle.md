# Single-worker HTTP recycle experiment

`tests/unit/test_http_recycle_probe.py` owns one loopback listener at a time and
one outer Linux process group for at most 20 seconds. It uses the locked sample
Gunicorn and the real `ApplicationHttp` client. A test-only gthread subclass
records fixed listener-enabled/disabled events after the real transition; WSGI
records fixed request-entry events. No request headers, identities, credentials,
response text, public service or Ray process is involved.

The first case holds one request while a second reaches `max_requests=2` with
one worker and two threads. After the listener is disabled, a fresh GET must
time out before response headers and before entering the held worker's app.
Timeout retry remains forbidden. Releasing the held request must let a different
worker start accepting and serve a fresh GET. The no-recycle control requires
that GET to succeed while its first request remains held. Both cases require
the held request to finish and their owned server/client cleanup to complete.
The outer owner retains its leader with Linux `WNOWAIT` through its final group
signal, then reaps it; a missing receipt or unconfirmed cleanup fails.

Run the explicit Linux proof with:

```sh
REQUIRE_HTTP_RECYCLE_PROOF=1 uv run --frozen --extra sample pytest \
  tests/unit/test_http_recycle_probe.py -q -s
```

Ordinary environments without Linux and the locked sample dependency skip the
native case. The explicit proof refuses that skip. Passing this fixture proves
that a single-worker drain can produce the observed failure *category* on a
fresh connection. It does not identify the cause of historical issue #613
samples, measure real application startup under resource pressure, or justify a
retry, timeout, worker-count or recycle-policy change. In particular, Gunicorn's
worker-boot log precedes WSGI loading and is not serving-readiness evidence.

The test-only worker fixture loads its optional Gunicorn module through
`pytest.importorskip`, so typing supports both the development environment and
development plus the sample extra. The documented command supplies pytest to
the child fixture. This does not relax the outer admission check: required proof
still fails when Linux or the exact locked Gunicorn is unavailable. Resource-free
tests replace that optional import and verify listener observations happen after
worker delegation; they do not import or start the Unix server on other hosts.

The current client constructs and closes a connection on every attempt, so
stale client keepalive reuse is not its mechanism. In the locked Gunicorn 27.0.1,
the [request-count limit](https://github.com/benoitc/gunicorn/blob/27.0.1/gunicorn/workers/gthread.py#L750-L771)
marks the worker no longer alive before invoking WSGI. The
[drain loop](https://github.com/benoitc/gunicorn/blob/27.0.1/gunicorn/workers/gthread.py#L406-L458)
then disables new accepts while draining existing connections. The arbiter
[replenishes missing workers](https://github.com/benoitc/gunicorn/blob/27.0.1/gunicorn/arbiter.py#L694-L700),
and its [bootstrap sequence](https://github.com/benoitc/gunicorn/blob/27.0.1/gunicorn/arbiter.py#L726-L752)
logs worker boot before initialization. [WSGI loading precedes the worker run loop](https://github.com/benoitc/gunicorn/blob/27.0.1/gunicorn/workers/base.py#L138-L146),
so that boot log still does not establish serving readiness. These source paths
explain the mechanism exercised by the required locked-server experiment; they
do not replace its observed recycle/control and cleanup assertions.

The historical source investigation used Gunicorn 26.2.0's
[gthread drain loop](https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/workers/gthread.py#L389)
and [worker replacement/bootstrap sequence](https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/arbiter.py#L642).
Those links remain historical grounding, separate from the current locked-server
proof. Keep original failed receipts and their missing caller/deadline
attribution separate from this controlled mechanism proof.
