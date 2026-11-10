#!/usr/bin/env bash
# Build a small throwaway git repository with two release tags, v1.41.0 and
# v1.42.0, so you can try deploy-diff-summary without pointing it at real code.
#
#   bash examples/make-demo-repo.sh /tmp/deploy-demo
set -euo pipefail

dir="${1:?usage: make-demo-repo.sh <new-directory>}"
if [ -e "$dir" ]; then
  echo "error: $dir already exists; pick a new path" >&2
  exit 1
fi

mkdir -p "$dir"
cd "$dir"
git init -q -b main
git config user.name "Priya Raman"
git config user.email "priya@example.com"
commit() { git add -A; git commit -q -m "$1"; }

mkdir -p app config db/migrations deploy/k8s helm

cat > app/orders.py <<'PY'
def create_order(items):
    return {"items": items, "state": "new"}
PY
cat > config/production.env <<'ENV'
# Production settings for the orders service
DB_POOL_SIZE=10
REQUEST_TIMEOUT_SECONDS=30
PAYMENT_API_KEY=example-key-one
LEGACY_EXPORT_ENABLED=true
ENV
cat > helm/values-prod.yaml <<'YAML'
replicaCount: 3
resources:
  limits:
    memory: 512Mi
autoscaling:
  enabled: false
YAML
cat > deploy/k8s/orders.yaml <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders
spec:
  template:
    spec:
      containers:
        - name: orders
          image: ghcr.io/acme/orders:1.41.0
        - name: envoy
          image: envoyproxy/envoy:v1.30.1
YAML
cat > Dockerfile <<'DOCKER'
FROM python:3.11-slim
COPY . /srv
CMD ["python", "-m", "app"]
DOCKER
cat > requirements.txt <<'REQ'
flask==2.3.3
requests==2.31.0
sqlalchemy==1.4.52
simplejson==3.19.2
REQ
cat > db/migrations/0007_create_orders.sql <<'SQL'
CREATE TABLE orders (id BIGINT PRIMARY KEY, state TEXT NOT NULL);
SQL
commit "Release 1.41.0"
git tag v1.41.0

cat > db/migrations/0008_add_currency.sql <<'SQL'
ALTER TABLE orders ADD COLUMN currency CHAR(3) NOT NULL DEFAULT 'USD';
SQL
cat > app/orders.py <<'PY'
def create_order(items, currency="USD"):
    return {"items": items, "state": "new", "currency": currency}
PY
commit "feat: store order currency"

git config user.name "Marcus Chen"
cat > requirements.txt <<'REQ'
flask==3.0.3
requests==2.32.3
sqlalchemy==1.4.52
orjson==3.10.7
REQ
commit "Upgrade flask to 3.x and switch to orjson"

cat > config/production.env <<'ENV'
# Production settings for the orders service
DB_POOL_SIZE=50
REQUEST_TIMEOUT_SECONDS=30
PAYMENT_API_KEY=example-key-two
CURRENCY_CONVERSION_ENABLED=true
ENV
cat > helm/values-prod.yaml <<'YAML'
replicaCount: 6
resources:
  limits:
    memory: 512Mi
autoscaling:
  enabled: true
YAML
commit "Raise DB pool size and enable autoscaling for launch"

git config user.name "Priya Raman"
printf '\nVALID_STATES = ("new", "paid", "shipped")\n' >> app/orders.py
commit "hotfix: revert order state validation"

sed -i.bak 's#orders:1.41.0#orders:1.42.0#; s#envoy:v1.30.1#envoy:v1.31.0#' deploy/k8s/orders.yaml
sed -i.bak 's#python:3.11-slim#python:3.12-slim#' Dockerfile
rm -f deploy/k8s/orders.yaml.bak Dockerfile.bak
commit "Release 1.42.0"
git tag v1.42.0

echo "Demo repository created at $dir (tags: v1.41.0, v1.42.0)"
