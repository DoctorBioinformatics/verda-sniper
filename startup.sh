#!/bin/bash
# Runs as root on first boot of a freshly booked ac2-prod box.
#
# Does everything that does NOT need the private ac2 repo: installs Ollama at
# the pinned version, applies the VRAM-residency config, pulls the four models
# (~28 GB, the slow part) and warms them so they are resident. After this the
# only remaining work is rsyncing the code and starting the service.
#
# Ollama MUST stay pinned to 0.24.0. An unpinned install.sh grabs latest, which
# reintroduces a TTFT regression measured at ~1.35s p50 versus ~0.44s.
set -uo pipefail
exec > >(tee -a /var/log/ac2-startup.log) 2>&1
echo "=== ac2 startup $(date -u) ==="

OLLAMA_PIN_VERSION=0.24.0
MAIN_MODEL=gemma4:26b-a4b-it-q4_K_M
ROUTER_MODEL=qwen2.5:7b
GUARD_MODEL=llama-guard3:8b
EMBED_MODEL=nomic-embed-text
MAIN_NUM_CTX=8192
ROUTER_NUM_CTX=12288
GUARD_NUM_CTX=4096

echo "--- installing ollama $OLLAMA_PIN_VERSION"
curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION="$OLLAMA_PIN_VERSION" sh

# These belong on the Ollama SERVER unit, not the app .env - in .env they are
# inert for pinning.
echo "--- ollama server config"
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/ac2.conf <<EOF
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_MAX_LOADED_MODELS=5"
Environment="OLLAMA_NUM_PARALLEL=4"
Environment="OLLAMA_FLASH_ATTENTION=1"
EOF
systemctl daemon-reload
systemctl enable --now ollama
sleep 10

echo "--- pulling models (the slow part)"
for m in "$MAIN_MODEL" "$ROUTER_MODEL" "$GUARD_MODEL" "$EMBED_MODEL"; do
  echo "pulling $m"
  ollama pull "$m" || echo "WARN: pull failed for $m"
done

# KEEP_ALIVE=-1 pins a model only once it is loaded; Ollama does not pre-load.
# Warm each one out-of-band at its configured num_ctx so it becomes resident.
echo "--- warming models to make them resident"
warm() {
  local model="$1" ctx="$2"
  curl -s http://127.0.0.1:11434/api/generate -d "{
    \"model\": \"$model\", \"prompt\": \"hi\", \"stream\": false,
    \"keep_alive\": -1, \"options\": {\"num_ctx\": $ctx}
  }" > /dev/null && echo "warmed $model (num_ctx $ctx)" || echo "WARN: warm failed $model"
}
warm "$MAIN_MODEL" "$MAIN_NUM_CTX"
warm "$ROUTER_MODEL" "$ROUTER_NUM_CTX"
warm "$GUARD_MODEL" "$GUARD_NUM_CTX"
curl -s http://127.0.0.1:11434/api/embeddings -d "{\"model\":\"$EMBED_MODEL\",\"prompt\":\"hi\",\"keep_alive\":-1}" >/dev/null

echo "--- resident models:"
curl -s http://127.0.0.1:11434/api/ps
echo
echo "--- disk:"; df -h / | tail -1
echo "=== ac2 startup done $(date -u). Remaining: rsync code to /root/ac2, run scripts/verda_bootstrap.sh ==="
