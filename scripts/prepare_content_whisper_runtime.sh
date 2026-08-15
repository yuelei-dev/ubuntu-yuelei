#!/usr/bin/env bash
set -euo pipefail

# Prepare the local subtitle runtime before huangque-content is restarted.
# This script intentionally never restarts services: deployment must chain the
# restart with && so any cache/download/offline verification failure is fatal.

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "prepare_content_whisper_runtime.sh must run as root" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PREPARE_SCRIPT="${SCRIPT_DIR}/prepare_content_whisper.py"
PYTHON_BIN="${CONTENT_PYTHON_BIN:-/usr/bin/python3}"
RUNUSER_BIN="${RUNUSER_BIN:-$(command -v runuser || true)}"
SERVICE_USER="${WHISPER_SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${WHISPER_SERVICE_GROUP:-ubuntu}"
CACHE_DIR="${WHISPER_CACHE_DIR:-/var/cache/huangque/faster-whisper}"
MODEL="${WHISPER_MODEL:-small}"
DEVICE="${WHISPER_DEVICE:-cpu}"
COMPUTE_TYPE="${WHISPER_COMPUTE_TYPE:-int8}"

if [[ ! -x "${PYTHON_BIN}" || ! -f "${PREPARE_SCRIPT}" ]]; then
  echo "content Python or Whisper prepare script is unavailable" >&2
  exit 1
fi
if [[ -z "${RUNUSER_BIN}" || ! -x "${RUNUSER_BIN}" ]]; then
  echo "runuser is unavailable" >&2
  exit 1
fi
if [[ ! "${SERVICE_USER}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] \
    || [[ ! "${SERVICE_GROUP}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
  echo "invalid content service user or group" >&2
  exit 1
fi

"${PYTHON_BIN}" "${PREPARE_SCRIPT}" \
  --cache-dir "${CACHE_DIR}" \
  --prepare-cache \
  --service-user "${SERVICE_USER}" \
  --service-group "${SERVICE_GROUP}"

"${RUNUSER_BIN}" -u "${SERVICE_USER}" -- env \
  WHISPER_MODEL="${MODEL}" \
  WHISPER_DEVICE="${DEVICE}" \
  WHISPER_COMPUTE_TYPE="${COMPUTE_TYPE}" \
  WHISPER_CACHE_DIR="${CACHE_DIR}" \
  "${PYTHON_BIN}" "${PREPARE_SCRIPT}"

"${RUNUSER_BIN}" -u "${SERVICE_USER}" -- env \
  HF_HUB_OFFLINE=1 \
  WHISPER_MODEL="${MODEL}" \
  WHISPER_DEVICE="${DEVICE}" \
  WHISPER_COMPUTE_TYPE="${COMPUTE_TYPE}" \
  WHISPER_CACHE_DIR="${CACHE_DIR}" \
  "${PYTHON_BIN}" "${PREPARE_SCRIPT}" --verify-only

echo "Whisper runtime verified; huangque-content may now be restarted"
