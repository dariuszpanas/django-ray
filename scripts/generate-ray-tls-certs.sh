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

set -euo pipefail

# Ensure private keys are created with owner-only permissions (0600) from creation time.
umask 077

CERT_DIR="${CERT_DIR:-./certs/ray-tls}"
DAYS_VALID="${DAYS_VALID:-365}"
KEY_SIZE="${KEY_SIZE:-4096}"

# Create output directory with restrictive permissions
mkdir -p "$CERT_DIR"

cleanup() {
    rm -f "$CERT_DIR/ca.conf" "$CERT_DIR/server.csr" "$CERT_DIR/server.conf" "$CERT_DIR/ca.srl"
}
trap cleanup EXIT

echo "Generating Ray TLS certificates in $CERT_DIR..."

# Generate CA private key
echo "1. Generating CA private key..."
openssl genrsa -out "$CERT_DIR/ca.key" "$KEY_SIZE"

# Generate CA certificate with explicit X.509v3 CA constraints
echo "2. Generating CA certificate..."
cat > "$CERT_DIR/ca.conf" << EOF
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_ca
prompt = no

[req_distinguished_name]
CN = ray-cluster-ca
O = django-ray

[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

openssl req -x509 -new -nodes \
    -key "$CERT_DIR/ca.key" \
    -sha256 \
    -days "$DAYS_VALID" \
    -out "$CERT_DIR/ca.crt" \
    -config "$CERT_DIR/ca.conf"

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
CN = ray-head-svc

[v3_req]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = ray-head
DNS.2 = ray-head.django-ray
DNS.3 = ray-head.django-ray.svc
DNS.4 = ray-head.django-ray.svc.cluster.local
DNS.5 = *.ray-head.django-ray.svc.cluster.local
DNS.6 = ray-head-svc
DNS.7 = ray-head-svc.django-ray
DNS.8 = ray-head-svc.django-ray.svc
DNS.9 = ray-head-svc.django-ray.svc.cluster.local
DNS.10 = localhost
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

# Confirm restrictive permissions on keys and public certs
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

# Generate Kubernetes secret command without printing private key material
echo "To create the Kubernetes secret directly from files (never commit private keys):"
echo ""
echo "kubectl create secret generic ray-tls-certs \\"
echo "  --namespace=django-ray \\"
echo "  --from-file=ca.crt=$CERT_DIR/ca.crt \\"
echo "  --from-file=tls.crt=$CERT_DIR/tls.crt \\"
echo "  --from-file=tls.key=$CERT_DIR/tls.key"
echo ""
