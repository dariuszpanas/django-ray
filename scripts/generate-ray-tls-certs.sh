#!/bin/bash
# Generate self-signed TLS certificates for Ray cluster communication
#
# Usage:
#   ./scripts/generate-ray-tls-certs.sh
#
# This generates:
#   - CA certificate (ca.crt, ca.key)
#   - Server certificate signed by CA (tls.crt, tls.key)
#
# For production, use cert-manager or your organization's PKI instead.

set -e

CERT_DIR="${CERT_DIR:-./certs/ray-tls}"
DAYS_VALID="${DAYS_VALID:-365}"
KEY_SIZE="${KEY_SIZE:-4096}"

# Create output directory
mkdir -p "$CERT_DIR"

echo "Generating Ray TLS certificates in $CERT_DIR..."

# Generate CA private key
echo "1. Generating CA private key..."
openssl genrsa -out "$CERT_DIR/ca.key" "$KEY_SIZE"

# Generate CA certificate
echo "2. Generating CA certificate..."
openssl req -x509 -new -nodes \
    -key "$CERT_DIR/ca.key" \
    -sha256 \
    -days "$DAYS_VALID" \
    -out "$CERT_DIR/ca.crt" \
    -subj "/CN=ray-cluster-ca/O=django-ray"

# Generate server private key
echo "3. Generating server private key..."
openssl genrsa -out "$CERT_DIR/tls.key" "$KEY_SIZE"

# Create server certificate signing request (CSR)
echo "4. Creating server CSR..."
cat > "$CERT_DIR/server.conf" << EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = ray-head

[v3_req]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = ray-head
DNS.2 = ray-head.django-ray
DNS.3 = ray-head.django-ray.svc
DNS.4 = ray-head.django-ray.svc.cluster.local
DNS.5 = *.ray-head.django-ray.svc.cluster.local
DNS.6 = localhost
IP.1 = 127.0.0.1
EOF

openssl req -new \
    -key "$CERT_DIR/tls.key" \
    -out "$CERT_DIR/server.csr" \
    -config "$CERT_DIR/server.conf"

# Sign server certificate with CA
echo "5. Signing server certificate..."
openssl x509 -req \
    -in "$CERT_DIR/server.csr" \
    -CA "$CERT_DIR/ca.crt" \
    -CAkey "$CERT_DIR/ca.key" \
    -CAcreateserial \
    -out "$CERT_DIR/tls.crt" \
    -days "$DAYS_VALID" \
    -sha256 \
    -extensions v3_req \
    -extfile "$CERT_DIR/server.conf"

# Clean up intermediate files
rm -f "$CERT_DIR/server.csr" "$CERT_DIR/server.conf" "$CERT_DIR/ca.srl"

# Set restrictive permissions
chmod 600 "$CERT_DIR"/*.key
chmod 644 "$CERT_DIR"/*.crt

echo ""
echo "Certificate generation complete!"
echo ""
echo "Files created:"
ls -la "$CERT_DIR"
echo ""

# Verify certificates
echo "Verifying certificate chain..."
openssl verify -CAfile "$CERT_DIR/ca.crt" "$CERT_DIR/tls.crt"
echo ""

# Print certificate info
echo "Server certificate info:"
openssl x509 -in "$CERT_DIR/tls.crt" -noout -subject -issuer -dates
echo ""

# Generate Kubernetes secret command
echo "To create the Kubernetes secret:"
echo ""
echo "kubectl create secret generic ray-tls-certs \\"
echo "  --namespace=django-ray \\"
echo "  --from-file=ca.crt=$CERT_DIR/ca.crt \\"
echo "  --from-file=tls.crt=$CERT_DIR/tls.crt \\"
echo "  --from-file=tls.key=$CERT_DIR/tls.key"
echo ""

# Alternatively, print base64-encoded values for manual secret creation
echo "Or update k8s/base/ray-tls-secret.yaml with these values:"
echo ""
echo "data:"
echo "  ca.crt: $(base64 -w0 "$CERT_DIR/ca.crt")"
echo "  tls.crt: $(base64 -w0 "$CERT_DIR/tls.crt")"
echo "  tls.key: $(base64 -w0 "$CERT_DIR/tls.key")"

