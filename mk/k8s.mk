# Kubernetes deployment commands
# Include in main Makefile with: include mk/k8s.mk

.PHONY: k8s-build k8s-deploy k8s-deploy-local k8s-deploy-tls k8s-delete k8s-status k8s-reset

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

