#!/usr/bin/env bash
#
# Push runtime secrets and state from the workstation to the Azure VM.
#
# Runs LOCALLY (Git Bash on Windows, or any POSIX shell). Everything here is
# gitignored by design -- credentials, the vault, live board cookies, the CV --
# so `git pull` on the VM can never deliver it and scp is the only route.
#
#   ./deploy/sync_to_azure.sh                     sync, then bootstrap
#   SYNC_ONLY=1 ./deploy/sync_to_azure.sh         sync only
#   VM_HOST=1.2.3.4 SSH_KEY=~/.ssh/x ./deploy/sync_to_azure.sh

set -euo pipefail

VM_HOST="${VM_HOST:-74.248.18.3}"
VM_USER="${VM_USER:-azureuser}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
APP_DIR="${APP_DIR:-/home/azureuser/AI_Job_Hunter_Bot}"
PM2_APP="${PM2_APP:-job-hunter-cloud}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$VM_USER@$VM_HOST")
SCP=(scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
step "Preflight"
"${SSH[@]}" -o BatchMode=yes -o ConnectTimeout=15 true 2>/dev/null \
  || die "cannot authenticate to $VM_USER@$VM_HOST with $SSH_KEY.
   Authorise the key first:
     ssh-copy-id -i ${SSH_KEY}.pub $VM_USER@$VM_HOST
   or paste $(basename "${SSH_KEY}.pub") into the VM's ~/.ssh/authorized_keys
   via the Azure portal (VM > Help > Reset password > Add SSH public key)."
ok "ssh to $VM_HOST"

# browser_sessions must exist before scp -r: `scp -r src/. dst/` refuses to
# create the destination and fails with a canonicalization error.
"${SSH[@]}" "mkdir -p '$APP_DIR/secrets' '$APP_DIR/state/browser_sessions' '$APP_DIR/assets'"

# ---------------------------------------------------------------------------
# Stop the daemon before touching the databases. SQLite in WAL mode keeps
# uncommitted pages in a sidecar file; copying the .db out from under a live
# writer yields a torn snapshot that looks fine until it does not.
RUNNING=0
if "${SSH[@]}" "pm2 describe '$PM2_APP' >/dev/null 2>&1"; then
  step "Pausing $PM2_APP for a consistent database copy"
  "${SSH[@]}" "pm2 stop '$PM2_APP'" >/dev/null && RUNNING=1
  ok "paused"
fi

# ---------------------------------------------------------------------------
step "Secrets"
# secrets/ holds vault.key, the Gmail OAuth token, the Telegram string session
# and every saved board login. Directory mode 700 on arrival.
[ -d "$HERE/secrets" ] || die "no secrets/ directory at $HERE"
"${SCP[@]}" -r "$HERE/secrets/." "$VM_USER@$VM_HOST:$APP_DIR/secrets/"
"${SSH[@]}" "chmod -R go-rwx '$APP_DIR/secrets'"
ok "secrets/ -> $APP_DIR/secrets/ (mode 700)"

if [ -f "$HERE/.env" ]; then
  "${SCP[@]}" "$HERE/.env" "$VM_USER@$VM_HOST:$APP_DIR/.env"
  "${SSH[@]}" "chmod 600 '$APP_DIR/.env'"
  ok ".env -> $APP_DIR/.env (mode 600)"
else
  warn "no .env locally -- the bot will start with no credentials"
fi

# ---------------------------------------------------------------------------
step "CV"
# The spec called this Hossam_Eldefrawy_CV.pdf; in this repo the master CV is
# assets/master_cv.pdf, and config.yml resolves it by that path.
for f in assets/master_cv.pdf assets/cv_profile.json; do
  if [ -f "$HERE/$f" ]; then
    "${SCP[@]}" "$HERE/$f" "$VM_USER@$VM_HOST:$APP_DIR/$f"
    ok "$f"
  else
    warn "$f not found locally"
  fi
done

# ---------------------------------------------------------------------------
step "Databases (consistent snapshots)"
SNAP="$(mktemp -d)"
trap 'rm -rf "$SNAP"' EXIT
for db in jobs.db vault.db; do
  src="$HERE/state/$db"
  [ -f "$src" ] || { warn "state/$db not found locally"; continue; }
  # sqlite3's online backup API checkpoints WAL and produces a single
  # self-consistent file -- unlike `cp`, which can catch a half-written page.
  python -c "
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
d = sqlite3.connect(dst)
s.backup(d); d.close(); s.close()
" "$src" "$SNAP/$db" || { warn "could not snapshot $db; skipping"; continue; }
  "${SCP[@]}" "$SNAP/$db" "$VM_USER@$VM_HOST:$APP_DIR/state/$db"
  ok "state/$db ($(du -h "$SNAP/$db" | cut -f1))"
done

# Stale sidecars from the OLD remote database would be read against the NEW
# one. They describe pages that no longer exist; delete them.
"${SSH[@]}" "rm -f '$APP_DIR'/state/*.db-wal '$APP_DIR'/state/*.db-shm '$APP_DIR'/state/*.db-journal"
ok "cleared stale WAL/SHM sidecars on the VM"

# ---------------------------------------------------------------------------
step "Browser sessions"
# A Chromium profile is mostly disposable cache: 341MB on disk, of which the
# part that actually carries a login -- Network/Cookies, Local State,
# Preferences, Local/Session Storage, IndexedDB -- is about 10MB. Shipping the
# rest means thousands of scp round-trips to deliver files Chromium throws away
# and regenerates on first launch.
#
# Singleton{Lock,Socket,Cookie} are excluded for a different reason: they are
# symlinks naming the HOST and PID that held the profile. Carried over they
# name a machine that is not this one, and Chromium either honours a lock
# nothing holds or trips over a dangling link.
PROFILE_EXCLUDES=(
  --exclude='*/Cache'
  --exclude='*/Code Cache'
  --exclude='*/GPUCache'
  --exclude='*/ShaderCache'
  --exclude='*/GrShaderCache'
  --exclude='*/DawnGraphiteCache'
  --exclude='*/DawnWebGPUCache'
  --exclude='*/BrowserMetrics'
  --exclude='*/Crashpad'
  --exclude='*/component_crx_cache'
  --exclude='*/extensions_crx_cache'
  --exclude='*/CacheStorage'
  --exclude='*/ScriptCache'
  --exclude='SingletonLock'
  --exclude='SingletonSocket'
  --exclude='SingletonCookie'
)
if [ -d "$HERE/state/browser_sessions" ]; then
  profiles=$(find "$HERE/state/browser_sessions" -maxdepth 1 -mindepth 1 -type d | wc -l)
  tar -cz -C "$HERE/state/browser_sessions" "${PROFILE_EXCLUDES[@]}" . \
    | "${SSH[@]}" "tar -xz -C '$APP_DIR/state/browser_sessions'"
  ok "$profiles board profile(s), caches excluded"
else
  warn "no state/browser_sessions/ locally"
fi

# ---------------------------------------------------------------------------
if [ "${SYNC_ONLY:-0}" = "1" ]; then
  [ "$RUNNING" = "1" ] && "${SSH[@]}" "pm2 start '$PM2_APP'" >/dev/null && ok "$PM2_APP resumed"
  printf '\n\033[1;32mSync complete.\033[0m\n'
  exit 0
fi

step "Handing off to the VM bootstrap"
"${SSH[@]}" "cd '$APP_DIR' && git pull --ff-only && bash deploy/azure_bootstrap.sh"
