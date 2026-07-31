# Kubernetes deployment commands
# Include in main Makefile with: include mk/k8s.mk

.PHONY: k8s-build k8s-deploy k8s-deploy-local k8s-deploy-tls k8s-delete k8s-status k8s-reset k8s-urls k8s-urls-kong
.PHONY: k8s-check-prometheus-targets k8s-final-gate-preflight k8s-final-gate
.PHONY: k8s-install-kuberay k8s-uninstall-kuberay k8s-kind-load k8s-prepare-kuberay-kind k8s-delete-local-raycluster k8s-deploy-kuberay-kind k8s-delete-kuberay-kind
.PHONY: k8s-install-kong-local k8s-uninstall-kong-local k8s-deploy-kong-local
.PHONY: k8s-logs k8s-logs-web k8s-logs-worker k8s-logs-ray k8s-logs-ray-head k8s-logs-ray-workers

KIND_CLUSTER_NAME ?= kind
K8S_URL_SCHEME ?= http
K8S_URL_HOST ?= localhost
K8S_WEB_PORT ?= 30080
K8S_RAY_DASHBOARD_PORT ?= 30265
K8S_GRAFANA_PORT ?= 30030
K8S_PROMETHEUS_PORT ?= 30090
K8S_WEB_URL ?= $(K8S_URL_SCHEME)://$(K8S_URL_HOST):$(K8S_WEB_PORT)
K8S_API_DOCS_URL ?= $(K8S_WEB_URL)/api/docs
K8S_ADMIN_URL ?= $(K8S_WEB_URL)/admin/
K8S_RAY_DASHBOARD_URL ?= $(K8S_URL_SCHEME)://$(K8S_URL_HOST):$(K8S_RAY_DASHBOARD_PORT)
K8S_GRAFANA_URL ?= $(K8S_URL_SCHEME)://$(K8S_URL_HOST):$(K8S_GRAFANA_PORT)
K8S_PROMETHEUS_URL ?= $(K8S_URL_SCHEME)://$(K8S_URL_HOST):$(K8S_PROMETHEUS_PORT)
K8S_KONG_PORT ?= 30080
K8S_KONG_WEB_HOST ?= localhost
K8S_KONG_GRAFANA_HOST ?= grafana.localhost
K8S_KONG_PROMETHEUS_HOST ?= prometheus.localhost
K8S_KONG_RAY_HOST ?= ray.localhost
K8S_KONG_WEB_URL ?= $(K8S_URL_SCHEME)://$(K8S_KONG_WEB_HOST):$(K8S_KONG_PORT)
K8S_KONG_API_DOCS_URL ?= $(K8S_KONG_WEB_URL)/api/docs
K8S_KONG_ADMIN_URL ?= $(K8S_KONG_WEB_URL)/admin/
K8S_KONG_GRAFANA_URL ?= $(K8S_URL_SCHEME)://$(K8S_KONG_GRAFANA_HOST):$(K8S_KONG_PORT)
K8S_KONG_PROMETHEUS_URL ?= $(K8S_URL_SCHEME)://$(K8S_KONG_PROMETHEUS_HOST):$(K8S_KONG_PORT)
K8S_KONG_RAY_DASHBOARD_URL ?= $(K8S_URL_SCHEME)://$(K8S_KONG_RAY_HOST):$(K8S_KONG_PORT)
K8S_PROMETHEUS_TARGET_TIMEOUT ?= 120
K8S_CONTEXT ?=
K8S_NAMESPACE ?= django-ray
K8S_RAY_RESTART ?=
K8S_FINAL_GATE_EXTRA_ARGS ?=

# Build Docker images for Kubernetes
k8s-build:
	@echo "Building Django web image..."
	docker build -t django-ray:latest .
	@echo "Building Ray worker image (with django-ray installed)..."
	docker build -f Dockerfile.ray -t django-ray-worker:latest .

# Deploy to Kubernetes cluster (dev overlay)
k8s-deploy: k8s-build
	kubectl apply -k k8s/overlays/dev
	@echo "Waiting for deployments..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	@echo ""
	@echo "Deployment complete!"
	@$(MAKE) --no-print-directory k8s-urls

# Deploy with full resources (16+ CPUs, 32GB+ RAM)
k8s-deploy-local: k8s-build
	kubectl apply -k k8s/overlays/local
	@echo "Waiting for deployments..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	@echo ""
	@echo "Deployment complete!"
	@$(MAKE) --no-print-directory k8s-urls

# Deploy with TLS enabled
k8s-deploy-tls: k8s-build k8s-create-tls-secret
	kubectl apply -k k8s/overlays/dev-tls
	@echo "Waiting for deployments..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/ray-head -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	@echo ""
	@echo "TLS-enabled deployment complete!"
	@$(MAKE) --no-print-directory k8s-urls

# Install/upgrade KubeRay operator (required for RayCluster CRD mode)
k8s-install-kuberay:
	helm repo add kuberay https://ray-project.github.io/kuberay-helm/ || true
	helm repo update
	helm upgrade --install kuberay-operator kuberay/kuberay-operator \
		--namespace kuberay-system \
		--create-namespace
	kubectl wait --for=condition=available deployment -l app.kubernetes.io/name=kuberay-operator -n kuberay-system --timeout=180s

# Uninstall KubeRay operator
k8s-uninstall-kuberay:
	helm uninstall kuberay-operator -n kuberay-system || true

# Load locally built images into kind cluster
k8s-kind-load:
	@echo "Attempting to load images into kind cluster: $(KIND_CLUSTER_NAME)"
	-@kind load docker-image django-ray:latest --name $(KIND_CLUSTER_NAME)
	-@kind load docker-image django-ray-worker:latest --name $(KIND_CLUSTER_NAME)
	@echo "If 'kind' is unavailable, these load steps can be ignored (Docker Desktop uses local images directly)."

# Shared non-applying prerequisites for both local KubeRay capacity profiles.
k8s-prepare-kuberay-kind: k8s-build k8s-kind-load k8s-install-kuberay

# Replace the package-owned local RayCluster instead of trusting an in-place
# profile edit to recreate worker pods or the generated head Service.
k8s-delete-local-raycluster:
	kubectl delete raycluster/ray -n django-ray --ignore-not-found --cascade=foreground --wait=true --timeout=240s
	kubectl delete service/ray-head-svc -n django-ray --ignore-not-found --wait=true

# Deploy using KubeRay operator on kind
k8s-deploy-kuberay-kind: k8s-prepare-kuberay-kind
	$(MAKE) --no-print-directory k8s-uninstall-kong-local
	$(MAKE) --no-print-directory k8s-delete-local-raycluster
	kubectl apply -k k8s/overlays/kuberay-kind
	@echo "Waiting for deployments and Ray pods..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=create service/ray-head-svc -n django-ray --timeout=240s
	kubectl wait --for=create pod -l app=ray,component=head -n django-ray --timeout=240s
	kubectl wait --for=condition=Ready pod -l app=ray,component=head -n django-ray --timeout=240s
	kubectl wait --for=create pod -l app=ray,component=worker -n django-ray --timeout=240s
	kubectl wait --for=jsonpath='{.status.desiredWorkerReplicas}'=2 raycluster/ray -n django-ray --timeout=240s
	kubectl wait --for=jsonpath='{.status.readyWorkerReplicas}'=2 raycluster/ray -n django-ray --timeout=240s
	kubectl wait --for=jsonpath='{.status.availableWorkerReplicas}'=2 raycluster/ray -n django-ray --timeout=240s
	kubectl wait --for=condition=Ready pod -l app=ray,component=worker -n django-ray --timeout=240s
	@echo ""
	@echo "KubeRay deployment complete!"
	@$(MAKE) --no-print-directory k8s-urls
	@echo "  For Kong subdomain routing on Docker Desktop managed kind:"
	@echo "    make k8s-deploy-kong-local"

# Install Kong Gateway + Kong Ingress Controller for the local overlay
k8s-install-kong-local:
	helm repo add kong https://charts.konghq.com/ || true
	helm repo update
	helm upgrade --install kong kong/ingress \
		--namespace kong \
		--create-namespace \
		-f k8s/overlays/kong-local/kong-values.yaml
	kubectl rollout status deployment/kong-controller -n kong --timeout=180s
	kubectl rollout status deployment/kong-gateway -n kong --timeout=180s

# Remove only the package-owned local Kong release and its application routes.
k8s-uninstall-kong-local:
	helm uninstall kong --namespace kong --ignore-not-found --wait --timeout 180s
	kubectl delete ingress/grafana-ingress ingress/prometheus-ingress ingress/ray-dashboard-ingress -n django-ray --ignore-not-found --wait=true

# Deploy KubeRay plus Kong host-based local routes
k8s-deploy-kong-local: k8s-prepare-kuberay-kind k8s-install-kong-local
	$(MAKE) --no-print-directory k8s-delete-local-raycluster
	kubectl apply -k k8s/overlays/kong-local
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s
	kubectl wait --for=create service/ray-head-svc -n django-ray --timeout=240s
	kubectl wait --for=create pod -l app=ray,component=head -n django-ray --timeout=240s
	kubectl wait --for=condition=Ready pod -l app=ray,component=head -n django-ray --timeout=240s
	kubectl wait --for=create pod -l app=ray,component=worker -n django-ray --timeout=240s
	kubectl wait --for=jsonpath='{.status.desiredWorkerReplicas}'=4 raycluster/ray -n django-ray --timeout=240s
	kubectl wait --for=jsonpath='{.status.readyWorkerReplicas}'=4 raycluster/ray -n django-ray --timeout=240s
	kubectl wait --for=jsonpath='{.status.availableWorkerReplicas}'=4 raycluster/ray -n django-ray --timeout=240s
	kubectl wait --for=condition=Ready pod -l app=ray,component=worker -n django-ray --timeout=240s
	kubectl rollout restart deployment/django-web -n django-ray
	kubectl rollout restart deployment/django-ray-worker -n django-ray
	-kubectl rollout restart deployment/django-ray-worker-sync -n django-ray
	-kubectl rollout restart deployment/django-ray-worker-ml -n django-ray
	kubectl rollout status deployment/django-web -n django-ray --timeout=180s
	kubectl rollout status deployment/django-ray-worker -n django-ray --timeout=180s
	-kubectl rollout status deployment/django-ray-worker-sync -n django-ray --timeout=180s
	-kubectl rollout status deployment/django-ray-worker-ml -n django-ray --timeout=180s
	@echo ""
	@echo "Kong local deployment complete!"
	@$(MAKE) --no-print-directory k8s-urls-kong

# Delete KubeRay operator-based overlay resources
k8s-delete-kuberay-kind:
	kubectl delete -k k8s/overlays/kuberay-kind --ignore-not-found

# Delete deployment
k8s-delete:
	kubectl delete -k k8s/overlays/dev --ignore-not-found

# Show deployment status
k8s-status:
	@echo "=== Pods ==="
	kubectl get pods -n django-ray
	@echo ""
	@echo "=== Services ==="
	kubectl get svc -n django-ray
	@echo ""
	@echo "=== RayClusters ==="
	@kubectl get crd rayclusters.ray.io >NUL 2>&1 && kubectl get rayclusters -n django-ray || echo "RayCluster CRD not installed (static Ray deployment path)"
	@echo ""
	@echo "=== Deployments ==="
	kubectl get deployments -n django-ray

# Verify the bundled Ray and authenticated django-ray Prometheus scrape targets.
k8s-check-prometheus-targets:
	python scripts/check_prometheus_targets.py \
		--url "$(K8S_PROMETHEUS_URL)" \
		--timeout "$(K8S_PROMETHEUS_TARGET_TIMEOUT)"

# Non-mutating context, clean-tree, Kustomize, and client-side apply checks.
k8s-final-gate-preflight:
	$(if $(strip $(K8S_CONTEXT)),,$(error K8S_CONTEXT is required (docker-desktop or kind-<name>)))
	$(if $(strip $(K8S_RAY_RESTART)),,$(error K8S_RAY_RESTART is required (required or skip)))
	python -m scripts.local_kuberay_gate \
		--context "$(K8S_CONTEXT)" \
		--namespace "$(K8S_NAMESPACE)" \
		--ray-restart "$(K8S_RAY_RESTART)" \
		--web-url "$(K8S_WEB_URL)" \
		--prometheus-url "$(K8S_PROMETHEUS_URL)" \
		--preflight-only $(K8S_FINAL_GATE_EXTRA_ARGS)

# Complete guarded Docker Desktop/Kind KubeRay final integration gate.
k8s-final-gate:
	$(if $(strip $(K8S_CONTEXT)),,$(error K8S_CONTEXT is required (docker-desktop or kind-<name>)))
	$(if $(strip $(K8S_RAY_RESTART)),,$(error K8S_RAY_RESTART is required (required or skip)))
	python -m scripts.local_kuberay_gate \
		--context "$(K8S_CONTEXT)" \
		--namespace "$(K8S_NAMESPACE)" \
		--ray-restart "$(K8S_RAY_RESTART)" \
		--web-url "$(K8S_WEB_URL)" \
		--prometheus-url "$(K8S_PROMETHEUS_URL)" $(K8S_FINAL_GATE_EXTRA_ARGS)

# Print local service URLs. Override K8S_URL_HOST, K8S_URL_SCHEME, or ports for non-local clusters.
k8s-urls:
	@echo === Project URLs ===
	@echo Django Web:       $(K8S_WEB_URL)
	@echo API Docs:         $(K8S_API_DOCS_URL)
	@echo Django Admin:     $(K8S_ADMIN_URL)
	@echo Ray Dashboard:    $(K8S_RAY_DASHBOARD_URL)
	@echo Grafana:          $(K8S_GRAFANA_URL)
	@echo Prometheus:       $(K8S_PROMETHEUS_URL)
	@echo.
	@echo Override examples:
	@echo   make k8s-urls K8S_URL_HOST=my-load-balancer.example.com K8S_WEB_PORT=80 K8S_GRAFANA_PORT=3000 K8S_PROMETHEUS_PORT=9090
	@echo   make k8s-urls K8S_WEB_URL=https://app.example.com K8S_RAY_DASHBOARD_URL=https://ray.example.com K8S_GRAFANA_URL=https://grafana.example.com K8S_PROMETHEUS_URL=https://prometheus.example.com

# Print Kong host-based local URLs. Override K8S_KONG_* variables for custom ingress hosts.
k8s-urls-kong:
	@echo === Project URLs (Kong) ===
	@echo Django Web:       $(K8S_KONG_WEB_URL)
	@echo API Docs:         $(K8S_KONG_API_DOCS_URL)
	@echo Django Admin:     $(K8S_KONG_ADMIN_URL)
	@echo Grafana:          $(K8S_KONG_GRAFANA_URL)
	@echo Prometheus:       $(K8S_KONG_PROMETHEUS_URL)
	@echo Ray Dashboard:    $(K8S_KONG_RAY_DASHBOARD_URL)
	@echo.
	@echo Override examples:
	@echo   make k8s-urls-kong K8S_KONG_WEB_HOST=app.example.com K8S_KONG_GRAFANA_HOST=grafana.example.com K8S_KONG_PROMETHEUS_HOST=prometheus.example.com K8S_KONG_RAY_HOST=ray.example.com K8S_KONG_PORT=443 K8S_URL_SCHEME=https
	@echo   make k8s-urls-kong K8S_KONG_WEB_URL=https://app.example.com K8S_KONG_RAY_DASHBOARD_URL=https://ray.example.com K8S_KONG_GRAFANA_URL=https://grafana.example.com K8S_KONG_PROMETHEUS_URL=https://prometheus.example.com

# Complete reset - delete namespace and redeploy
k8s-reset:
	@echo "Deleting namespace django-ray..."
	kubectl delete namespace django-ray --ignore-not-found --wait=true
	@echo "Redeploying..."
	$(MAKE) k8s-deploy

# View Django application logs
k8s-logs:
	kubectl logs -n django-ray -l app=django-ray --all-containers=true --prefix --tail=50 -f --max-log-requests=16

k8s-logs-web:
	kubectl logs -n django-ray -l app=django-ray,component=web -c django-web --prefix --tail=50 -f

# Django task managers claim durable rows and submit work to Ray.
k8s-logs-worker:
	kubectl logs -n django-ray -l app=django-ray,component=worker -c django-ray-worker --prefix --tail=50 -f --max-log-requests=8

# Follow every Ray container, including the one-shot dashboard importer.
k8s-logs-ray:
	kubectl logs -n django-ray -l app=ray --all-containers=true --prefix --tail=50 -f --max-log-requests=16

k8s-logs-ray-head:
	kubectl logs -n django-ray -l app=ray,component=head -c ray-head --prefix --tail=50 -f

k8s-logs-ray-workers:
	kubectl logs -n django-ray -l app=ray,component=worker -c ray-worker --prefix --tail=50 -f --max-log-requests=8

# Restart deployments
k8s-restart:
	kubectl rollout restart deployment/django-web -n django-ray
	kubectl rollout restart deployment/django-ray-worker -n django-ray

k8s-restart-ray:
	kubectl rollout restart deployment/ray-head -n django-ray
	kubectl rollout restart deployment/ray-worker -n django-ray

# Scale Ray workers
k8s-scale-ray-2:
	kubectl scale deployment/ray-worker --replicas=2 -n django-ray

k8s-scale-ray-3:
	kubectl scale deployment/ray-worker --replicas=3 -n django-ray

k8s-scale-ray-4:
	kubectl scale deployment/ray-worker --replicas=4 -n django-ray

# Shell into pods
k8s-shell-web:
	kubectl exec -it -n django-ray deployment/django-web -- /bin/bash

k8s-shell-worker:
	kubectl exec -it -n django-ray deployment/django-ray-worker -- /bin/bash

