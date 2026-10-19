'use strict';
// uTools 预加载脚本：运行在 Node 环境，负责监测录音卡、与服务端比对和后台同步。
// 界面通过 window.services 读取状态并订阅变化。

const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { PING, findCards, listCardFiles, Client, pickServer, syncFiles } = require('./lib/sync');

const CONFIG_KEY = 'wenji-config';
const TOKEN_KEY = 'wenji-token';
// server 是当前使用的地址，servers 是保存过的全部地址（内网、穿透等），连不上时依次尝试。
const DEFAULTS = { server: '', servers: [], token: '', prefer: 'opus', autoSync: true, ejectAfter: false };
const POLL_MS = 3000;
const VOLUMES = process.env.WENJI_VOLUMES || '/Volumes'; // 环境变量仅用于测试
// 令牌优先放加密存储，避免随 uTools 数据同步以明文出现在其他设备上。
const secretStore = utools.dbCryptoStorage || utools.dbStorage;

const listeners = new Set();
const state = {
  running: false,
  card: null,               // { name, dir }
  files: [],                // [{ name, size, path, state: synced|pending|partial|uploading|done|failed, done, error }]
  checkedAt: null,
  checkError: '',
  connection: { status: 'unknown', message: '' }, // unknown | ok | error | unconfigured
  progress: null,           // { name, totalBytes, doneBytes, speed, startedAt }
  log: [],
};
let seen = new Set(); // 已自动同步过的卡，拔出后移除，再插入会重新同步

function config() {
  const cfg = { ...DEFAULTS, ...(utools.dbStorage.getItem(CONFIG_KEY) || {}), token: secretStore.getItem(TOKEN_KEY) || '' };
  if (cfg.server && !cfg.servers.includes(cfg.server)) cfg.servers = [cfg.server, ...cfg.servers]; // 兼容只存一个地址的旧配置
  return cfg;
}

function writeConfig(next) {
  const { token, ...rest } = next;
  utools.dbStorage.setItem(CONFIG_KEY, rest);
  secretStore.setItem(TOKEN_KEY, String(token || '').trim());
  return config();
}

function normalizeServer(value) {
  const server = String(value || '').trim().replace(/\/+$/, '');
  if (server && !/^https?:\/\/[^/]+/.test(server)) throw new Error('服务地址需以 http:// 或 https:// 开头');
  return server;
}

const host = url => new URL(url).host;

function emit() {
  for (const fn of listeners) fn(state);
}

function log(message, level = 'info') {
  state.log.unshift({ time: Date.now(), message, level });
  state.log.length = Math.min(state.log.length, 200);
  emit();
}

function client() {
  return new Client(config());
}

async function checkConnection() {
  const cfg = config();
  if (!cfg.server || !cfg.token) {
    state.connection = { status: 'unconfigured', message: '未配置服务地址和令牌' };
  } else {
    try {
      const server = await pickServer(cfg);
      if (server !== cfg.server) {
        writeConfig({ ...cfg, server });
        log(`「${host(cfg.server)}」连不上，已切换到「${host(server)}」`);
      }
      state.connection = { status: 'ok', message: host(server) };
    } catch (e) {
      state.connection = { status: 'error', message: e.message };
    }
  }
  emit();
  return state.connection;
}

/** 列出卡上文件并向服务端比对，只读不上传。 */
async function inspect(retried = false) {
  if (!state.card) return;
  const cfg = config();
  let files;
  try {
    files = listCardFiles(state.card.dir, cfg.prefer);
  } catch (e) {
    state.checkError = `读取录音卡失败：${e.message}`;
    emit();
    return;
  }
  state.files = files.map(f => ({ ...f, state: 'unknown' }));
  state.checkError = '';
  emit();
  if (!cfg.server || !cfg.token) return;
  try {
    const { results } = await client().check(files);
    applyResults(results);
    state.checkedAt = Date.now();
    state.connection = { status: 'ok', message: host(cfg.server) };
  } catch (e) {
    // 网络不通时先尝试切换到其他已保存地址，切换成功再比对一次。
    if (e.retryable && !retried && (await checkConnection()).status === 'ok') return inspect(true);
    state.checkError = e.message;
    state.connection = { status: 'error', message: e.message };
  }
  emit();
}

function applyResults(results) {
  for (const r of results) {
    const f = state.files.find(x => x.name === r.name && x.size === r.size);
    if (!f) continue;
    f.state = r.state === 'synced' ? 'synced' : 'pending';
    f.done = r.state === 'partial' ? r.offset : 0;
  }
}

function eject(card) {
  if (process.platform === 'win32') {
    // 资源管理器的“弹出”：命令本身不报告成败，稍等后看盘符是否还在。
    const script = `(New-Object -ComObject Shell.Application).Namespace(17).ParseName('${card.name}').InvokeVerb('Eject')`;
    return new Promise(resolve => {
      execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { timeout: 30000 }, () => {
        setTimeout(() => resolve(!fs.existsSync(card.root)), 3000);
      });
    });
  }
  return new Promise(resolve => {
    execFile('diskutil', ['eject', card.root], { timeout: 30000 }, err => resolve(!err));
  });
}

async function upload(files, source, label) {
  if (state.running) throw new Error('正在同步，请稍候');
  state.running = true;
  state.progress = null;
  emit();
  const started = Date.now();
  let sentBytes = 0;
  try {
    const result = await syncFiles({
      client: client(), files, source,
      onEvent: e => {
        const f = state.files.find(x => x.name === e.name);
        if (e.type === 'checked') {
          applyResults(e.results);
          const pending = e.results.filter(r => r.state !== 'synced');
          const totalBytes = pending.reduce((sum, r) => sum + r.size - (r.offset || 0), 0);
          state.progress = { totalBytes, doneBytes: 0, count: pending.length, index: 0, name: '', speed: 0 };
          log(`${label}：共 ${e.total} 条，服务端已有 ${e.synced} 条，需上传 ${e.pending} 条`);
        } else if (e.type === 'progress') {
          sentBytes += e.sent;
          const elapsed = (Date.now() - started) / 1000;
          Object.assign(state.progress, { name: e.name, doneBytes: sentBytes, speed: elapsed > 0 ? sentBytes / elapsed : 0 });
          if (f) Object.assign(f, { state: 'uploading', done: e.done });
          emit();
        } else if (e.type === 'uploaded') {
          state.progress.index++;
          if (f) Object.assign(f, { state: 'done', done: f.size });
          log(`已上传 ${e.name}`, 'ok');
        } else if (e.type === 'failed') {
          if (f) Object.assign(f, { state: 'failed', error: e.error });
          log(`上传 ${e.name} 失败：${e.error}`, 'error');
        }
      },
    });
    state.checkedAt = Date.now();
    state.connection = { status: 'ok', message: host(config().server) };
    return result;
  } catch (e) {
    state.connection = { status: 'error', message: e.message };
    throw e;
  } finally {
    state.running = false;
    state.progress = null;
    emit();
  }
}

async function syncCard() {
  if (state.running) throw new Error('正在同步，请稍候'); // 重复点击不算失败，不写记录
  if (!state.card) throw new Error('没有发现录音卡');
  const cfg = config();
  if (!cfg.server || !cfg.token) throw new Error('请先在设置中填写服务地址和访问令牌');
  const card = state.card;
  const files = listCardFiles(card.dir, cfg.prefer);
  if (!state.files.length) state.files = files.map(f => ({ ...f, state: 'unknown' }));
  try {
    const result = await upload(files, card.name, `录音卡「${card.name}」`);
    let text = result.uploaded.length
      ? `同步完成：新上传 ${result.uploaded.length} 条，跳过已同步 ${result.skipped} 条`
      : `全部 ${result.total} 条录音都已在服务端，无需上传`;
    if (result.failed.length) text += `，失败 ${result.failed.length} 条`;
    if (cfg.ejectAfter && !result.failed.length) text += (await eject(card)) ? '，已推出录音卡' : '，推出录音卡失败';
    log(text, result.failed.length ? 'error' : 'ok');
    utools.showNotification(text);
    return result;
  } catch (e) {
    log(`同步失败：${e.message}`, 'error');
    utools.showNotification(`闻记同步失败：${e.message}`);
    throw e;
  }
}

async function uploadPaths(paths) {
  const files = paths.filter(p => fs.statSync(p).isFile())
    .map(p => ({ name: path.basename(p), path: p, size: fs.statSync(p).size }));
  if (!files.length) throw new Error('没有可上传的文件');
  const result = await upload(files, '手动上传', `手动上传 ${files.length} 个文件`);
  const text = `上传完成：新增 ${result.uploaded.length} 个，服务端已有 ${result.skipped} 个` +
    (result.failed.length ? `，失败 ${result.failed.length} 个` : '');
  log(text, result.failed.length ? 'error' : 'ok');
  utools.showNotification(text);
  return result;
}

// 轮询卷列表（macOS 的 /Volumes、Windows 的盘符）：插入录音卡后先比对，开启自动同步时直接上传缺失文件。
// 插件需在 uTools 中开启“跟随主程序同时启动运行”并关闭“退出到后台立即结束运行”才能常驻。
function poll() {
  const cards = findCards(VOLUMES);
  const names = new Set(cards.map(c => c.name));
  seen = new Set([...seen].filter(n => names.has(n)));
  const card = cards[0] || null;
  if ((card && card.name) !== (state.card && state.card.name)) {
    state.card = card;
    state.files = [];
    state.checkedAt = null;
    state.checkError = '';
    emit();
    if (card) {
      log(`发现录音卡「${card.name}」`);
      inspect().then(() => {
        const cfg = config();
        if (cfg.autoSync && !seen.has(card.name) && state.connection.status === 'ok' && !state.running) {
          seen.add(card.name);
          if (state.files.some(f => f.state === 'pending')) syncCard().catch(() => {});
        }
      });
    } else {
      log('录音卡已拔出');
    }
  }
}

setInterval(poll, POLL_MS);
setTimeout(() => {
  checkConnection();
  poll();
}, 500);

window.services = {
  getState: () => state,
  getConfig: config,
  saveConfig(value) {
    const next = { ...config(), ...value };
    next.server = normalizeServer(next.server);
    if (next.server && !next.servers.includes(next.server)) next.servers = [...next.servers, next.server];
    return writeConfig(next);
  },
  removeServer(url) {
    const cfg = config();
    const servers = cfg.servers.filter(s => s !== url);
    return writeConfig({ ...cfg, servers, server: cfg.server === url ? servers[0] || '' : cfg.server });
  },
  async testConnection(value) {
    const cfg = { ...config(), ...value };
    await new Client({ ...cfg, server: normalizeServer(cfg.server) }).request('GET', PING);
    return true;
  },
  checkConnection,
  inspect,
  syncNow: syncCard,
  uploadPaths,
  async pickAndUpload() {
    const paths = utools.showOpenDialog({
      title: '选择要上传到闻记的音频',
      properties: ['openFile', 'multiSelections'],
      filters: [{ name: '音频', extensions: ['wav', 'opus', 'ogg', 'mp3', 'm4a', 'flac', 'aac', 'webm', 'mp4'] }],
    });
    if (paths && paths.length) return uploadPaths(paths);
    return null;
  },
  onUpdate(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  openWeb() {
    const { server } = config();
    if (server) utools.shellOpenExternal(server);
  },
};
