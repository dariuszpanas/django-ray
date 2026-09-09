# Shared application assertions

`api.py` contains the source-owned HTTP assertion layer used by
`scripts/local_kuberay_gate.py`. A bounded assertion Job can import this module using only the
Python standard library. It does not import the host gate, Django, Ray, or Kubernetes tooling.

`verify_application_api` receives an HTTP transport, a credential supplier and the task deadline.
It retains the existing gate checks: unauthenticated rejection, safe OpenAPI routes, authenticated
statistics and protocol metrics, canonical enqueue identity, bounded task polling and provenance,
durable result `5`, and rejected execution deletion with unchanged detail. The host gate calls the
same function, so these assertions have one implementation.

The caller owns URL scope, redirects, credential handling, per-request timeouts, response byte
limits and required response headers. The function specifies the OpenAPI and task-status limits
and required polling headers through its transport protocol; omitted limits use the transport's
bounded default. Supply a positive finite task timeout. The credential supplier is invoked only
after unauthenticated protection and schema checks pass.

The returned `ApiEvidence` records observations from this layer. A caller may instead supply a
compatible mutable evidence object, as the host gate does, to retain partial progress and the
enqueued task identity after a later assertion fails. Success requires the function to return;
partial flags do not establish a passing layer. Credentials and raw response bodies are not stored
in that evidence object.

This extraction does not change the supported application images, create a DRT runbook, or replace
the required KubeRay gate. Packaging the assertion Job, proving cold Ray generations, manager and
workflow recovery, RuntimeEnv delivery and encryption, and the remaining deployment assertions
remain tracked in django-ray issue #455. Source/image identity and bounded run evidence must be
established by that workload and its executor before a DRT result can serve as application proof.

## Running the API layer

From a source checkout or an assertion image containing this directory:

```sh
python -m qualification.application.run_api \
  --base-url http://django-web:8000 \
  --token-file /run/application-credentials/DJANGO_API_TOKEN \
  --task-timeout 180
```

The caller provides the admitted application service origin and a mounted application token file.
The token must contain 32–512 ASCII token68 characters, without a trailing newline. It is read only
after the unauthenticated and schema assertions pass. HTTP(S) origins cannot include credentials,
paths, queries or fragments. The transport ignores ambient proxies, does not follow redirects,
verifies HTTPS with the standard system trust store, and enforces response byte limits for success
and error responses. Each blocking socket operation has a ten-second timeout. Task polling accepts
a positive finite timeout of at most 600 seconds; the enclosing Job must also set a hard execution
deadline because socket timeouts do not bound an entire slow-streaming response.

Exit zero means the shared API function returned successfully. A failed assertion exits one. Both
outcomes emit a single JSON receipt containing the layer name, status, elapsed time, request count,
last HTTP status and typed partial observations. Failure messages and raw bodies are omitted because
they may contain application data. Partial observations remain diagnostic even when the layer
fails. The receipt always reports `complete_application_gate: false`; it does not establish image
identity, Ray generation, manager ownership, cleanup or any other gate layer.

The command starts no services and has no Kubernetes or DRT client. Assertion image packaging,
namespace manifests, source binding and the overall gate remain work for issue #455.
