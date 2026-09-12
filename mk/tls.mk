# TLS certificate management for Ray cluster
# Include in main Makefile with: include mk/tls.mk

.PHONY: k8s-gen-tls-certs k8s-create-tls-secret

# Generate TLS certificates for Ray
k8s-gen-tls-certs:
	@echo "Generating TLS certificates for Ray..."
	bash scripts/generate-ray-tls-certs.sh
	@echo ""
	@echo "Certificates generated in ./certs/ray-tls/"

# Create TLS secret in Kubernetes
k8s-create-tls-secret: k8s-evaluation-warning k8s-require-local-context
	@echo "Creating ray-tls-certs secret..."
	kubectl --context "$(K8S_CONTEXT)" apply -f k8s/base/namespace.yaml
	@kubectl --context "$(K8S_CONTEXT)" get secret/ray-tls-certs -n django-ray || kubectl --context "$(K8S_CONTEXT)" create secret generic ray-tls-certs \
		--namespace=django-ray \
		--from-file=ca.crt=./certs/ray-tls/ca.crt \
		--from-file=tls.crt=./certs/ray-tls/tls.crt \
		--from-file=tls.key=./certs/ray-tls/tls.key
	@echo "TLS secret is present; existing certificate material was preserved."

