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

FROM ubuntu:26.04

ARG MEDIAMTX_VERSION=v1.21.3
ARG MEDIAMTX_SHA256=0a2ea7eba96023631efa1bea7804823a9145b043b6646a8a05798d4ac24ed635

ENV DEBIAN_FRONTEND=noninteractive

# jellyfin-ffmpeg8 from Jellyfin's repository: ffmpeg 8.1+ (needed for
# ISDB-Tb audio) with Jellyfin's hardware transcoding work. Only ffmpeg is
# installed, not Jellyfin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg python3 python3-venv tini \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg \
    && . /etc/os-release \
    && printf '%s\n' \
        'Types: deb' \
        'URIs: https://repo.jellyfin.org/ubuntu' \
        "Suites: ${VERSION_CODENAME}" \
        'Components: main' \
        'Architectures: amd64' \
        'Signed-By: /etc/apt/keyrings/jellyfin.gpg' \
        > /etc/apt/sources.list.d/jellyfin.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends jellyfin-ffmpeg8 \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
    && echo "${MEDIAMTX_SHA256}  /tmp/mediamtx.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx \
    && rm /tmp/mediamtx.tar.gz \
    && mediamtx --version

COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN python3 -m venv /opt/sintonia \
    && /opt/sintonia/bin/pip install --no-cache-dir /src \
    && rm -rf /src

RUN useradd --system --uid 10001 --home-dir /nonexistent \
        --shell /usr/sbin/nologin sintonia

ENV PATH=/opt/sintonia/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SINTONIA_CONFIG=/config/config.toml \
    NVIDIA_DRIVER_CAPABILITIES=video,compute,utility

USER sintonia
EXPOSE 8080/tcp 8189/udp

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
    CMD ["sintonia", "health"]

ENTRYPOINT ["tini", "--", "sintonia"]
CMD ["serve"]

LABEL org.opencontainers.image.title="sintonia" \
      org.opencontainers.image.description="Simple web app for live over-the-air TV" \
      org.opencontainers.image.source="https://github.com/kuralabs/sintonia" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="KuraLabs S.R.L."
