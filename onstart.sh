#!/bin/bash
# ComfyUI + AnimaWebUI セットアップ (Vast.ai onstart)
# ANIMA_REPO_PATH, SETUP_REPO 等は環境変数で注入
set -e

W=/workspace; C=$W/ComfyUI; A=$W/anima-webui
STATUS=$W/setup_status.txt
exec > >(tee -a "$W/setup.log") 2>&1

phase() { echo "$1" > "$STATUS"; echo "=== $1 ==="; }

echo "[$(date)] セットアップ開始"
echo "starting" > "$STATUS"

# ── Phase 1: システム ──
phase "Phase 1: System"
apt-get update -qq
apt-get install -y -qq git curl wget aria2 ca-certificates gnupg >/dev/null 2>&1

NODE_MAJOR=20
if ! command -v node &>/dev/null || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt "$NODE_MAJOR" ]; then
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list
    apt-get update -qq; apt-get install -y -qq nodejs >/dev/null 2>&1
fi
pip install -q --upgrade pip
echo "[OK] System ready"

# ── Phase 2: ComfyUI ──
phase "Phase 2: ComfyUI"
if [ ! -d "$C" ]; then
    cd $W && git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git
    cd $C
    pip install -q -r requirements.txt
    pip install -q torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
    echo "[OK] ComfyUI installed"
else
    echo "[SKIP] ComfyUI exists"
fi

# ── Phase 3: カスタムノード ──
phase "Phase 3: Custom Nodes"
N=$C/custom_nodes; mkdir -p $N
install_node() {
    [ -d "$N/$2" ] && return 0
    git clone --depth 1 "$1" "$N/$2" 2>/dev/null || return 0
    [ -f "$N/$2/requirements.txt" ] && pip install -q -r "$N/$2/requirements.txt" 2>/dev/null || true
}
install_node "https://github.com/kijai/ComfyUI-KJNodes.git" "ComfyUI-KJNodes"
install_node "https://github.com/ltdrdata/ComfyUI-Manager.git" "ComfyUI-Manager"
echo "[OK] Custom nodes ready"

# ── Phase 4: モデル (HuggingFace) ──
phase "Phase 4: Models (HF)"
pip install -q huggingface_hub
M=$C/models
mkdir -p $M/diffusion_models $M/text_encoders $M/vae $M/upscale_models

python <<'PY'
from huggingface_hub import hf_hub_download as h
import shutil, os
R="circlestone-labs/Anima"; M="/workspace/ComfyUI/models"
for d in ["diffusion_models/anima-preview.safetensors",
          "text_encoders/qwen_3_06b_base.safetensors",
          "vae/qwen_image_vae.safetensors"]:
    t = M + "/" + d
    if not os.path.exists(t):
        p = h(R, "split_files/" + d)
        os.makedirs(os.path.dirname(t), exist_ok=True)
        shutil.copy2(p, t)
        print(f"[DL] {os.path.basename(t)}")
# botan_anima
bt = M + "/diffusion_models/botan_animaTest_V0_3.safetensors"
if not os.path.exists(bt):
    shutil.copy2(h("KKTT8823/botan_anima", "試作品/botan_animaTest_V0_3.safetensors"), bt)
    print("[DL] botan_animaTest_V0_3")
# upscaler
us = M + "/upscale_models/2x-AnimeSharpV4_Fast_RCAN_PU.safetensors"
if not os.path.exists(us):
    shutil.copy2(h("Kim2091/2x-AnimeSharpV4", "2x-AnimeSharpV4_Fast_RCAN_PU.safetensors"), us)
    print("[DL] 2x-AnimeSharpV4")
PY
echo "[OK] HF models ready"

# ── Phase 4b: モデル (CivitAI) ──
phase "Phase 4b: Models (CivitAI)"
if [ -n "${CIVITAI_API_KEY}" ]; then
    D=$M/diffusion_models; K=$CIVITAI_API_KEY; U=https://civitai.com/api/download/models
    dl() { [ ! -f "$D/$1" ] && aria2c -x16 -s16 -d $D -o "$1" "$U/$2&token=$K" || true; }
    dl copycatAnima_20260209.safetensors '2673536?type=Model&format=SafeTensor&size=full&fp=fp16'
    dl copycatAnima_0302.safetensors '2737875?type=Model&format=SafeTensor&size=full&fp=fp16'
    dl cottonanima_preview.safetensors '2678837?type=Model&format=SafeTensor&size=full&fp=fp16'
    dl animaCatTower_v02_pruned_bf16.safetensors '2688353?type=Model&format=SafeTensor&size=pruned&fp=bf16'
    echo "[OK] CivitAI models ready"
else
    echo "[SKIP] CIVITAI_API_KEY unset"
fi

# ── Phase 5: AnimaWebUI ──
phase "Phase 5: AnimaWebUI"
if [ ! -d "$A" ]; then
    REPO_URL="https://github.com/${ANIMA_REPO_PATH}.git"
    [ -n "${GH_TOKEN:-}" ] && REPO_URL="https://${GH_TOKEN}@github.com/${ANIMA_REPO_PATH}.git"
    BRANCH_OPT="-b stable"
    [ -n "${DEPLOY_BRANCH:-}" ] && BRANCH_OPT="-b ${DEPLOY_BRANCH}"
    git clone --depth 1 $BRANCH_OPT "$REPO_URL" "$A" 2>/dev/null
    echo "[OK] AnimaWebUI cloned"
else
    echo "[SKIP] AnimaWebUI exists"
fi

if [ -d "$A" ]; then
    cat > "$A/.env" <<EOF
PORT=8501
COMFYUI_URL=http://127.0.0.1:8188
PASSPHRASE=${ANIMA_PASSPHRASE:-}
IMAGE_DIR=./data/images
DB_PATH=./data/anima.db
EOF
    cd "$A" && npm install --production 2>/dev/null
    echo "[OK] AnimaWebUI configured"
fi

# ── Phase 6: サービス起動 ──
phase "Phase 6: Start Services"
cd $C
nohup python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch > $W/comfyui.log 2>&1 &
echo "[OK] ComfyUI starting (PID: $!)"

for i in $(seq 1 60); do
    curl -s http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && break
    sleep 2
done

if [ -d "$A" ] && [ -f "$A/server/index.js" ]; then
    cd "$A"
    nohup node server/index.js > $W/anima.log 2>&1 &
    echo "[OK] AnimaWebUI starting (PID: $!)"
fi

# ── Phase 7: Cloudflare Tunnel ──
phase "Phase 7: Tunnel"
command -v cloudflared &>/dev/null || {
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
}
nohup cloudflared tunnel --url http://localhost:8501 > $W/cloudflared.log 2>&1 &

CF_URL=""
for i in $(seq 1 30); do
    CF_URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' $W/cloudflared.log 2>/dev/null | head -1)
    [ -n "$CF_URL" ] && break
    sleep 1
done

# ブローカーにトンネルURLを自動登録
BROKER="https://anima-broker.beamman.workers.dev/register"
if [ -n "$CF_URL" ]; then
    curl -s -X POST "$BROKER" -H "Content-Type: application/json" \
         -d "{\"url\":\"${CF_URL}\"}" >/dev/null 2>&1 \
        && echo "[OK] Broker registered: ${CF_URL}" \
        || echo "[WARN] Broker registration failed"
fi

echo "done" > "$STATUS"
echo ""
echo "============================================"
echo "  セットアップ完了！"
echo "  ComfyUI:    http://0.0.0.0:8188"
echo "  AnimaWebUI: http://0.0.0.0:8501"
[ -n "$CF_URL" ] && echo "  Cloudflare: ${CF_URL}"
echo "============================================"
echo "[$(date)] done"
