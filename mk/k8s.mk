# Kubernetes deployment commands
# Include in main Makefile with: include mk/k8s.mk

.PHONY: k8s-build k8s-deploy k8s-deploy-local k8s-deploy-tls k8s-delete k8s-status k8s-reset
.PHONY: k8s-install-kuberay k8s-uninstall-kuberay k8s-kind-load k8s-deploy-kuberay-kind k8s-delete-kuberay-kind
.PHONY: k8s-install-kong-local k8s-deploy-kong-local

KIND_CLUSTER_NAME ?= kind

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
	@echo "  Django Web:     http://localhost:30080"
	@echo "  Ray Dashboard:  http://localhost:30265"

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

# Deploy using KubeRay operator on kind
k8s-deploy-kuberay-kind: k8s-build k8s-kind-load k8s-install-kuberay
	kubectl apply -k k8s/overlays/kuberay-kind
	@echo "Waiting for deployments and Ray pods..."
	kubectl wait --for=condition=available deployment/postgres -n django-ray --timeout=120s || true
	kubectl wait --for=condition=available deployment/django-web -n django-ray --timeout=180s || true
	kubectl wait --for=condition=available deployment/django-ray-worker -n django-ray --timeout=180s || true
	kubectl wait --for=condition=Ready pod -l app=ray,component=head -n django-ray --timeout=240s || true
	kubectl wait --for=condition=Ready pod -l app=ray,component=worker -n django-ray --timeout=240s || true
	@echo ""
	@echo "KubeRay deployment complete!"
	@echo "  Django Web (NodePort path):    http://localhost:30080"
	@echo "  Ray Dashboard (NodePort path): http://localhost:30265"
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

# Deploy KubeRay plus Kong host-based local routes
k8s-deploy-kong-local: k8s-deploy-kuberay-kind k8s-install-kong-local
	kubectl apply -k k8s/overlays/kong-local
	kubectl delete pod -l app=ray,component=head -n django-ray --ignore-not-found
	kubectl wait --for=condition=Ready pod -l app=ray,component=head -n django-ray --timeout=240s
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
	@echo "  Django Web:     http://localhost:30080"
	@echo "  Grafana:        http://grafana.localhost:30080"
	@echo "  Prometheus:     http://prometheus.localhost:30080"
	@echo "  Ray Dashboard:  http://ray.localhost:30080"

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
	-kubectl get rayclusters -n django-ray
	@echo ""
	@echo "=== Deployments ==="
	kubectl get deployments -n django-ray

# Complete reset - delete namespace and redeploy
k8s-reset:
	@echo "Deleting namespace django-ray..."
	kubectl delete namespace django-ray --ignore-not-found --wait=true
	@echo "Redeploying..."
	$(MAKE) k8s-deploy

# View logs
k8s-logs:
	kubectl logs -n django-ray -l app=django-ray --tail=50 -f

k8s-logs-web:
	kubectl logs -n django-ray -l app=django-ray,component=web --tail=50 -f

k8s-logs-worker:
	kubectl logs -n django-ray -l app=django-ray,component=worker --tail=50 -f

k8s-logs-ray:
	kubectl logs -n django-ray -l app=ray --tail=50 -f

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

