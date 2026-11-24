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

import { WhepSession } from './whep.js';

const TEXT = {
    en: {
        unmute: 'Tap to turn the sound on',
        fullscreen: 'Fullscreen (F)',
        channels: 'Channels (P)',
        details: 'Signal details (I)',
        quality: ['No signal', 'Poor signal', 'Fair signal', 'Good signal',
                  'Excellent signal'],
        errors: 'errors',
        idle: 'idle',
        tuning: 'tuning',
        nolock: 'no signal',
        error: 'error',
        noSignal: 'No signal',
        noChannels: 'No channels configured',
        retry: 'Connection lost, retrying…',
        offline: 'Server unreachable, retrying…',
    },
    es: {
        unmute: 'Toque para activar el sonido',
        fullscreen: 'Pantalla completa (F)',
        channels: 'Canales (P)',
        details: 'Detalles de la señal (I)',
        quality: ['Sin señal', 'Señal mala', 'Señal regular', 'Señal buena',
                  'Señal excelente'],
        errors: 'errores',
        idle: 'libre',
        tuning: 'sintonizando',
        nolock: 'sin señal',
        error: 'error',
        noSignal: 'Sin señal',
        noChannels: 'No hay canales configurados',
        retry: 'Se perdió la conexión, reintentando…',
        offline: 'No se alcanza el servidor, reintentando…',
    },
};
const LANG = navigator.language.toLowerCase().startsWith('es') ? 'es' : 'en';
const T = TEXT[LANG];

const STORE = 'sintonia.';
const TUNE_DELAY_MS = 250;   // let quick Up/Down presses settle
const BANNER_MS = 3500;
const IDLE_MS = 2500;
const RETRY_MS = 2000;
const SWIPE_PX = 60;

const $ = (id) => document.getElementById(id);
const player = $('player');
const video = $('video');

const state = {
    channels: [],
    index: -1,
    session: null,
    generation: 0,
    server: { tuners: [], streams: [] },
};

function load(key, fallback) {
    try {
        const value = localStorage.getItem(STORE + key);
        return value === null ? fallback : JSON.parse(value);
    } catch {
        return fallback;
    }
}

function save(key, value) {
    try {
        localStorage.setItem(STORE + key, JSON.stringify(value));
    } catch {
        // Private mode or storage disabled: nothing to remember then.
    }
}

/* Text ----------------------------------------------------------------- */

function translate() {
    document.documentElement.lang = LANG;
    for (const node of document.querySelectorAll('[data-i18n]')) {
        node.textContent = T[node.dataset.i18n];
    }
    for (const node of document.querySelectorAll('[data-i18n-label]')) {
        const text = T[node.dataset.i18nLabel];
        node.setAttribute('aria-label', text);
        node.title = text;
    }
}

/* Banner, messages ------------------------------------------------------ */

let bannerTimer = null;

function showBanner(channel, busy) {
    const banner = $('banner');
    $('banner-number').textContent = channel.number;
    $('banner-name').textContent = channel.name;
    $('spinner').hidden = !busy;
    banner.classList.remove('fading');
    banner.hidden = false;
    clearTimeout(bannerTimer);
    if (!busy) {
        bannerTimer = setTimeout(() => {
            banner.classList.add('fading');
            bannerTimer = setTimeout(() => { banner.hidden = true; }, 600);
        }, BANNER_MS);
    }
}

function showMessage(text) {
    $('message').textContent = text || '';
    $('message').hidden = !text;
}

let toastTimer = null;

function showToast(text) {
    const toast = $('toast');
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 4000);
}

/* Channel list ---------------------------------------------------------- */

function renderChannels() {
    const list = $('channels');
    list.replaceChildren();
    state.channels.forEach((channel, index) => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.index = index;
        const number = document.createElement('span');
        number.className = 'number';
        number.textContent = channel.number;
        const name = document.createElement('span');
        name.className = 'name';
        name.textContent = channel.name;
        const live = document.createElement('span');
        live.className = 'live';
        button.append(number, name, live);
        button.addEventListener('click', () => select(index));
        item.append(button);
        list.append(item);
    });
}

function markCurrent() {
    for (const button of $('channels').querySelectorAll('button')) {
        const current = Number(button.dataset.index) === state.index;
        button.setAttribute('aria-current', String(current));
        if (current) {
            button.scrollIntoView({ block: 'nearest' });
        }
    }
}

function renderServerState() {
    const { tuners, streams } = state.server;
    const byNumber = new Map(streams.map((s) => [s.number, s]));
    for (const button of $('channels').querySelectorAll('button')) {
        const channel = state.channels[Number(button.dataset.index)];
        const stream = byNumber.get(channel.number);
        const live = button.querySelector('.live');
        live.classList.toggle('on', Boolean(stream && !stream.prewarm));
        live.classList.toggle('warm', Boolean(stream && stream.prewarm));
    }

    renderSignal(tuners, byNumber);

    // "No signal" when the current channel's tuner lost its lock.
    const current = state.channels[state.index];
    const stream = current && byNumber.get(current.number);
    const tuner = stream && tuners.find((t) => t.adapter === stream.tuner);
    if (tuner && tuner.state === 'nolock') {
        showMessage(T.noSignal);
    } else if ($('message').textContent === T.noSignal) {
        showMessage('');
    }
}

/* Signal ---------------------------------------------------------------- */

// Signal quality of a tuner, 0 (no signal) to 4, or null when unknown
// (idle, still tuning). Corrupt packets matter most: they are what makes
// the picture break up; a low C/N warns of a signal close to failing.
function quality(tuner) {
    if (!tuner || tuner.state === 'idle' || tuner.state === 'tuning') {
        return null;
    }
    if (tuner.state !== 'locked') {
        return 0;
    }
    const { errors, cnr_db: cnr } = tuner;
    if (errors !== null && errors >= 0.05) {
        return 1;
    }
    if ((errors !== null && errors > 0.001) || (cnr !== null && cnr < 10)) {
        return 2;
    }
    return cnr !== null && cnr < 18 ? 3 : 4;
}

function percent(share) {
    return `${(share * 100).toFixed(share && share < 0.1 ? 1 : 0)}%`;
}

function describe(tuner) {
    if (tuner.state !== 'locked') {
        return T[tuner.state] || tuner.state;
    }
    const parts = [];
    if (tuner.signal_dbm !== null) {
        parts.push(`${tuner.signal_dbm} dBm`);
    }
    if (tuner.cnr_db !== null) {
        parts.push(`C/N ${tuner.cnr_db} dB`);
    }
    if (tuner.errors !== null) {
        parts.push(`${percent(tuner.errors)} ${T.errors}`);
    }
    return parts.join(' · ');
}

function renderSignal(tuners, byNumber) {
    const current = state.channels[state.index];
    const stream = current && byNumber.get(current.number);
    const tuner = stream && tuners.find((t) => t.adapter === stream.tuner);
    const level = quality(tuner);
    $('bars').dataset.level = level === null ? '' : level;
    $('signal-text').textContent = level === null
        ? (tuner ? T[tuner.state] : '') : T.quality[level];

    // Details: every tuner, what it carries and how well it receives.
    $('tuners').replaceChildren(...tuners.map((t) => {
        const row = document.createElement('li');
        row.className = t.state;
        const name = document.createElement('b');
        name.textContent = `T${t.adapter}`;
        const channels = document.createElement('span');
        channels.className = 'carries';
        for (const number of t.channels) {
            const item = document.createElement('span');
            item.textContent = number;
            if (current && number === current.number) {
                item.className = 'watching';
            } else if (byNumber.get(number)?.prewarm) {
                item.className = 'warm';
            }
            channels.append(item);
        }
        const stats = document.createElement('span');
        stats.className = 'stats';
        stats.textContent = describe(t);
        row.append(name, channels, stats);
        return row;
    }));
}

function toggleDetails(open) {
    const details = $('tuners');
    details.hidden = open === undefined ? !details.hidden : !open;
    $('signal').setAttribute('aria-expanded', String(!details.hidden));
    save('details', !details.hidden);
}

/* Tuning ---------------------------------------------------------------- */

let tuneTimer = null;

function select(index) {
    if (!state.channels.length) {
        return;
    }
    const count = state.channels.length;
    state.index = ((index % count) + count) % count;
    const channel = state.channels[state.index];
    save('channel', channel.number);
    markCurrent();
    renderServerState();
    showBanner(channel, true);
    showMessage('');
    clearTimeout(tuneTimer);
    tuneTimer = setTimeout(() => tune(channel), TUNE_DELAY_MS);
}

async function tune(channel) {
    const generation = ++state.generation;
    if (state.session) {
        state.session.stop();
        state.session = null;
    }
    try {
        const response = await fetch('/api/watch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: channel.number }),
        });
        const body = await response.json();
        if (generation !== state.generation) {
            return;
        }
        if (!response.ok) {
            throw new Error(body.error || response.statusText);
        }
        const session = new WhepSession(video);
        session.onfailure = () => {
            if (generation === state.generation) {
                showMessage(T.retry);
                setTimeout(() => select(state.index), RETRY_MS);
            }
        };
        state.session = session;
        await session.play(body.whep);
        if (generation !== state.generation) {
            session.stop();
        }
    } catch (error) {
        if (generation === state.generation) {
            $('spinner').hidden = true;
            // Do not leave the previous channel's picture under the error.
            video.srcObject = null;
            showMessage(String(error.message || error));
        }
    }
}

video.addEventListener('playing', () => {
    const channel = state.channels[state.index];
    if (channel) {
        showBanner(channel, false);
        showMessage('');
    }
    requestWakeLock();
});

/* Sound ----------------------------------------------------------------- */

function unmute() {
    if (video.muted) {
        video.muted = false;
        video.play().catch(() => {});
    }
    $('unmute').hidden = true;
}

video.addEventListener('volumechange', () => {
    $('unmute').hidden = !video.muted;
});

/* Fullscreen, pane, idle controls --------------------------------------- */

async function toggleFullscreen() {
    if (document.fullscreenElement) {
        await document.exitFullscreen();
        return;
    }
    await player.requestFullscreen({ navigationUI: 'hide' });
    // Phones: watch in landscape. Fails harmlessly elsewhere.
    screen.orientation?.lock?.('landscape').catch(() => {});
}

function togglePane() {
    const collapsed = document.body.classList.toggle('pane-collapsed');
    save('paneCollapsed', collapsed);
}

let idleTimer = null;

function wake() {
    player.classList.remove('idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => player.classList.add('idle'), IDLE_MS);
}

/* Screen wake lock: keep the phone's screen on while watching. */
let wakeLock = null;

async function requestWakeLock() {
    if (!('wakeLock' in navigator) || wakeLock
            || document.visibilityState !== 'visible') {
        return;
    }
    try {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
    } catch {
        wakeLock = null;
    }
}

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !video.paused) {
        requestWakeLock();
    }
});

/* Input ----------------------------------------------------------------- */

document.addEventListener('keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) {
        return;
    }
    let handled = true;
    switch (event.key) {
        case 'ArrowUp':
            select(state.index - 1);
            break;
        case 'ArrowDown':
            select(state.index + 1);
            break;
        case 'f':
        case 'F':
            toggleFullscreen();
            break;
        case 'p':
        case 'P':
            togglePane();
            break;
        case 'm':
        case 'M':
            video.muted = !video.muted;
            break;
        case 'i':
        case 'I':
            toggleDetails();
            break;
        default:
            handled = false;
    }
    if (handled) {
        event.preventDefault();
        unmute();
        wake();
    }
});

let touchStart = null;

player.addEventListener('touchstart', (event) => {
    const touch = event.changedTouches[0];
    touchStart = { x: touch.clientX, y: touch.clientY };
}, { passive: true });

player.addEventListener('touchend', (event) => {
    if (!touchStart) {
        return;
    }
    const touch = event.changedTouches[0];
    const dx = touch.clientX - touchStart.x;
    const dy = touch.clientY - touchStart.y;
    touchStart = null;
    if (Math.abs(dy) > SWIPE_PX && Math.abs(dy) > 2 * Math.abs(dx)) {
        // Swipe up for the next channel, like scrolling a list.
        select(state.index + (dy < 0 ? 1 : -1));
    }
    unmute();
    wake();
}, { passive: true });

player.addEventListener('mousemove', wake);
player.addEventListener('click', (event) => {
    if (event.target === video) {
        unmute();
    }
    wake();
});
player.addEventListener('dblclick', (event) => {
    if (event.target === video) {
        toggleFullscreen();
    }
});
$('unmute').addEventListener('click', unmute);
$('fullscreen').addEventListener('click', toggleFullscreen);
$('pane-toggle').addEventListener('click', togglePane);
$('signal').addEventListener('click', () => toggleDetails());

/* Server events --------------------------------------------------------- */

function connectEvents() {
    const events = new EventSource('/api/events');
    events.addEventListener('state', (event) => {
        state.server = JSON.parse(event.data);
        renderServerState();
        if ($('message').textContent === T.offline) {
            showMessage('');
        }
    });
    events.addEventListener('notice', (event) => {
        const notice = JSON.parse(event.data);
        const current = state.channels[state.index];
        if (current && notice.channel === current.number) {
            showToast(notice.message);
            // Our tuner was taken: get the channel back on another one.
            setTimeout(() => select(state.index), 500);
        }
    });
    events.addEventListener('error', () => {
        if (events.readyState === EventSource.CLOSED) {
            showMessage(T.offline);
            setTimeout(connectEvents, RETRY_MS);
        }
    });
}

/* Start ----------------------------------------------------------------- */

async function start() {
    translate();
    try {
        // The tuner selector is gone: forget a tuner chosen with it.
        localStorage.removeItem(`${STORE}tuner`);
    } catch {
        // Storage disabled.
    }
    if (load('paneCollapsed', false)) {
        document.body.classList.add('pane-collapsed');
    }

    let config;
    try {
        config = await (await fetch('/api/config')).json();
    } catch {
        showMessage(T.offline);
        setTimeout(start, RETRY_MS);
        return;
    }
    state.channels = config.channels;
    renderChannels();

    toggleDetails(load('details', false));

    connectEvents();
    wake();
    $('unmute').hidden = !video.muted;

    if (!state.channels.length) {
        showMessage(T.noChannels);
        return;
    }
    const last = load('channel', null);
    const index = state.channels.findIndex((c) => c.number === last);
    select(index >= 0 ? index : 0);
}

start();
