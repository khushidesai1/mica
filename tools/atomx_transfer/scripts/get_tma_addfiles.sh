#!/usr/bin/env bash
# Mica adaptation: worker download-only mode bypasses upstream discovery/upload.
# Upstream pinned at 5d864b6cd13f1873364aa70b9ac1939f4c656616.
if [[ "${1:-}" == "--download-only" ]]; then
  exec bash "$(dirname "$0")/download_manifest.sh" "$2"
fi
#
# get_tma_addfiles.sh
# Downloads the REMAINING decoded files for a TMA (everything in the
# DecodedFiles/<tma>/<run> folder except Morphology2D and the RunSummary
# dictionary file, which are already handled by get_tma.sh) and
# uploads them to GCS under raw_outputs/additional_files/<tma_name>.
#
set -uo pipefail

# ---------------- CONFIG (from the environment) ----------------
SFTP_HOST="${ATOMX_SFTP_HOST:-na.export.atomx.nanostring.com}"
SFTP_USER="${ATOMX_SFTP_USER:-}"          # your AtoMx export login
SFTP_PORT="${ATOMX_SFTP_PORT:-22}"
LOG_DIR="${LOG_DIR:-./logs}"
RUN_SUMMARY_FILE="Morphology_ChannelID_Dictionary.txt"   # already grabbed by get_tma.sh, excluded here
GCS_DEST_BASE="${GCS_ADDITIONAL_DEST_BASE:-}"  # e.g. gs://<bucket>/raw_outputs/additional_files
# -------------------------------------------------------
#
# Usage:
#   export SFTP_PASSWORD='yourpassword'   # or set ATOMX_PASSWORD_FILE and use run_batch.sh
#   ./get_tma_addfiles.sh <root_dir> <tma_name> [local_dest] [gcs_dest_name]
#   ./get_tma_addfiles.sh /MyExport_17_08_2026_10_30_47_577 tma36
#
# <root_dir>    the changing top-level remote folder, e.g.
#               /MyExport_17_08_2026_10_30_47_577
# <tma_name>    e.g. tma36
# [local_dest]     optional, defaults to <tma_name>_additional in the current directory
# [gcs_dest_name]  optional, defaults to <tma_name>
#
# Requires sshpass (apt install sshpass / brew install hudochenkov/sshpass/sshpass)

if [[ -z "${1:-}" || -z "${2:-}" ]]; then
  echo "Usage: $0 <root_dir> <tma_name> [local_dest] [gcs_dest_name]" >&2
  echo "Example: $0 /MyExport_17_08_2026_10_30_47_577 tma36" >&2
  exit 1
fi
ROOT_DIR="$1"
TMA_NAME="$2"
LOCAL_DEST="${3:-${TMA_NAME}_additional}"
GCS_DEST_NAME="${4:-${TMA_NAME}}"

# Settings come from the environment (see examples/env.example); nothing site-specific
# is hardcoded. Checked here, after the usage message.
for var in ATOMX_SFTP_USER GCS_ADDITIONAL_DEST_BASE; do
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
LOG_FILE="${LOG_DIR}/sftp_additional_${TMA_NAME}_${TIMESTAMP}.log"
mkdir -p "$LOG_DIR" "$LOCAL_DEST"

SFTP_OPTS=(-o "Port=${SFTP_PORT}" -o "StrictHostKeyChecking=accept-new")
DECODED_BASE="${ROOT_DIR}/DecodedFiles/${TMA_NAME}"

{
  echo "=== Starting additional-files transfer at $(date) ==="
  echo "Root dir: ${ROOT_DIR}"
  echo "TMA name: ${TMA_NAME}"
  echo "Local destination: ${LOCAL_DEST}"
  echo "GCS destination name: ${GCS_DEST_NAME}"
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

# Match the timestamp pattern at the end of a line (server returns full
# paths), which naturally excludes non-run folders like "Logs". If more
# than one run folder matches, the alphabetically-last one is picked.
RUN_SUBDIR=$(echo "$LIST_OUTPUT" | grep -oE '[0-9]{8}_[0-9]{6}_S[0-9]+$' | sort | tail -n 1)

if [[ -z "$RUN_SUBDIR" ]]; then
  echo "----------------------------------------" >> "$LOG_FILE"
  echo "ERROR: No run subfolder found under ${DECODED_BASE} at $(date)" >> "$LOG_FILE"
  echo "ERROR: No run subfolder found under ${DECODED_BASE}. See ${LOG_FILE}." >&2
  exit 1
fi

echo "Found run subfolder: ${RUN_SUBDIR}" >> "$LOG_FILE"
echo "----------------------------------------" >> "$LOG_FILE"

RUN_DIR="${DECODED_BASE}/${RUN_SUBDIR}"
echo "Run dir: ${RUN_DIR}" >> "$LOG_FILE"
echo "----------------------------------------" >> "$LOG_FILE"

# ---- Step 2: download the ENTIRE run dir's contents directly into LOCAL_DEST ----
# The trailing /* flattens it: contents of RUN_DIR land straight in
# LOCAL_DEST rather than nested under an extra RUN_SUBDIR-named folder.
sshpass -e sftp "${SFTP_OPTS[@]}" "${SFTP_USER}@${SFTP_HOST}" >> "$LOG_FILE" 2>&1 <<EOF
lcd ${LOCAL_DEST}
get -r ${RUN_DIR}/*
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

# ---- Step 3: strip out files already handled by get_tma.sh ----
if [[ -d "${LOCAL_DEST}/CellStatsDir/Morphology2D" ]]; then
  echo "Removing already-fetched CellStatsDir/Morphology2D from local copy" >> "$LOG_FILE"
  rm -rf "${LOCAL_DEST}/CellStatsDir/Morphology2D"
fi
if [[ -f "${LOCAL_DEST}/RunSummary/${RUN_SUMMARY_FILE}" ]]; then
  echo "Removing already-fetched RunSummary/${RUN_SUMMARY_FILE} from local copy" >> "$LOG_FILE"
  rm -f "${LOCAL_DEST}/RunSummary/${RUN_SUMMARY_FILE}"
fi

echo "----------------------------------------" >> "$LOG_FILE"

# ---- Step 4: upload the remaining files to GCS ----
GCS_DEST="${GCS_DEST_BASE}/${GCS_DEST_NAME}"
echo "Uploading ${LOCAL_DEST} to ${GCS_DEST} ..." >> "$LOG_FILE"

gcloud storage cp -r "${LOCAL_DEST}"/* "${GCS_DEST}/" >> "$LOG_FILE" 2>&1
GCS_EXIT_CODE=$?

echo "----------------------------------------" >> "$LOG_FILE"

if [[ $GCS_EXIT_CODE -eq 0 ]]; then
  echo "SUCCESS: Additional-files download and GCS upload both completed at $(date)" >> "$LOG_FILE"
  echo "SUCCESS"
  exit 0
else
  echo "ERROR: GCS upload failed (exit code ${GCS_EXIT_CODE}) at $(date)" >> "$LOG_FILE"
  echo "ERROR: SFTP download succeeded but GCS upload failed. See ${LOG_FILE} for details." >&2
  exit 1
fi
