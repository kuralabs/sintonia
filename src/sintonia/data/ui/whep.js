/*
 * Copyright (C) 2026 KuraLabs S.R.L.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * Minimal WHEP (RFC 9725) player: a single POST with the SDP offer once ICE
 * gathering is done (no trickle; host candidates are enough on a LAN).
 */

function iceGathered(pc, timeoutMs) {
    if (pc.iceGatheringState === 'complete') {
        return Promise.resolve();
    }
    return new Promise((resolve) => {
        const done = () => {
            pc.removeEventListener('icegatheringstatechange', check);
            resolve();
        };
        const check = () => {
            if (pc.iceGatheringState === 'complete') {
                done();
            }
        };
        pc.addEventListener('icegatheringstatechange', check);
        setTimeout(done, timeoutMs);
    });
}

export class WhepSession {
    constructor(video) {
        this.video = video;
        this.pc = null;
        this.resource = null;
        this.onfailure = null;
    }

    async play(url) {
        const pc = new RTCPeerConnection();
        this.pc = pc;
        const stream = new MediaStream();
        pc.addTransceiver('video', { direction: 'recvonly' });
        pc.addTransceiver('audio', { direction: 'recvonly' });
        pc.addEventListener('track', (event) => {
            stream.addTrack(event.track);
            if (this.video.srcObject !== stream) {
                this.video.srcObject = stream;
            }
        });
        pc.addEventListener('connectionstatechange', () => {
            if (pc === this.pc && pc.connectionState === 'failed'
                    && this.onfailure) {
                this.onfailure();
            }
        });

        await pc.setLocalDescription(await pc.createOffer());
        await iceGathered(pc, 1000);
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/sdp' },
            body: pc.localDescription.sdp,
        });
        if (response.status !== 201) {
            throw new Error(`WHEP ${response.status}: `
                            + await response.text());
        }
        this.resource = response.headers.get('Location');
        await pc.setRemoteDescription({
            type: 'answer', sdp: await response.text(),
        });
    }

    stop() {
        // Tell the server first, so the viewer count drops right away, and
        // close the connection once it has answered: closing first makes
        // the session vanish and the DELETE fail with 404.
        const pc = this.pc;
        const resource = this.resource;
        this.pc = null;
        this.resource = null;
        const close = () => pc && pc.close();
        if (resource) {
            fetch(resource, { method: 'DELETE' }).catch(() => {})
                .finally(close);
        } else {
            close();
        }
    }
}
