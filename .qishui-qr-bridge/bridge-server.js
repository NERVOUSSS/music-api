'use strict';

// Authentication and playback bridge. The Qishui playback resolver and audio
// decryptor are adapted from Wx2yZx/Mineradio (GPL-3.0-only).
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const path = require('path');
const { app } = require('electron');
const { TrackDecryptor } = require('./qishui-audio-decryptor/track-decryptor');
const qishuiApi = require('./qishui-api');

const ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'data');
const userDataDir = process.env.QISHUI_ELECTRON_USER_DATA || path.join(DATA_DIR, 'qishui-electron-user-data');
const configFile = process.env.QISHUI_QR_CONFIG_FILE || path.join(DATA_DIR, '.qishui-qr-login.json');
const port = Number(process.env.QISHUI_BRIDGE_PORT || 3211);
const bridgeToken = String(process.env.QISHUI_BRIDGE_TOKEN || '');
const pcContextFile = process.env.QISHUI_PC_CONTEXT_FILE || path.join(DATA_DIR, '.qishui-pc-context.json');
const pcVersionName = String(process.env.QISHUI_PC_VERSION_NAME || '3.8.0').trim();
const pcAppUa = String(process.env.QISHUI_PC_APP_UA || ('LunaPC/' + pcVersionName + '(467160162)')).trim();
const decryptor = new TrackDecryptor();
const audioCache = new Map();
const AUDIO_CACHE_MAX_BYTES = 96 * 1024 * 1024;
let audioCacheBytes = 0;

fs.mkdirSync(DATA_DIR, { recursive: true });
fs.mkdirSync(userDataDir, { recursive: true });
try { app.setPath('userData', userDataDir); } catch (_) {}
try { app.setPath('sessionData', path.join(userDataDir, 'session-data')); } catch (_) {}
try { app.disableHardwareAcceleration(); } catch (_) {}

process.env.QISHUI_QR_CONFIG_FILE = configFile;
const qrLogin = require('./qishui-qr-login');

function authorized(request) {
  return !bridgeToken || String(request.headers['x-qishui-bridge-token'] || '') === bridgeToken;
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    let raw = '';
    request.setEncoding('utf8');
    request.on('data', chunk => {
      raw += chunk;
      if (raw.length > 1024 * 1024) reject(new Error('request too large'));
    });
    request.on('end', () => {
      if (!raw.trim()) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (_) { reject(new Error('invalid JSON')); }
    });
    request.on('error', reject);
  });
}

function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
  });
  response.end(body);
}

function safeError(error) {
  const message = String(error && error.message || error || 'bridge error');
  return message.slice(0, 500);
}

function audioAuthFromUrl(audioUrl) {
  const text = String(audioUrl || '');
  const index = text.indexOf('#auth=');
  if (index < 0) return { cleanUrl: text, auth: '' };
  let auth = text.slice(index + 6);
  try { auth = decodeURIComponent(auth); } catch (_) {}
  return { cleanUrl: text.slice(0, index), auth };
}

function audioHeaders(audioUrl, range) {
  let context = {};
  try {
    if (fs.existsSync(pcContextFile)) {
      const parsed = JSON.parse(fs.readFileSync(pcContextFile, 'utf8').replace(/^\uFEFF/, ''));
      if (parsed && typeof parsed === 'object') context = parsed;
    }
  } catch (_) {}
  const headers = {
    'Accept': '*/*',
    'User-Agent': String(process.env.QISHUI_PC_APP_UA || context.QISHUI_PC_APP_UA || context.userAgent || pcAppUa).trim(),
    'Referer': 'https://www.qishui.com/',
  };
  const dynamicHeaders = [
    ['x-helios', 'QISHUI_X_HELIOS'],
    ['x-medusa', 'QISHUI_X_MEDUSA'],
    ['x-ss-stub', 'QISHUI_X_SS_STUB'],
  ];
  for (const [headerName, envName] of dynamicHeaders) {
    const value = String(process.env[envName] || context[envName] || context[headerName] || '').trim();
    if (value) headers[headerName] = value;
  }
  if (range) headers.Range = range;
  return headers;
}

function audioContentType(audioUrl, upstreamType) {
  let pathname = '';
  try { pathname = new URL(audioUrl).pathname.toLowerCase(); } catch (_) {}
  if (/\.flac$/.test(pathname)) return 'audio/flac';
  if (/\.mp3$/.test(pathname)) return 'audio/mpeg';
  if (/\.(m4a|mp4)$/.test(pathname)) return 'audio/mp4';
  if (/\.ogg$/.test(pathname)) return 'audio/ogg';
  return upstreamType || 'audio/mpeg';
}

function cacheKey(cleanUrl, auth) {
  return crypto.createHash('sha1').update(String(cleanUrl) + '\n' + String(auth)).digest('hex');
}

function rememberAudio(key, payload) {
  if (!payload || !Buffer.isBuffer(payload.buffer)) return;
  const previous = audioCache.get(key);
  if (previous) audioCacheBytes -= previous.buffer.length;
  audioCache.set(key, { ...payload, at: Date.now() });
  audioCacheBytes += payload.buffer.length;
  while (audioCacheBytes > AUDIO_CACHE_MAX_BYTES && audioCache.size > 1) {
    const oldest = [...audioCache.entries()].sort((a, b) => a[1].at - b[1].at)[0];
    if (!oldest) break;
    audioCache.delete(oldest[0]);
    audioCacheBytes -= oldest[1].buffer.length;
  }
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...(options || {}), signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function decryptedAudio(audioUrl) {
  const parsed = audioAuthFromUrl(audioUrl);
  if (!parsed.auth) return null;
  const key = cacheKey(parsed.cleanUrl, parsed.auth);
  const cached = audioCache.get(key);
  if (cached) {
    cached.at = Date.now();
    return cached;
  }
  const upstream = await fetchWithTimeout(parsed.cleanUrl, { headers: audioHeaders(parsed.cleanUrl, '') }, 20000);
  if (!upstream.ok) throw new Error(`Qishui encrypted audio fetch failed: HTTP ${upstream.status}`);
  const encryptedBuffer = Buffer.from(await upstream.arrayBuffer());
  const result = decryptor.decrypt({ encryptedBuffer, spadeA: parsed.auth });
  const payload = {
    buffer: result.buffer,
    contentType: result.extension === '.flac' ? 'audio/flac' : 'audio/mp4',
  };
  rememberAudio(key, payload);
  return payload;
}

function sendAudioBuffer(response, buffer, contentType, range) {
  const total = buffer.length;
  const match = /^bytes=(\d*)-(\d*)$/i.exec(String(range || ''));
  if (match) {
    let start = match[1] ? Number(match[1]) : 0;
    let end = match[2] ? Number(match[2]) : total - 1;
    if (!Number.isFinite(start) || start < 0) start = 0;
    if (!Number.isFinite(end) || end >= total) end = total - 1;
    if (start > end || start >= total) {
      response.writeHead(416, { 'Content-Range': `bytes */${total}` });
      response.end();
      return;
    }
    response.writeHead(206, {
      'Content-Type': contentType || 'audio/mp4',
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'no-store',
      'Content-Length': end - start + 1,
      'Content-Range': `bytes ${start}-${end}/${total}`,
    });
    response.end(buffer.subarray(start, end + 1));
    return;
  }
  response.writeHead(200, {
    'Content-Type': contentType || 'audio/mp4',
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'no-store',
    'Content-Length': total,
  });
  response.end(buffer);
}

async function proxyAudio(request, response, audioUrl) {
  if (!/^https?:\/\//i.test(audioUrl)) return send(response, 400, { ok: false, error: 'invalid audio url' });
  const range = String(request.headers.range || '');
  const decrypted = await decryptedAudio(audioUrl);
  if (decrypted && decrypted.buffer) {
    sendAudioBuffer(response, decrypted.buffer, decrypted.contentType, range);
    return;
  }
  const parsed = audioAuthFromUrl(audioUrl);
  const upstream = await fetchWithTimeout(parsed.cleanUrl, { headers: audioHeaders(parsed.cleanUrl, range) }, 20000);
  const headers = {
    'Content-Type': audioContentType(parsed.cleanUrl, upstream.headers.get('content-type')),
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'no-store',
  };
  for (const name of ['content-length', 'content-range']) {
    const value = upstream.headers.get(name);
    if (value) headers[name] = value;
  }
  response.writeHead(upstream.status, headers);
  if (!upstream.body) return response.end();
  const reader = upstream.body.getReader();
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      if (!response.write(chunk.value)) await new Promise(resolve => response.once('drain', resolve));
    }
    response.end();
  } finally {
    try { reader.releaseLock(); } catch (_) {}
  }
}

async function route(request, response) {
  if (!authorized(request)) return send(response, 403, { ok: false, error: 'forbidden' });
  const url = new URL(request.url || '/', 'http://127.0.0.1');
  try {
    if (request.method === 'GET' && url.pathname === '/health') {
      return send(response, 200, { ok: true, provider: 'qishui', status: qrLogin.getStatus(), playbackReady: true });
    }
    if (request.method === 'GET' && url.pathname === '/session') {
      return send(response, 200, { ok: true, provider: 'qishui', status: qrLogin.getStatus(), cookie: qrLogin.getCookie() });
    }
    if (request.method === 'POST' && url.pathname === '/search') {
      const body = await readJson(request);
      const keyword = String(body.keyword || body.q || '').trim();
      if (!keyword) return send(response, 400, { ok: false, error: 'missing search keyword' });
      const limit = Math.max(1, Math.min(30, Number(body.limit) || 8));
      const offset = Math.max(0, Number(body.offset) || 0);
      const result = await qishuiApi.handleQishuiSearch(keyword, limit, qrLogin.getCookie(), offset);
      return send(response, 200, { ok: true, result });
    }
    if (request.method === 'POST' && url.pathname === '/playback') {
      const body = await readJson(request);
      const id = String(body.id || '').trim();
      if (!id) return send(response, 400, { ok: false, error: 'missing track id' });
      const result = await qishuiApi.handleQishuiSongUrl({ id, quality: String(body.quality || '') }, qrLogin.getCookie());
      return send(response, 200, { ok: true, result });
    }
    if ((request.method === 'GET' || request.method === 'POST') && url.pathname === '/audio') {
      const body = request.method === 'POST' ? await readJson(request) : {};
      const audioUrl = String(body.url || url.searchParams.get('url') || '');
      return await proxyAudio(request, response, audioUrl);
    }
    if (request.method === 'POST' && url.pathname === '/qr/start') {
      const result = await qrLogin.createQrCode();
      const data = result && result.data || {};
      return send(response, 200, { ok: true, result: { ...result, data: { ...data, qr_image: data.qrcode, qr_url: data.scan_url } } });
    }
    if (request.method === 'POST' && url.pathname === '/qr/poll') {
      const body = await readJson(request);
      const token = String(body.token || url.searchParams.get('token') || '');
      const result = await qrLogin.checkQrConnect(token);
      const data = result && result.data || {};
      const confirmed = Boolean(data.confirmed || result.status === 'authorized');
      return send(response, 200, {
        ok: true,
        result: {
          ...result,
          status: confirmed ? 'authorized' : (result.status || 'waiting'),
          cookie: confirmed ? qrLogin.getCookie() : '',
          data: { ...data, confirmed },
        },
      });
    }
    if (request.method === 'POST' && url.pathname === '/clear') {
      return send(response, 200, { ok: true, status: await qrLogin.clear() });
    }
    return send(response, 404, { ok: false, error: 'not found' });
  } catch (error) {
    console.error('[QishuiBridge]', safeError(error));
    if (response.headersSent) {
      try { response.destroy(); } catch (_) {}
      return;
    }
    return send(response, 500, { ok: false, error: safeError(error), code: String(error && error.code || '') });
  }
}

const server = http.createServer((request, response) => {
  if (request.url && request.url.length > 4096) return send(response, 414, { ok: false, error: 'uri too long' });
  route(request, response).catch(error => {
    if (response.headersSent) {
      try { response.destroy(); } catch (_) {}
    } else {
      send(response, 500, { ok: false, error: safeError(error) });
    }
  });
});

server.on('error', error => {
  console.error('[QishuiBridge] server error:', safeError(error));
  process.exitCode = 1;
});

server.listen(port, '127.0.0.1', () => {
  console.log(`[QishuiBridge] listening on 127.0.0.1:${port}`);
});

function shutdown() {
  server.close(() => { try { app.quit(); } catch (_) {} });
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
