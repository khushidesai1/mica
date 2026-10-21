#!/usr/bin/env bash
# Mica adaptation: worker download-only mode bypasses upstream discovery/upload.
# Upstream pinned at 5d864b6cd13f1873364aa70b9ac1939f4c656616.
if [[ "${1:-}" == "--download-only" ]]; then
  exec bash "$(dirname "$0")/download_manifest.sh" "$2"
fi
#
# get_tma.sh
# Given just the TMA root dir and TMA name, this:
#   1. Asks the SFTP server what the "S#" run subfolder is under
#      DecodedFiles/<tma>/ (no need to know that timestamp yourself)
#   2. Downloads Morphology2D, the RunSummary dictionary file, and
#      flatFiles/<tma> into the right places under your local <tma>/ folder
#   3. Only prints/logs SUCCESS if the whole thing completes cleanly
#
set -uo pipefail

# ---------------- CONFIG (from the environment) ----------------
SFTP_HOST="${ATOMX_SFTP_HOST:-na.export.atomx.nanostring.com}"
SFTP_USER="${ATOMX_SFTP_USER:-}"          # your AtoMx export login
SFTP_PORT="${ATOMX_SFTP_PORT:-22}"
LOG_DIR="${LOG_DIR:-./logs}"
RUN_SUMMARY_FILE="Morphology_ChannelID_Dictionary.txt"
GCS_DEST_BASE="${GCS_RAW_DEST_BASE:-}"     # e.g. gs://<bucket>/raw_outputs/raw
# -------------------------------------------------------
#
# Usage:
#   export SFTP_PASSWORD='yourpassword'   # or set ATOMX_PASSWORD_FILE and use run_batch.sh
#   ./get_tma.sh <root_dir> <tma_name> [local_dest]
#   ./get_tma.sh /MyExport_17_08_2026_10_31_17_209 tma33
#
# <root_dir>    the changing top-level remote folder, e.g.
#               /MyExport_17_08_2026_10_31_17_209
# <tma_name>    e.g. tma33
# [local_dest]  optional, defaults to <tma_name> in the current directory
#
# Requires sshpass (apt install sshpass / brew install hudochenkov/sshpass/sshpass)

if [[ -z "${1:-}" || -z "${2:-}" ]]; then
  echo "Usage: $0 <root_dir> <tma_name> [local_dest]" >&2
  echo "Example: $0 /MyExport_17_08_2026_10_31_17_209 tma33" >&2
  exit 1
fi
ROOT_DIR="$1"
TMA_NAME="$2"
LOCAL_DEST="${3:-$TMA_NAME}"

# Settings come from the environment (see examples/env.example); nothing site-specific
# is hardcoded. Checked here, after the usage message.
for var in ATOMX_SFTP_USER GCS_RAW_DEST_BASE; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var is not set. See examples/env.example." >&2
    exit 1
  fi
done

if [[ -z "${SFTP_PASSWORD:-}" ]]; then
  echo "ERROR: SFTP_PASSWORD environment variable is not set. Run: export SFTP_PASSWORD='yourpassword'" >&2
  exit 1
fi
export SSHPASS="$SFTP_PASSWORD"   # sshpass -e specifically reads the SSHPASS env var

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${LOG_DIR}/sftp_transfer_${TMA_NAME}_${TIMESTAMP}.log"
mkdir -p "$LOG_DIR" "$LOCAL_DEST"

SFTP_OPTS=(-o "Port=${SFTP_PORT}" -o "StrictHostKeyChecking=accept-new")
DECODED_BASE="${ROOT_DIR}/DecodedFiles/${TMA_NAME}"

{
  echo "=== Starting TMA transfer at $(date) ==="
  echo "Root dir: ${ROOT_DIR}"
  echo "TMA name: ${TMA_NAME}"
  echo "Local destination: ${LOCAL_DEST}"
} > "$LOG_FILE"

# ---- Step 1: discover the run subfolder (e.g. 20260522_231022_S1) ----
echo "Discovering run subfolder under ${DECODED_BASE} ..." >> "$LOG_FILE"

LIST_OUTPUT=$(sshpass -e sftp "${SFTP_OPTS[@]}" "${SFTP_USER}@${SFTP_HOST}" 2>>"$LOG_FILE" <<EOF
ls -1 ${DECODED_BASE}
bye
EOF
)
LIST_EXIT=$?

echo "$LIST_OUTPUT" >> "$LOG_FILE"

if [[ $LIST_EXIT -ne 0 ]]; then
  echo "----------------------------------------" >> "$LOG_FILE"
  echo "ERROR: Could not list ${DECODED_BASE} (exit code ${LIST_EXIT}) at $(date)" >> "$LOG_FILE"
  echo "ERROR: Failed to discover run subfolder. See ${LOG_FILE} for details." >&2
  exit 1
fi

# The server returns full paths for each entry (not bare names), and
# sftp echoes "sftp>" prompts too. Match the timestamp pattern at the end
# of a line (e.g. .../20260522_223456_S2), which naturally excludes
# non-run folders like "Logs" and the echoed prompt/command lines, then
# strip it down to just the folder name. If more than one run folder
# matches, the alphabetically-last one is picked (timestamp prefixes sort
# chronologically, so this is the most recent run).
RUN_SUBDIR=$(echo "$LIST_OUTPUT" | grep -oE '[0-9]{8}_[0-9]{6}_S[0-9]+$' | sort | tail -n 1)

if [[ -z "$RUN_SUBDIR" ]]; then
  echo "----------------------------------------" >> "$LOG_FILE"
  echo "ERROR: No run subfolder found under ${DECODED_BASE} at $(date)" >> "$LOG_FILE"
  echo "ERROR: No run subfolder found under ${DECODED_BASE}. See ${LOG_FILE}." >&2
  exit 1
fi

echo "Found run subfolder: ${RUN_SUBDIR}" >> "$LOG_FILE"
echo "----------------------------------------" >> "$LOG_FILE"

# ---- Step 2: build the real remote paths ----
RUN_DIR="${DECODED_BASE}/${RUN_SUBDIR}"
REMOTE_MORPHOLOGY="${RUN_DIR}/CellStatsDir/Morphology2D"
REMOTE_RUNSUMMARY="${RUN_DIR}/RunSummary/${RUN_SUMMARY_FILE}"
REMOTE_FLATFILES="${ROOT_DIR}/flatFiles/${TMA_NAME}"

{
  echo "Morphology2D : ${REMOTE_MORPHOLOGY}"
  echo "RunSummary   : ${REMOTE_RUNSUMMARY}"
  echo "flatFiles    : ${REMOTE_FLATFILES}"
  echo "----------------------------------------"
} >> "$LOG_FILE"

# ---- Step 3: pull everything in one session ----
sshpass -e sftp "${SFTP_OPTS[@]}" "${SFTP_USER}@${SFTP_HOST}" >> "$LOG_FILE" 2>&1 <<EOF
lcd ${LOCAL_DEST}
get -r ${REMOTE_MORPHOLOGY}
get ${REMOTE_RUNSUMMARY}
get -r ${REMOTE_FLATFILES}/*
bye
EOF

EXIT_CODE=$?

echo "----------------------------------------" >> "$LOG_FILE"

if [[ $EXIT_CODE -ne 0 ]]; then
  echo "ERROR: Transfer failed or connection closed early (exit code ${EXIT_CODE}) at $(date)" >> "$LOG_FILE"
  echo "ERROR: SFTP transfer failed. See ${LOG_FILE} for details." >&2
  exit 1
fi

echo "SFTP download completed at $(date)" >> "$LOG_FILE"
echo "----------------------------------------" >> "$LOG_FILE"

# ---- Step 4: upload the downloaded TMA folder to GCS ----
GCS_DEST="${GCS_DEST_BASE}/${TMA_NAME}"
echo "Uploading ${LOCAL_DEST} to ${GCS_DEST} ..." >> "$LOG_FILE"

gcloud storage cp -r "${LOCAL_DEST}" "${GCS_DEST_BASE}/" >> "$LOG_FILE" 2>&1
GCS_EXIT_CODE=$?

echo "----------------------------------------" >> "$LOG_FILE"

if [[ $GCS_EXIT_CODE -eq 0 ]]; then
  echo "SUCCESS: SFTP download and GCS upload both completed at $(date)" >> "$LOG_FILE"
  echo "SUCCESS"
  exit 0
else
  echo "ERROR: GCS upload failed (exit code ${GCS_EXIT_CODE}) at $(date)" >> "$LOG_FILE"
  echo "ERROR: SFTP download succeeded but GCS upload failed. See ${LOG_FILE} for details." >&2
  exit 1
fi