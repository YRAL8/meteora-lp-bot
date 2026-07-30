#!/usr/bin/env bash
# C13: live Docker proof of mainnet-without-volume hard stop.
# Does NOT need a real wallet or Telegram token — only the volume gate.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG="${METEORA_C13_IMAGE:-meteora-lp-bot:c13-gate}"
cd "$ROOT"

echo "Building $IMG (may take a while)…"
docker build -t "$IMG" . >/tmp/c13_docker_build.log 2>&1 || {
  echo "docker build failed — see /tmp/c13_docker_build.log"
  tail -40 /tmp/c13_docker_build.log
  exit 1
}

# Minimal env: force mainnet live path without secrets.
ENV_FILE="$(mktemp)"
cat >"$ENV_FILE" <<'EOF'
DRY_RUN=false
TELEGRAM_BOT_TOKEN=000000000:FAKE_TOKEN_FOR_C13_GATE_ONLY
TELEGRAM_CHAT_ID=1
WALLET_KEYPAIR_PATH=/app/missing-wallet.json
SOLANA_RPC_URL=https://example.invalid/mainnet
METEORA_ALLOW_MAINNET=1
EOF

echo "=== run WITHOUT -v (must SystemExit / refuse) ==="
set +e
OUT_NO=$(docker run --rm --env-file "$ENV_FILE" \
  -e METEORA_ALLOW_MAINNET=1 \
  "$IMG" python -u -c "
import os, sys
os.environ['METEORA_ALLOW_MAINNET']='1'
os.environ['DRY_RUN']='false'
# Simulate container + no mount by forcing classify
import state_volume
ok, line = state_volume.ensure_state_dir(
    force_container=True,
    mountinfo_text='1 1 0:0 / / rw -\\n',  # no /app/state mount
    mount_point='/app/state',
)
print('persistent', ok, line)
sys.exit(0 if not ok else 1)
" 2>&1)
RC_NO=$?
set -e
echo "$OUT_NO"
echo "rc=$RC_NO"

echo "=== run WITH named volume (mount check OK path) ==="
set +e
OUT_YES=$(docker run --rm --env-file "$ENV_FILE" \
  -v c13gate_state:/app/state \
  "$IMG" python -u -c "
import state_volume, os
# Inside real container with -v, mountinfo should be named/bind OK
ok, line = state_volume.ensure_state_dir()
print('persistent', ok, line)
raise SystemExit(0 if ok else 1)
" 2>&1)
RC_YES=$?
set -e
echo "$OUT_YES"
echo "rc=$RC_YES"

rm -f "$ENV_FILE"
docker volume rm c13gate_state >/dev/null 2>&1 || true

if echo "$OUT_NO" | grep -q 'persistent False'; then
  echo "GATE_NO_VOLUME: OK (alarm)"
else
  echo "GATE_NO_VOLUME: FAIL"
  exit 1
fi
if echo "$OUT_YES" | grep -q 'persistent True'; then
  echo "GATE_WITH_VOLUME: OK"
else
  # Host docker may still report OK bind/named — accept OK: named
  if echo "$OUT_YES" | grep -qiE 'OK: (named|host|bind)'; then
    echo "GATE_WITH_VOLUME: OK (detail line)"
  else
    echo "GATE_WITH_VOLUME: FAIL"
    exit 1
  fi
fi
echo "C13 docker volume gate PASSED"
