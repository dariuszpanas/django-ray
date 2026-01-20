# TLS Configuration

Ray supports TLS for encrypted communication between nodes. This is **required** for production deployments.

## Overview

TLS encrypts communication between:
- Ray head and Ray workers
- Django-Ray worker and Ray cluster

## Quick Start

### 1. Generate Certificates

```bash
./scripts/generate-ray-tls-certs.sh
```

This creates self-signed certificates in `./certs/ray-tls/`:
- `ca.crt` - Certificate Authority
- `tls.crt` - Server certificate
- `tls.key` - Private key

### 2. Create Kubernetes Secret

```bash
kubectl create namespace django-ray --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic ray-tls-certs \
  --namespace=django-ray \
  --from-file=ca.crt=./certs/ray-tls/ca.crt \
  --from-file=tls.crt=./certs/ray-tls/tls.crt \
  --from-file=tls.key=./certs/ray-tls/tls.key
```

### 3. Deploy with TLS

```bash
kubectl apply -k k8s/overlays/dev-tls
```

## How It Works

### Environment Variables

TLS is configured via environment variables:

| Variable | Value | Description |
|----------|-------|-------------|
| `RAY_USE_TLS` | `1` | Enable TLS |
| `RAY_TLS_SERVER_CERT` | `/etc/ray/tls/tls.crt` | Server certificate |
| `RAY_TLS_SERVER_KEY` | `/etc/ray/tls/tls.key` | Private key |
| `RAY_TLS_CA_CERT` | `/etc/ray/tls/ca.crt` | CA certificate |

### Certificate Mounting

Certificates are mounted as Kubernetes secrets:

```yaml
volumeMounts:
  - name: ray-tls
    mountPath: /etc/ray/tls
    readOnly: true

volumes:
  - name: ray-tls
    secret:
      secretName: ray-tls-certs
```

This approach:
- ✅ Enables certificate rotation without rebuilding images
- ✅ Keeps secrets secure in Kubernetes
- ✅ Allows different certificates per environment

## Certificate Requirements

### Subject Alternative Names (SANs)

Certificates must include these SANs:

```
DNS.1 = ray-head
DNS.2 = ray-head.django-ray
DNS.3 = ray-head.django-ray.svc
DNS.4 = ray-head.django-ray.svc.cluster.local
DNS.5 = localhost
IP.1 = 127.0.0.1
```

The provided script automatically includes these.

### Key Size

Recommended: RSA 4096-bit

```bash
openssl genrsa -out tls.key 4096
```

## Production: Using cert-manager

For production, use [cert-manager](https://cert-manager.io/) to automatically manage certificates.

### 1. Install cert-manager

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.0/cert-manager.yaml
```

### 2. Create ClusterIssuer

```yaml
# cluster-issuer.yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-issuer
spec:
  selfSigned: {}
```

### 3. Create Certificate

```yaml
# ray-certificate.yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ray-tls
  namespace: django-ray
spec:
  secretName: ray-tls-certs
  duration: 8760h  # 1 year
  renewBefore: 720h  # 30 days
  subject:
    organizations:
      - django-ray
  isCA: false
  privateKey:
    algorithm: RSA
    size: 4096
  usages:
    - server auth
    - client auth
  dnsNames:
    - ray-head
    - ray-head.django-ray
    - ray-head.django-ray.svc
    - ray-head.django-ray.svc.cluster.local
    - localhost
  ipAddresses:
    - 127.0.0.1
  issuerRef:
    name: selfsigned-issuer
    kind: ClusterIssuer
```

Apply:

```bash
kubectl apply -f cluster-issuer.yaml
kubectl apply -f ray-certificate.yaml
```

cert-manager will automatically create and renew the `ray-tls-certs` secret.

## Verifying TLS

### Check Certificate

```bash
kubectl get secret ray-tls-certs -n django-ray -o jsonpath='{.data.tls\.crt}' | \
  base64 -d | openssl x509 -text -noout
```

### Check Ray Connection

```bash
kubectl exec -n django-ray deployment/ray-head -- \
  ray status
```

### Check TLS Environment

```bash
kubectl exec -n django-ray deployment/ray-head -- \
  env | grep RAY_TLS
```

## Troubleshooting

### Certificate Errors

```
SSL: CERTIFICATE_VERIFY_FAILED
```

**Cause**: CA certificate not trusted or SANs missing.

**Solution**: Ensure all nodes have the CA certificate and SANs match hostnames.

### Connection Refused

```
Connection refused to ray-head:10001
```

**Cause**: TLS mismatch - one side has TLS enabled, other doesn't.

**Solution**: Ensure `RAY_USE_TLS=1` is set on all components.

### Certificate Expired

**Solution**: Regenerate certificates or use cert-manager for auto-renewal.

## Local Development

For local development without TLS:

```bash
# Use dev overlay (no TLS)
kubectl apply -k k8s/overlays/dev
```

Or run workers locally:

```bash
# Local Ray - no TLS needed
python manage.py django_ray_worker --local
```

## See Also

- [Kubernetes Deployment](kubernetes.md) - Full deployment guide
- [Ray TLS Documentation](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/tls.html)

