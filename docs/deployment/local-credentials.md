# Private local evaluation credentials

The checked-in Kubernetes examples are evaluation assets. Every overlay renders without Secrets
or Ingress resources, and all application, Ray, and monitoring Services use `ClusterIP`. Prepare
credentials separately before deploying into an admitted, trusted local cluster.

## Prepare and provision

```bash
python scripts/prepare_k8s_secrets.py --namespace django-ray
make k8s-bootstrap-django-ray-secret K8S_CONTEXT=docker-desktop
```

Preparation creates `.local/k8s/django-ray/secrets.json`, which Git ignores and Docker build contexts
exclude. Source RuntimeEnv archives select only the `src` and `testproject` trees; local credentials
are not part of their upload. It generates independent
random values for the Django signing key, operator API token, metrics token, Ray token, database
password, bootstrap password, Grafana password, and separate demo token. It prints only the path, never credentials,
and refuses to overwrite an existing file. Separate namespace preparations produce separate values.
The helper rejects directory links and junctions, traversal, malformed documents, unexpected
resources or keys, and documents over 32 KiB. Provisioning sends only the validated Secret list
through stdin, suppressing Kubernetes output. A complete existing Secret set is preserved. A partial
set fails closed for explicit operator recovery; a failed first creation may itself leave a partial
set. The helper never applies over an existing Secret or retries by deleting state.

The helper requests POSIX directory mode `0700` and file mode `0600`. These modes do not establish
Windows ACL isolation: use a private user directory with reviewed local access permissions. Do not
copy the file into images, tracked files, issue comments, evidence, shell arguments, or terminal
logs. Inspect a required credential locally using an editor that does not share its contents.

The context-name guard accepts `docker-desktop` and `kind-<name>`. It does not prove the server is
local or admit a workload against shared capacity. Verify kubeconfig and resource ownership first.
The guarded final gate additionally verifies its local API endpoint. The checked-in manifests use
the `django-ray` namespace; deploying a different namespace requires a corresponding overlay.

## Optional administrator bootstrap

Without an explicit bootstrap request, setup performs migrations and artifact preparation without
creating an administrator. To opt in on a fresh local deployment, prepare once with an identity:

```bash
python scripts/prepare_k8s_secrets.py --namespace django-ray \
  --bootstrap-username chosen-admin --bootstrap-email chosen@example.invalid
```

The password is generated into the ignored file. No username, email, or password falls back to a
reusable account. `DJANGO_BOOTSTRAP_SUPERUSER=true` requires all three explicit values and a password
of at least 32 characters. The setup helper creates the account only if the username does not
already exist. Reapplying an enabled bootstrap against an existing account fails with guidance; it
does not silently keep an obsolete password or reset one.

After successful creation, explicitly set `DJANGO_BOOTSTRAP_SUPERUSER=false` in the live
`django-ray-bootstrap` Secret and retained local file before rerunning setup. Only that boolean
needs changing. To rotate an existing Django account, use Django's interactive
`manage.py changepassword <username>` through an authorized administrative session. Changing the
bootstrap Secret does not rotate an account stored in PostgreSQL. Likewise, changing initial
PostgreSQL or Grafana environment values does not rotate passwords already stored on persistent
volumes; use their supported administrative procedures and update the matching Secret values.

Older deployments require an explicit migration from the combined Secret. Preserve the existing
Django signing and database values when splitting them into component Secrets, disable bootstrap
for existing accounts, rotate previously reusable credentials through the backing services, and
then remove obsolete keys from `django-ray-secret`. Do not generate fresh database/signing values
over an existing volume. The provisioning helper deliberately refuses this partial-set migration.

## Credential delivery

| Secret | Values | Recipients |
|---|---|---|
| `django-ray-runtime` | Django signing key and application database credentials | Django web, setup, task managers, and Ray execution nodes |
| `django-ray-secret` | Operator API token | Django web only |
| `django-ray-database` | PostgreSQL initialization credentials | PostgreSQL only |
| `django-ray-auth` | Ray authentication token | Ray head/workers and Ray-connected task managers |
| `django-ray-bootstrap` | Opt-in administrator identity and password | Setup Job only |
| `django-ray-grafana` | Grafana administrator credentials | Grafana and its dashboard importer |
| `django-ray-metrics` | Metrics-only bearer token | Django web and the Prometheus scrape file |
| `django-ray-demo` | Demo workload bearer token | Web in the explicit `kuberay-kind` and inherited `kong-local` demo profiles only |

Delivery uses explicit key references; no container imports a whole Secret using `envFrom`.
Non-web Django processes set `DJANGO_API_ENABLED=false`, which disables operator/workload API
authorization even if a token accidentally reaches that process. Prometheus keeps scraping Django
through the dedicated metrics-only credential. That credential cannot access workload, execution,
or lifecycle routes. Anonymous Grafana access is disabled; the importer authenticates explicitly.

Ray nodes and authorized managers use `RAY_AUTH_MODE=token` with the same random `RAY_AUTH_TOKEN`.
This token grants cluster control and must be treated as execution authority. Ray tasks still need
Django/database runtime credentials in this evaluation topology; this split does not provide tenant
isolation or a production identity architecture. Token authentication does not encrypt transport;
the local TLS example is a separate transport option.

## Temporary loopback access

Run each required forward in its own terminal after the services exist:

```bash
make k8s-forward-web K8S_CONTEXT=docker-desktop
make k8s-forward-ray K8S_CONTEXT=docker-desktop
make k8s-forward-grafana K8S_CONTEXT=docker-desktop
make k8s-forward-prometheus K8S_CONTEXT=docker-desktop
```

Each command binds only `127.0.0.1`, lasts at most 900 seconds by default, and stops when interrupted.
`K8S_FORWARD_SECONDS` can select 1–3600 seconds. Web uses port 30080, Ray dashboard 30265, Grafana
30030, and Prometheus 30090. Forwarding does not bypass application authentication. Authenticate
Ray dashboard access with its Ray token and Grafana with its generated administrator account.
No helper forwards Ray GCS or Ray Client. No overlay installs external Ray/control-plane ingress.
The historically named `kong-local` overlay retains its larger capacity profile and uses these same
private access paths; it no longer installs Kong or ingress routes.

Before upgrading an older sample, explicitly remove its package-owned ingress routes and verify
that no old NodePort or external controller still publishes these services. Omission from Kustomize
does not prune old Ingress resources. Do not remove unrelated controller releases or routes.

The local KubeRay gate records and verifies preservation of all eight Secret data mappings, scans
protected values without printing them, and retains its PostgreSQL/PVC preservation and cleanup
contract. Its authentication layer uses fresh processes in the existing authorized manager and
Grafana importer. It requires missing-token and invalid-token denial on Ray Dashboard, Jobs, Client,
and GCS, plus successful authorized reads. Grafana must reject anonymous access and an invalid
password, then identify the generated administrator. Connection errors never count as denial.
Source tests and pure renders verify structure only. Release qualification must also
prove authenticated task execution, rejected unauthenticated Ray access, Grafana login, the Django
metrics scrape, and absence of host/LAN listeners from the exact candidate on admitted Linux.
