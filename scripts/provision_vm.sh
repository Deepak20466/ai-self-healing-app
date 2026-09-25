#!/usr/bin/env bash
# Idempotent Ubuntu 22.04/24.04 VM provisioning (SPEC.md CLOUD DEPLOYMENT).
# Run once over SSH as a sudo-capable user:
#
#   scp -r . user@host:/tmp/selfheal-src
#   ssh user@host 'cd /tmp/selfheal-src && sudo bash scripts/provision_vm.sh [domain]'
#
# `domain` is optional: pass a real DNS name for auto-HTTPS, or omit it for
# an IP-only HTTP deployment. No Docker, no Kubernetes — everything here is
# native packages + systemd, per SPEC.md HARD CONSTRAINTS #2.
#
# Safe to re-run: every step below checks current state before changing
# anything (package installs are already idempotent via apt; directory/user
# creation is guarded with `id`/`[ -d ]` checks; systemd unit installs
# always `daemon-reload` + `enable` which are themselves idempotent).

set -euo pipefail

DOMAIN="${1:-}"
DEPLOY_USER="deploy"
BASE_DIR="/opt/selfheal"
REPO_SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (sudo bash scripts/provision_vm.sh [domain])." >&2
  exit 1
fi

echo "=== 1. Base packages: Python 3.11+, PostgreSQL, git, Caddy ==="
apt-get update -y
apt-get install -y --no-install-recommends \
  software-properties-common ca-certificates curl gnupg git ufw unattended-upgrades

if ! command -v python3.11 >/dev/null 2>&1; then
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
fi
apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-dev

if ! command -v psql >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends postgresql postgresql-contrib
fi

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "=== 2. Tune PostgreSQL for a low-RAM VM ==="
PG_CONF="$(sudo -u postgres psql -tAc 'show config_file;')"
if [ -f "$PG_CONF" ]; then
  sed -i \
    -e "s/^#\?shared_buffers.*/shared_buffers = 64MB/" \
    -e "s/^#\?max_connections.*/max_connections = 30/" \
    -e "s/^#\?work_mem.*/work_mem = 4MB/" \
    "$PG_CONF"
  systemctl restart postgresql
fi

echo "=== 3. Claude Code CLI (free-mode backend) ==="
if ! command -v claude >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    npm install -g @anthropic-ai/claude-code
  else
    curl -fsSL https://claude.ai/install.sh | bash || true
  fi
fi
echo "NOTE: after provisioning, log in once as the deploy user:"
echo "  sudo -u ${DEPLOY_USER} -H claude"
echo "(one-time interactive login for the free-mode subscription backend;"
echo " skip entirely if you'll run USE_CLAUDE_CODE=false / API mode.)"

echo "=== 4. deploy user + /opt/selfheal layout ==="
if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "${DEPLOY_USER}"
fi
mkdir -p "${BASE_DIR}/releases" "${BASE_DIR}/shared"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${BASE_DIR}"

echo "=== 5. Postgres role + database (scram-sha-256, no pg_hba.conf weakening) ==="
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='selfheal'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE ROLE selfheal LOGIN PASSWORD 'CHANGE_ME' CREATEDB;"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='selfheal'" | grep -q 1 || \
  sudo -u postgres createdb -O selfheal selfheal
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='selfheal_test'" | grep -q 1 || \
  sudo -u postgres createdb -O selfheal selfheal_test
echo "NOTE: the CREATE ROLE above used a placeholder password ('CHANGE_ME')."
echo "Set a real one now: sudo -u postgres psql -c \"ALTER ROLE selfheal PASSWORD '...';\""
echo "and put the matching DATABASE_URL in ${BASE_DIR}/shared/.env"

echo "=== 6. Install systemd units for all 4 pods ==="
cp "${REPO_SRC_DIR}/deploy/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload
for unit in selfheal-app selfheal-sentinel selfheal-mcp selfheal-healer; do
  systemctl enable "${unit}.service"
done
echo "(units enabled, not yet started — start them after the first real"
echo " release lands in ${BASE_DIR}/current via deploy.yml / local_deploy.py)"

echo "=== 7. Caddyfile (auto-HTTPS for a domain, IP-only HTTP otherwise) ==="
mkdir -p /etc/caddy
if [ -n "${DOMAIN}" ]; then
  sed "s/example\.com/${DOMAIN}/" "${REPO_SRC_DIR}/deploy/Caddyfile" > /etc/caddy/Caddyfile
else
  # No domain: rewrite the site address to a bare ":80" (Caddy skips TLS
  # issuance for a non-FQDN address automatically).
  sed "s/^example\.com {/:80 {/" "${REPO_SRC_DIR}/deploy/Caddyfile" > /etc/caddy/Caddyfile
fi
systemctl enable caddy
systemctl restart caddy

echo "=== 8. Firewall: only 22, 80, 443 ==="
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "=== 9. Unattended security upgrades ==="
dpkg-reconfigure -f noninteractive unattended-upgrades || true
systemctl enable unattended-upgrades
systemctl start unattended-upgrades

echo "=== Done. Next steps ==="
echo "1. Set a real DB password (step 5) and write ${BASE_DIR}/shared/.env"
echo "   (copy .env.example, fill in real secrets — never commit it)."
echo "2. Push a release via .github/workflows/deploy.yml (on merge to main),"
echo "   or run scripts/local_deploy.py-equivalent steps by hand for a first"
echo "   manual release, then: systemctl start selfheal-app selfheal-sentinel"
echo "   selfheal-mcp selfheal-healer"
echo "3. If using free mode: sudo -u ${DEPLOY_USER} -H claude   (one-time login)"
