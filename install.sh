#!/usr/bin/env bash
# install.sh — Debian/Ubuntu setup for the image-to-layers project.
# Tested on: Ubuntu 22.04/24.04, Debian 12 (bookworm), Debian 13 (trixie).

set -euo pipefail

if [ -t 1 ]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; NC=""
fi
log()  { printf '%s[+]%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s[!]%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
TORCH_INDEX="${TORCH_INDEX:-auto}"
INSTALL_SAM="${INSTALL_SAM:-1}"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

# ── Distro detection ──────────────────────────────────────────────────────
. /etc/os-release
DISTRO_ID="${ID:-unknown}"
DISTRO_VER="${VERSION_ID:-0}"
log "Detected: ${DISTRO_ID} ${DISTRO_VER}"

# ── 1. Base packages (distro-aware) ───────────────────────────────────────
$SUDO apt-get update -y

BASE_PKGS=(
    build-essential ca-certificates curl git wget
    ninja-build python3 python3-dev python3-pip
)
# libglib2.0-0 was renamed to libglib2.0-0t64 in Debian 13 / Ubuntu 24.04+
if apt-cache show libglib2.0-0t64 >/dev/null 2>&1; then
    BASE_PKGS+=(libglib2.0-0t64)
else
    BASE_PKGS+=(libglib2.0-0)
fi
BASE_PKGS+=(
    libgl1 libsm6 libxext6 libxrender1 libgomp1
)

# python3-venv is a transitional package on Debian 13+; python3-full is the
# reliable way to get venv + ensurepip there.
if [ "$DISTRO_ID" = "debian" ] && [ "$DISTRO_VER" -ge 13 ] 2>/dev/null; then
    BASE_PKGS+=(python3-full)
elif apt-cache show python3-full >/dev/null 2>&1; then
    BASE_PKGS+=(python3-venv python3-full)
else
    BASE_PKGS+=(python3-venv)
fi

# software-properties-common was removed from Debian 13 (trixie).
if [ "$DISTRO_ID" != "debian" ] || [ "$DISTRO_VER" -lt 13 ] 2>/dev/null; then
    BASE_PKGS+=(software-properties-common)
fi

log "Installing base packages…"
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${BASE_PKGS[@]}"

# ── 2. Python >= 3.10 check ───────────────────────────────────────────────
PY_MAJOR=$(python3 -c 'import sys;print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys;print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    if [ "$DISTRO_ID" = "debian" ] && [ "$DISTRO_VER" -ge 13 ]; then
        die "Python $(python3 -V) too old and deadsnakes is Ubuntu-only. Install a newer Python manually."
    fi
    warn "Python ${PY_MAJOR}.${PY_MINOR} too old; adding deadsnakes PPA…"
    $SUDO add-apt-repository -y ppa:deadsnakes/ppa
    $SUDO apt-get update -y
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y "python3.12" "python3.12-venv" "python3.12-dev"
    PYBIN="python3.12"
else
    PYBIN="python3"
fi
log "Using $($PYBIN -V) at $($PYBIN -c 'import sys;print(sys.executable)')"

# ── 3. Virtualenv ─────────────────────────────────────────────────────────
WANT_VER="$($PYBIN -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if [ -x "$VENV_DIR/bin/python" ]; then
    CUR_VER="$("$VENV_DIR/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo unknown)"
    if [ "$CUR_VER" != "$WANT_VER" ]; then
        warn "Recreating venv (had Python $CUR_VER, want $WANT_VER)"
        rm -rf "$VENV_DIR"
    fi
fi
[ -d "$VENV_DIR" ] || "$PYBIN" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip

# ── 4. torch (CPU / CUDA) ─────────────────────────────────────────────────
if [ "$TORCH_INDEX" = "auto" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        DRV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | cut -d. -f1 | tr -d ' ')
        if [ -n "$DRV" ] && [ "$DRV" -ge 550 ]; then TORCH_INDEX="cu124"; else TORCH_INDEX="cu121"; fi
    else
        TORCH_INDEX="cpu"
    fi
fi
if [ "$TORCH_INDEX" = "cpu" ]; then
    TORCH_URL="https://download.pytorch.org/whl/cpu"
else
    TORCH_URL="https://download.pytorch.org/whl/${TORCH_INDEX}"
fi
log "Installing torch from $TORCH_URL"
python -m pip install --index-url "$TORCH_URL" torch torchvision

# ── 5. Project deps ───────────────────────────────────────────────────────
log "Installing Pillow…"
python -m pip install "Pillow>=10.0.0"

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    log "Installing requirements.txt (torch already installed)…"
    python -m pip install -r "$SCRIPT_DIR/requirements.txt" \
        || warn "Some requirements.txt packages failed; core modes still work."
fi

if [ "$INSTALL_SAM" = "1" ]; then
    log "Installing SAM 2 from git…"
    python -m pip install "git+https://github.com/facebookresearch/sam2.git" \
        || warn "SAM 2 install failed — Automatic SAM unavailable, other modes still work."
fi

# ── 6. Data dirs + .env ───────────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR"/data/{projects,downloads,imports,uploads}
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cat > "$SCRIPT_DIR/.env" <<'ENV'
# DEEPSEEK_API_KEY=sk-...
# ITL_VISION_MODEL=deepseek-flash
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# ITL_SSL_VERIFY=true  # set to false only behind a custom/self-signed MITM proxy cert
# ITL_OLLAMA_BASE_URL=http://127.0.0.1:11434
# ITL_OLLAMA_API_KEY=
ENV
    warn "Created placeholder .env — add DEEPSEEK_API_KEY for DeepSeek, or run Ollama locally."
fi

# ── 7. Verify ─────────────────────────────────────────────────────────────
log "Verifying…"
python - <<'PY'
import sys
print("python      ", sys.version.split()[0])
for mod, name in [("PIL","Pillow"),("numpy","numpy"),("torch","torch"),
                  ("torchvision","torchvision"),("transformers","transformers")]:
    try:
        m = __import__(mod); print(f"{name:<12}", getattr(m,"__version__","?"))
    except Exception: print(f"{name:<12}", "MISSING")
for mod, name in [("sam2","sam2"),("rembg","rembg")]:
    try: __import__(mod); print(f"{name:<12} ok")
    except Exception: print(f"{name:<12} not installed")
try:
    import torch
    print("cuda        ", torch.version.cuda if torch.cuda.is_available() else "no")
except Exception: pass
PY

cat <<EOF

${BOLD}Done.${NC}

    source "$VENV_DIR/bin/activate"
    python image-to-layers-server.py

Then open http://127.0.0.1:8009/image-to-layers-server.html

EOF