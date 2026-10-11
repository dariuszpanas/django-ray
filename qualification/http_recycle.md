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

The current client constructs and closes a connection on every attempt, so
stale client keepalive reuse is not its mechanism. The source investigation is
grounded in Gunicorn's pinned [gthread drain loop](https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/workers/gthread.py#L389)
and [worker replacement/bootstrap sequence](https://github.com/benoitc/gunicorn/blob/26.2.0/gunicorn/arbiter.py#L642).
Keep original failed receipts and their missing caller/deadline attribution
separate from this controlled mechanism proof.
