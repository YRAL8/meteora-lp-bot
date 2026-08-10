#!/usr/bin/env bash
# LITE_2: state_volume gate removed — this scenario is retired.
# Kept as a stub so old docs/scripts that call it do not confuse operators.
set -euo pipefail
echo "LITE_2: docker volume gate removed (state_volume.py deleted)."
echo "Startup now only mkdir(state/); no mountinfo / mainnet hard-stop on volume."
echo "GATE_RETIRED: OK"
exit 0
