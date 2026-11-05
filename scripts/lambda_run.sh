#!/usr/bin/env bash
# Run ON the Lambda GPU box: the merged Metis site (property prediction + Metis Lead Studio (Beta)) in tmux session `metis-app`.
#   scripts/lambda_run.sh start|stop|status|logs
# Binds 127.0.0.1:8080 only; reach it from your laptop with:  ssh -L 8080:127.0.0.1:8080 ubuntu@<box>
# DynamicBind (tmux `dbsvc`, 127.0.0.1:8100) must be running; binding_module (PLAPT) is imported from ~/studio-app.
# Models in property prediction are SYNTHETIC sample models; see scripts/demo.py.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${METIS_PY:-$HOME/metis312/bin/python}"
case "${1:-status}" in
  start)
    tmux kill-session -t metis-app 2>/dev/null || true
    tmux new-session -d -s metis-app "bash -lc 'set -a; [ -f ~/.studio_env ] && . ~/.studio_env; set +a; export PYTHONNOUSERSITE=1 PYTHONPATH=\$HOME/studio-app; cd $ROOT && $PY scripts/demo.py --port 8080 2>&1 | tee ~/metis-app.log'"
    echo "started; logs: tail -f ~/metis-app.log" ;;
  stop) tmux send-keys -t metis-app C-c 2>/dev/null || true; sleep 3; tmux kill-session -t metis-app 2>/dev/null || true ;;
  logs) tail -n 60 ~/metis-app.log ;;
  status) tmux ls 2>/dev/null || true; ss -ltn | grep -E ':(8080|8100) ' || true
          curl -s -m 5 -o /dev/null -w "site:%{http_code}\n" http://127.0.0.1:8080/ || true ;;
esac
