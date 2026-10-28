#!/usr/bin/env bash
# Default: fetch only pinned source. Explicit flags install dependencies/weights.
set -euo pipefail

AGENTJEV_REVISION='741c58f567681b78e4afa1e12d7491d00fda69e7'
AGENTJEV_ORIGIN='https://github.com/malevrigns/agent-jev.git'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENTJEV_DEST="${AGENTJEV_DEST:-${SCRIPT_DIR}/../vendor/agent-jev}"
AGENTJEV_PYTHON="${AGENTJEV_PYTHON:-python3}"
INSTALL_DEPS=0
DOWNLOAD_WEIGHTS=0

for arg in "$@"; do
  case "$arg" in
    --install) INSTALL_DEPS=1 ;;
    --download-weights) DOWNLOAD_WEIGHTS=1 ;;
    *) printf 'Usage: %s [--install] [--download-weights]\n' "$0" >&2; exit 2 ;;
  esac
done

if [[ -e "${AGENTJEV_DEST}" ]]; then
  if [[ ! -d "${AGENTJEV_DEST}/.git" ]]; then
    printf 'Refusing to overwrite an existing non-Git directory: %s\n' "${AGENTJEV_DEST}" >&2
    exit 1
  fi
  if [[ -n "$(git -C "${AGENTJEV_DEST}" status --porcelain)" ]]; then
    printf 'Existing AgentJev checkout has local changes; preserve them and use a clean destination.\n' >&2
    exit 1
  fi
  if [[ "$(git -C "${AGENTJEV_DEST}" remote get-url origin)" != "${AGENTJEV_ORIGIN}" ]] ||
     [[ "$(git -C "${AGENTJEV_DEST}" rev-parse HEAD)" != "${AGENTJEV_REVISION}" ]]; then
    printf 'Existing checkout differs from the pinned origin/revision; no branch was modified.\n' >&2
    exit 1
  fi
else
  mkdir -p -- "$(dirname -- "${AGENTJEV_DEST}")"
  git clone --filter=blob:none --no-checkout "${AGENTJEV_ORIGIN}" "${AGENTJEV_DEST}"
  git -C "${AGENTJEV_DEST}" checkout --detach "${AGENTJEV_REVISION}"
fi

if [[ "${INSTALL_DEPS}" == 1 ]]; then
  "${AGENTJEV_PYTHON}" -c 'import torch; print("Using installed PyTorch:", torch.__version__, "CUDA available:", torch.cuda.is_available())'
  "${AGENTJEV_PYTHON}" -m pip install -r "${AGENTJEV_DEST}/requirements.txt" huggingface_hub safetensors
fi

if [[ "${DOWNLOAD_WEIGHTS}" == 1 ]]; then
  # Upstream pins revision 7d433994fbde17a3f0993c2f2b02fe8ca1370db1 and checks hashes.
  # This downloads roughly 1.2 GB; no weights are bundled in this harness.
  (cd -- "${AGENTJEV_DEST}" && "${AGENTJEV_PYTHON}" fetch_v1_weights.py)
fi

printf '\nSource ready at %s\n' "${AGENTJEV_DEST}"
printf 'Install a CUDA-compatible PyTorch in your dedicated environment before --install.\n'
printf 'Read docs/agentjev.md for weight download, serving, and calibration.\n'
