'use strict';
// 同步逻辑（不依赖 uTools，便于测试）：发现录音卡 → 与服务端比对 → 分片续传缺失文件。

const fs = require('fs');
const path = require('path');

const CHUNK = 8 * 1024 * 1024; // 远小于内网穿透常见的 100MB 请求体限制
const AUDIO_EXTS = new Set(['.wav', '.opus', '.ogg', '.mp3', '.m4a', '.flac', '.aac', '.webm', '.mp4']);
const SYSTEM_VOLUMES = new Set(['macintosh hd', 'recovery', 'preboot', 'vm', 'update']);

/**
 * 列出可能是录音卡的卷：macOS 是 /Volumes 下的各个卷，Windows 是除系统盘外的 D:–Z: 盘符。
 * 返回 [{ name, root }]，root 是卷的根目录。
 */
function volumeRoots(platform = process.platform, volumesDir = '/Volumes') {
  if (platform === 'win32') {
    const system = (process.env.SystemDrive || 'C:').toUpperCase();
    return [...'DEFGHIJKLMNOPQRSTUVWXYZ'].map(letter => `${letter}:`)
      .filter(drive => drive !== system)
      .map(drive => ({ name: drive, root: drive + '\\' }));
  }
  let names;
  try {
    names = fs.readdirSync(volumesDir);
  } catch {
    return [];
  }
  return names
    .filter(name => !name.startsWith('.') && !SYSTEM_VOLUMES.has(name.toLowerCase()))
    .map(name => ({ name, root: path.join(volumesDir, name) }));
}

/** 含 record/ 目录的卷视为录音卡。 */
function findCards(volumesDir = '/Volumes', platform = process.platform) {
  return volumeRoots(platform, volumesDir)
    .map(v => ({ ...v, dir: path.join(v.root, 'record') }))
    .filter(card => {
      try {
        return fs.statSync(card.dir).isDirectory();
      } catch {
        return false;
      }
    });
}

/**
 * 列出卡上待同步的文件。同一录音常同时有 .opus 和 .wav，按 prefer 只取一个：
 * opus 体积约为 wav 的 1/16，经内网穿透上传更快；wav 是无损原始音频。
 */
function listCardFiles(dir, prefer = 'opus') {
  const byStem = new Map();
  for (const name of fs.readdirSync(dir)) {
    const ext = path.extname(name).toLowerCase();
    if (name.startsWith('.') || !AUDIO_EXTS.has(ext)) continue;
    const full = path.join(dir, name);
    const stat = fs.statSync(full);
    if (!stat.isFile() || stat.size === 0) continue;
    const stem = name.slice(0, -ext.length);
    if (!byStem.has(stem)) byStem.set(stem, []);
    byStem.get(stem).push({ name, path: full, size: stat.size, ext });
  }
  const files = [];
  for (const candidates of byStem.values()) {
    const preferred = candidates.find(f => f.ext === '.' + prefer);
    const fallback = candidates.find(f => f.ext === '.opus' || f.ext === '.wav');
    files.push(preferred || fallback || candidates[0]);
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

class Client {
  constructor({ server, token, fetchImpl = globalThis.fetch }) {
    if (!server || !token) throw new Error('请先在设置中填写服务地址和访问令牌');
    this.base = server.replace(/\/+$/, '');
    this.token = token;
    // 不能直接存成方法再用 this.fetch() 调用：浏览器的 fetch 以非 window 为 this 调用会报 Illegal invocation。
    this.fetch = (url, init) => fetchImpl(url, init);
  }

  async request(method, url, { json, body, headers = {} } = {}) {
    const init = { method, headers: { Authorization: `Bearer ${this.token}`, ...headers } };
    if (json !== undefined) {
      init.body = JSON.stringify(json);
      init.headers['Content-Type'] = 'application/json';
    } else if (body !== undefined) {
      init.body = body;
      init.headers['Content-Type'] = 'application/octet-stream';
    }
    let res;
    try {
      res = await this.fetch(this.base + url, init);
    } catch (e) {
      const err = new Error(`无法连接服务端：${e.cause?.code || e.message}`);
      err.retryable = true;
      throw err;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail;
      const err = new Error(res.status === 401 ? '访问令牌错误'
        : typeof detail === 'string' ? detail : detail?.message || `服务端返回 ${res.status}`);
      err.status = res.status;
      err.detail = detail;
      err.retryable = res.status >= 500 || res.status === 429;
      throw err;
    }
    return data;
  }

  check(files) {
    return this.request('POST', '/api/sync/check', { json: { items: files.map(f => ({ name: f.name, size: f.size })) } });
  }
}

const PING = '/api/recordings?q=__wenji_ping__';

/**
 * 从当前地址开始依次尝试已保存的地址，返回第一个能连通的。
 * 例如在家走内网地址、出门后自动改走穿透地址。当前地址令牌错误等非网络问题直接抛出，不换地址。
 */
async function pickServer({ server, servers = [], token, fetchImpl = globalThis.fetch, timeoutMs = 5000 }) {
  const candidates = [...new Set([server, ...servers].filter(Boolean))];
  // 内网地址在外网时可能一直不响应，限时放弃。
  const timed = (url, init) => fetchImpl(url, { ...init, signal: AbortSignal.timeout(timeoutMs) });
  let firstError;
  for (const url of candidates) {
    try {
      await new Client({ server: url, token, fetchImpl: timed }).request('GET', PING);
      return url;
    } catch (e) {
      if (url === server && !e.retryable) throw e;
      firstError = firstError || e;
    }
  }
  throw firstError || new Error('请先在设置中填写服务地址和访问令牌');
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function withRetry(fn, attempts = 4) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (!e.retryable || i >= attempts) throw e;
      await sleep(1000 * 2 ** i);
    }
  }
}

/** 从服务端记录的偏移开始续传一个文件，返回服务端录音 id。 */
async function uploadFile(client, file, source, onProgress = () => {}) {
  const open = () => withRetry(() => client.request('POST', '/api/uploads',
    { json: { name: file.name, size: file.size, source } }));
  let session = await open();
  if (session.state === 'synced') return session.recording_id;
  let offset = session.offset;
  let reopened = 0;
  const fd = fs.openSync(file.path, 'r');
  try {
    while (offset < file.size) {
      const length = Math.min(CHUNK, file.size - offset);
      const chunk = Buffer.alloc(length);
      const read = fs.readSync(fd, chunk, 0, length, offset);
      if (read !== length) throw new Error(`读取 ${file.name} 失败，录音卡可能已拔出`);
      try {
        const res = await withRetry(() => client.request('PUT', `/api/uploads/${session.upload_id}`,
          { body: chunk, headers: { 'Upload-Offset': String(offset) } }));
        offset = res.offset;
        onProgress(offset, file.size, read);
        if (res.complete) return res.recording_id;
      } catch (e) {
        // 服务端偏移与本地不一致（例如上次中断时最后一片其实已写入），按服务端偏移继续。
        if (e.status === 409 && typeof e.detail?.offset === 'number') {
          offset = e.detail.offset;
          continue;
        }
        // 会话已不存在：多半是最后一片已写入并入库、但响应在网络上丢了，重新比对确认。
        if (e.status === 404 && reopened++ < 2) {
          session = await open();
          if (session.state === 'synced') return session.recording_id;
          offset = session.offset;
          continue;
        }
        throw e;
      }
    }
  } finally {
    fs.closeSync(fd);
  }
  throw new Error(`${file.name} 上传结束但服务端未确认完成`);
}

/**
 * 同步一组文件：先比对，已同步的跳过，缺失或未传完的续传。
 * onEvent({ type: 'checked' | 'progress' | 'uploaded' | 'failed', ... })
 */
async function syncFiles({ client, files, source = '', onEvent = () => {} }) {
  const { results } = await withRetry(() => client.check(files));
  const pending = results.filter(r => r.state !== 'synced');
  onEvent({ type: 'checked', total: files.length, synced: files.length - pending.length, pending: pending.length, results });
  const summary = { total: files.length, skipped: files.length - pending.length, uploaded: [], failed: [] };
  for (const result of pending) {
    const file = files.find(f => f.name === result.name && f.size === result.size);
    try {
      const id = await uploadFile(client, file, source, (done, size, sent) =>
        onEvent({ type: 'progress', name: file.name, done, size, sent }));
      summary.uploaded.push(file.name);
      onEvent({ type: 'uploaded', name: file.name, recordingId: id });
    } catch (e) {
      summary.failed.push({ name: file.name, error: e.message });
      onEvent({ type: 'failed', name: file.name, error: e.message });
      if (e.status === 401 || /录音卡可能已拔出/.test(e.message)) break; // 后续文件同样会失败
    }
  }
  return summary;
}

module.exports = { CHUNK, PING, volumeRoots, findCards, listCardFiles, Client, pickServer, uploadFile, syncFiles, withRetry };
