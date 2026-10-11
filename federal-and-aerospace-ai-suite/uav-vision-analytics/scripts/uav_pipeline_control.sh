#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Manually start/stop/check the uav_object_detection_<device> (and
# _caption) pipelines on the DL Streamer Pipeline Server fronted by nginx
# in the pymavlink stack (docker-compose-pymavlink.yml), without needing to
# arm the drone via MAVLink. Useful for ad-hoc testing of 'make pymav-up'
# stacks. The MAVLink-driven ARM/DISARM flow (scripts/mavlink_pipeline_manager.py,
# used by 'make start-rtsp') remains the normal way to run this app end to end.
#
# Each started pipeline's instance_id is cached in a per-device/variant state
# file (.uav_pipeline_<device>[_caption]_instance_id) so 'stop'/'status' can
# find it without the caller tracking it manually.
#
# Usage:
#   ./scripts/uav_pipeline_control.sh start  [cpu|gpu|npu|all] [--caption]
#   ./scripts/uav_pipeline_control.sh stop   [cpu|gpu|npu|all] [--caption]
#   ./scripts/uav_pipeline_control.sh status [cpu|gpu|npu|all] [--caption]
#
# --caption starts the uav_object_detection_<device>_caption variant (RTSP +
# telemetry overlay, same as the base pipeline, plus VLM scene captioning
# published over MQTT on 'uav/caption/<device>'). Requires 'make vlm-model'
# to have been run first (see README.md).

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
fi

HOST_IP="${HOST_IP:-127.0.0.1}"
NPU_DEVICE="${NPU_DEVICE:-/dev/null}"
MODEL_PATH="/home/pipeline-server/resources/models/yolo11s/yolo11s_openvino_model/yolo11s.xml"
ALL_DEVICES=(cpu gpu npu)

log()  { echo "[INFO ] $*"; }
err()  { echo "[ERROR] $*" >&2; }

api() {
  curl -sk --noproxy '*' "$@"
}

state_file() {
  local device="$1" caption="$2"
  if [[ "${caption}" == "true" ]]; then
    echo "${REPO_ROOT}/.uav_pipeline_${device}_caption_instance_id"
  else
    echo "${REPO_ROOT}/.uav_pipeline_${device}_instance_id"
  fi
}

pipeline_name() {
  local device="$1" caption="$2"
  if [[ "${caption}" == "true" ]]; then
    echo "uav_object_detection_${device}_caption"
  else
    echo "uav_object_detection_${device}"
  fi
}

frame_path() {
  # Same RTSP path regardless of --caption, so the URL a viewer connects to
  # never changes whether captioning is on or off (matches
  # mavlink_pipeline_manager.py's RTSP_CAPTION_PIPELINES).
  local device="$1"
  echo "uav-mavlink-${device}"
}

start_one() {
  local device="$1" caption="$2"
  local name path sfile payload response instance_id

  if [[ "${device}" == "npu" && "${NPU_DEVICE}" == "/dev/null" ]]; then
    err "Skipping NPU: NPU_DEVICE is not set in .env (no NPU device available)."
    return 0
  fi

  name="$(pipeline_name "${device}" "${caption}")"
  path="$(frame_path "${device}")"
  sfile="$(state_file "${device}" "${caption}")"

  if [[ -f "${sfile}" ]]; then
    err "Pipeline '${name}' already tracked (instance $(cat "${sfile}")). Run 'stop' first."
    return 1
  fi

  # Base and caption variants for the same device share the same RTSP path
  # (so the URL a viewer connects to never changes) — only one of the two
  # can publish to it at a time, so refuse to start one while the other is
  # already tracked for this device.
  local other_caption other_sfile other_name
  if [[ "${caption}" == "true" ]]; then other_caption="false"; else other_caption="true"; fi
  other_sfile="$(state_file "${device}" "${other_caption}")"
  if [[ -f "${other_sfile}" ]]; then
    other_name="$(pipeline_name "${device}" "${other_caption}")"
    err "'${other_name}' is already running and publishes to the same RTSP path" \
        "(${path}) as '${name}'. Run 'stop ${device}$([ "${other_caption}" = "true" ] && echo " --caption")' first."
    return 1
  fi

  log "Starting ${name} on https://${HOST_IP} (RTSP path: ${path})"

  if [[ "${caption}" == "true" ]]; then
    # NOTE: model/device/prompt for the caption branch are hardcoded inline
    # in the *_caption pipeline strings (config-pymavlink.json) — do not
    # add 'detection-properties' or 'destination.metadata' here; both are
    # known to deadlock gvadetect/gvagenai when wired through the tee used
    # by these pipelines.
    #
    # 'destination.frame' is a list (not a single dict) so the pipeline
    # server attaches both an RTSP and a WebRTC AppDestination to the same
    # appsink -- matches mavlink_pipeline_manager.py's
    # _build_rtsp_caption_payload(). Detection-box overlay is NOT drawn
    # in-pipeline (the *_caption GStreamer string only has a gvapython
    # telemetry-text overlay before gvametaconvert/gvametapublish) -- it is
    # rendered per-destination by the pipeline server's own AppDestination,
    # so 'overlay' must be left at its default (on) here too, matching the
    # RTSP destination, or the WebRTC viewer shows no bounding boxes.
    payload="$(cat <<EOF
{
  "destination": {
    "frame": [
      {"type": "rtsp", "path": "${path}"},
      {"type": "webrtc", "peer-id": "${path}"}
    ]
  }
}
EOF
)"
  else
    payload="$(cat <<EOF
{
  "destination": {
    "metadata": {"type": "file", "path": "/tmp/results_${device}.jsonl", "format": "json-lines"},
    "frame": {"type": "rtsp", "path": "${path}"}
  },
  "parameters": {
    "detection-properties": {
      "model": "${MODEL_PATH}",
      "device": "${device^^}"
    }
  }
}
EOF
)"
  fi

  response="$(api -X POST "https://${HOST_IP}/pipelines/user_defined_pipelines/${name}" \
    -H 'Content-Type: application/json' \
    -d "${payload}")"

  instance_id="$(echo "${response}" | tr -d '"')"
  if [[ -z "${instance_id}" || "${instance_id}" == *error* || "${instance_id}" == *detail* ]]; then
    err "Failed to start '${name}': ${response}"
    return 1
  fi

  echo "${instance_id}" > "${sfile}"
  log "Started '${name}'. Instance ID: ${instance_id}"
  log "RTSP output: rtsp://${HOST_IP}:8555/${path}"
  if [[ "${caption}" == "true" ]]; then
    log "WebRTC (WHEP) viewer: https://${HOST_IP}/webrtc/${path}/"
    log "MQTT captions: topic 'uav/caption/${device}' on broker:1883"
  fi
}

stop_one() {
  local device="$1" caption="$2"
  local name sfile instance_id

  name="$(pipeline_name "${device}" "${caption}")"
  sfile="$(state_file "${device}" "${caption}")"

  if [[ ! -f "${sfile}" ]]; then
    log "No tracked instance for '${name}' (${sfile} missing) — skipping."
    return 0
  fi

  instance_id="$(cat "${sfile}")"
  log "Stopping '${name}' (instance ${instance_id})"
  api -X DELETE "https://${HOST_IP}/pipelines/${instance_id}" > /dev/null
  rm -f "${sfile}"
  log "Stopped."
}

status_one() {
  local device="$1" caption="$2"
  local name sfile instance_id

  name="$(pipeline_name "${device}" "${caption}")"
  sfile="$(state_file "${device}" "${caption}")"

  if [[ ! -f "${sfile}" ]]; then
    log "'${name}': no tracked instance."
    return 0
  fi

  instance_id="$(cat "${sfile}")"
  log "'${name}' (instance ${instance_id}):"
  api "https://${HOST_IP}/pipelines/${instance_id}/status" | python3 -m json.tool 2>/dev/null \
    || echo "  (status unavailable — instance may no longer exist)"
}

resolve_devices() {
  local device="$1"
  case "${device}" in
    all) echo "${ALL_DEVICES[@]}" ;;
    cpu|gpu|npu) echo "${device}" ;;
    *)
      err "device must be one of: cpu, gpu, npu, all (got '${device}')"
      exit 1
      ;;
  esac
}

ACTION="${1:-}"
DEVICE_ARG="${2:-gpu}"
CAPTION="false"
for arg in "${@:3}"; do
  [[ "${arg}" == "--caption" ]] && CAPTION="true"
done
# Allow --caption as the 2nd positional too, e.g. 'start all --caption'.
[[ "${DEVICE_ARG}" == "--caption" ]] && { CAPTION="true"; DEVICE_ARG="all"; }

case "${ACTION}" in
  start|stop|status)
    for device in $(resolve_devices "${DEVICE_ARG}"); do
      "${ACTION}_one" "${device}" "${CAPTION}"
    done
    ;;
  *)
    echo "Usage: $0 {start|stop|status} [cpu|gpu|npu|all] [--caption]" >&2
    exit 1
    ;;
esac
