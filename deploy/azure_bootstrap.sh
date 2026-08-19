#!/usr/bin/env bash
#
# Provision + start the AI Job Hunter Bot on the Azure Linux VM.
#
# Runs ON the VM. Idempotent: safe to re-run after a failed attempt, after a
# reboot, or as the ordinary way to ship a new commit.
#
#   ./deploy/azure_bootstrap.sh              full run
#   SKIP_TESTS=1 ./deploy/azure_bootstrap.sh skip the suite (faster redeploys)
#
# It does NOT copy secrets. Those never touch git; deploy/sync_to_azure.sh
# pushes them from the workstation over scp before this script runs.

set -euo pipefail

APP_DIR="${APP_DIR:-/home/azureuser/AI_Job_Hunter_Bot}"
APP_USER="${APP_USER:-azureuser}"
PM2_APP="${PM2_APP:-job-hunter-cloud}"
VENV="$APP_DIR/venv"
PY="$VENV/bin/python"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
step "STEP 1a  Project directory and permissions"
[ -d "$APP_DIR" ] || die "$APP_DIR does not exist. Clone the repo there first."
[ -d "$APP_DIR/.git" ] || die "$APP_DIR is not a git checkout."
# The daemon writes state/, screenshots/ and logs under here as $APP_USER.
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/secrets" 2>/dev/null || true
ok "$APP_DIR owned by $APP_USER"

# ---------------------------------------------------------------------------
step "STEP 1b  System dependencies"

python_ok() {
  command -v python3 >/dev/null 2>&1 &&
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
}
python_ok || {
  warn "python3 >= 3.10 missing; installing"
  sudo apt-get update -qq
  sudo apt-get install -y python3 python3-venv python3-pip
}
ok "python $(python3 -V 2>&1 | awk '{print $2}')"

# python3-venv is a separate package on Debian/Ubuntu and its absence only
# shows up at `python3 -m venv` time, with a confusing message.
dpkg -s python3-venv >/dev/null 2>&1 || {
  warn "python3-venv missing; installing"
  sudo apt-get update -qq && sudo apt-get install -y python3-venv
}

node_major() { command -v node >/dev/null 2>&1 && node -v | sed 's/^v\([0-9]*\).*/\1/' || echo 0; }
if [ "$(node_major)" -lt 20 ]; then
  warn "node 20+ missing (found $(node -v 2>/dev/null || echo none)); installing from NodeSource"
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
ok "node $(node -v)"

command -v pm2 >/dev/null 2>&1 || { warn "pm2 missing; installing"; sudo npm install -g pm2; }
ok "pm2 $(pm2 -v)"

# ---------------------------------------------------------------------------
step "STEP 1c  Virtualenv + Python dependencies"
[ -x "$PY" ] || python3 -m venv "$VENV"
"$PY" -m pip install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
ok "requirements.txt installed"

# ---------------------------------------------------------------------------
step "STEP 1d  Chromium + its OS libraries"
# `playwright install-deps` resolves the correct package set for THIS distro
# release, which matters: the library was renamed libasound2 -> libasound2t64
# in Ubuntu 24.04, so a hardcoded apt list silently breaks on a newer image.
sudo "$VENV/bin/playwright" install-deps chromium || {
  warn "install-deps failed; falling back to the explicit Ubuntu 22.04 list"
  sudo apt-get update -qq
  sudo apt-get install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
                          libgbm1 libasound2
}
"$VENV/bin/playwright" install chromium
ok "chromium ready"

# ---------------------------------------------------------------------------
step "STEP 3b  Swap (protects Playwright from the OOM killer on a 4GB box)"
if [ "$(swapon --show --noheadings | wc -l)" -gt 0 ]; then
  ok "swap already active: $(free -h | awk '/Swap/{print $2}')"
else
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  ok "2G swap enabled"
fi
# Persist across reboot -- `swapon` alone does not survive one.
grep -q '^/swapfile ' /etc/fstab || {
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  ok "swap recorded in /etc/fstab"
}

# ---------------------------------------------------------------------------
step "STEP 5a  Test suite"
if [ "${SKIP_TESTS:-0}" = "1" ]; then
  warn "SKIP_TESTS=1, skipping"
else
  ( cd "$APP_DIR" && "$PY" -m pytest tests/ -q ) || die "tests are red; not starting the daemon"
  ok "suite green"
fi

# ---------------------------------------------------------------------------
step "STEP 4  PM2 service"
# NOTE ON THE INVOCATION. `pm2 start "<interpreter> main.py -- --daemon"` does
# not work: pm2 treats the whole quoted string as ONE script path and fails to
# resolve it. The interpreter is the script; everything after `--` is argv.
cd "$APP_DIR"
if pm2 describe "$PM2_APP" >/dev/null 2>&1; then
  pm2 restart "$PM2_APP" --update-env
  ok "restarted $PM2_APP"
else
  pm2 start "$PY" --name "$PM2_APP" --time --cwd "$APP_DIR" -- main.py --daemon
  ok "started $PM2_APP"
fi

pm2 install pm2-logrotate >/dev/null 2>&1 || warn "pm2-logrotate install failed (non-fatal)"
pm2 set pm2-logrotate:max_size 10M    >/dev/null 2>&1 || true
pm2 set pm2-logrotate:retain 7        >/dev/null 2>&1 || true
pm2 set pm2-logrotate:compress true   >/dev/null 2>&1 || true
ok "log rotation: 10M x 7, compressed"

# Boot persistence. The pm2 path is derived, not hardcoded: it lives under
# /usr/lib/node_modules with apt-installed node but /usr/local/lib/... under
# NodeSource or nvm, and the wrong one fails only at the next reboot.
PM2_BIN="$(command -v pm2)"
sudo env PATH="$PATH:$(dirname "$(command -v node)")" \
     "$PM2_BIN" startup systemd -u "$APP_USER" --hp "/home/$APP_USER" >/dev/null
pm2 save >/dev/null
ok "boot persistence installed"

# ---------------------------------------------------------------------------
step "STEP 5b  Status and logs"
pm2 status
echo
pm2 logs "$PM2_APP" --lines 30 --nostream || true

# ---------------------------------------------------------------------------
step "STEP 5c  Telegram reachability ping"
( cd "$APP_DIR" && "$PY" main.py --selftest ) \
  || warn "selftest did not pass -- check credentials in .env / secrets/"

printf '\n\033[1;32mDeployment complete.\033[0m %s is under pm2 as "%s".\n' "$APP_DIR" "$PM2_APP"
