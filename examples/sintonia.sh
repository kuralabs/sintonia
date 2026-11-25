#!/usr/bin/env bash
#
# Copyright (C) 2026 KuraLabs S.R.L.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Deploy Sintonía with Docker, keeping config.toml next to this script.
#
#   ./sintonia.sh               Pull the image and (re)start the server.
#   ./sintonia.sh scan [ARGS]   Scan for channels into config.scan.toml,
#                               pausing the server while the tuners are in
#                               use. ARGS go to `sintonia scan`, such as
#                               --channels 14-51.
#
# Settings, from the environment:
#
#   SINTONIA_NAME     Container name (sintonia).
#   SINTONIA_IMAGE    Image (ghcr.io/kuralabs/sintonia:latest).
#   SINTONIA_CONFIG   Configuration file (config.toml next to this script).

set -o errexit
set -o nounset
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="${SINTONIA_NAME:-sintonia}"
IMAGE="${SINTONIA_IMAGE:-ghcr.io/kuralabs/sintonia:latest}"
CONFIG="${SINTONIA_CONFIG:-${HERE}/config.toml}"
SCAN="$(dirname "${CONFIG}")/config.scan.toml"

# Seconds to wait for the server to report healthy.
HEALTH_TIMEOUT=90

VIDEO_GID="$(getent group video | cut -d: -f3)"

die() {
    echo "ERROR: $*" >&2
    exit 1
}

running() {
    [[ "$(docker inspect --format '{{.State.Running}}' "${NAME}" \
        2>/dev/null)" == 'true' ]]
}

check_config() {
    [[ -f "${CONFIG}" ]] || die "No configuration at ${CONFIG}." \
        "Create one with: $0 scan"

    # The container runs as an unprivileged user: the file must be
    # readable by others.
    local mode
    mode="$(stat --format '%A' "${CONFIG}")"
    [[ "${mode:7:1}" == 'r' ]] || die "${CONFIG} is not readable by the" \
        "container; run: chmod o+r ${CONFIG}"
}

wait_healthy() {
    local waited=0

    echo -n 'Waiting for the server '
    until docker exec "${NAME}" sintonia health >/dev/null 2>&1; do
        if ! running || ((waited >= HEALTH_TIMEOUT)); then
            echo ' failed.'
            echo
            docker logs --tail 50 "${NAME}"
            echo
            die "The server did not start. Check the set-up with:" \
                "docker run --rm --gpus all --device /dev/dvb" \
                "--group-add ${VIDEO_GID}" \
                "-v ${CONFIG}:/config/config.toml:ro ${IMAGE} check"
        fi
        echo -n '.'
        sleep 2
        ((waited += 2))
    done
    echo ' ready.'
}

deploy() {
    check_config

    # Pull first, so the server is down only for the restart.
    echo "Pulling ${IMAGE} ..."
    docker pull "${IMAGE}"

    echo "Removing the old container ..."
    docker rm --force "${NAME}" >/dev/null 2>&1 || true

    echo "Starting ${NAME} ..."
    docker run \
        --detach \
        --name "${NAME}" \
        --restart unless-stopped \
        --gpus all \
        --device /dev/dvb \
        --group-add "${VIDEO_GID}" \
        --network host \
        --volume "${CONFIG}:/config/config.toml:ro" \
        "${IMAGE}" >/dev/null

    wait_healthy
    echo
    docker logs "${NAME}" 2>&1 | tail --lines 20
}

scan() {
    local paused=false
    local output
    output="$(mktemp "${SCAN}.XXXXXX")"

    # The scan needs every tuner: pause the server, and start it again
    # however the scan ends.
    if running; then
        echo "Stopping ${NAME} while scanning ..."
        docker stop "${NAME}" >/dev/null
        paused=true
    fi
    # shellcheck disable=SC2064
    trap "rm -f '${output}'; ${paused} && docker start '${NAME}' >/dev/null" \
        EXIT

    docker run --rm \
        --device /dev/dvb \
        --group-add "${VIDEO_GID}" \
        "${IMAGE}" scan "$@" > "${output}"

    mv "${output}" "${SCAN}"
    chmod 644 "${SCAN}"
    echo
    echo "Channels written to ${SCAN}."
    echo "Review them, merge them into ${CONFIG}, and run $0 to apply."

    if ${paused}; then
        echo "Starting ${NAME} again ..."
    fi
}

case "${1:-deploy}" in
    deploy)
        deploy
        ;;
    scan)
        shift
        scan "$@"
        ;;
    -h | --help | help)
        sed --quiet '17,29s/^# \{0,1\}//p' "$0"
        ;;
    *)
        die "Unknown command: $1 (try $0 --help)"
        ;;
esac
