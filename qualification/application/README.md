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
