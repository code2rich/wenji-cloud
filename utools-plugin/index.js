'use strict';

const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];
const services = window.services;
const form = $('#settings-form');
let view = 'sync';

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtSize(bytes) {
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(2)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function fmtTime(secs) {
  if (!isFinite(secs) || secs <= 0) return '';
  if (secs < 60) return `${Math.ceil(secs)} 秒`;
  return secs < 3600 ? `${Math.ceil(secs / 60)} 分钟` : `${(secs / 3600).toFixed(1)} 小时`;
}

/** note20261019-091446.opus → “10月19日 09:14 笔记” */
function friendly(name) {
  const m = name.match(/^(note|call)?(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})/i);
  if (!m) return name;
  const kind = { note: '笔记', call: '通话' }[(m[1] || '').toLowerCase()] || '录音';
  return `${Number(m[3])}月${Number(m[4])}日 ${m[5]}:${m[6]} ${kind}`;
}

function kindIcon(name) {
  const n = name.toLowerCase();
  return n.startsWith('call') ? 'i-phone' : n.startsWith('note') ? 'i-mic' : 'i-file';
}

function badge(f) {
  const pct = f.size ? Math.floor((f.done || 0) / f.size * 100) : 0;
  const text = {
    synced: '已同步', done: '已上传', failed: '失败', unknown: '比对中',
    uploading: `上传中 ${pct}%`, pending: f.done ? `待续传 ${pct}%` : '待上传',
  }[f.state] || f.state;
  return `<span class="badge ${f.state}" ${f.error ? `title="${esc(f.error)}"` : ''}>${text}</span>`;
}

function renderHero(s, cfg) {
  const hero = $('#hero');
  const configured = cfg.server && cfg.token;
  const counts = { synced: 0, pending: 0, failed: 0, pendingBytes: 0 };
  for (const f of s.files) {
    if (f.state === 'synced' || f.state === 'done') counts.synced++;
    else if (f.state === 'failed') counts.failed++;
    else if (f.state === 'pending') { counts.pending++; counts.pendingBytes += f.size - (f.done || 0); }
  }
  const uploadBtn = `<button class="btn" data-act="pick"${!configured || s.running ? ' disabled' : ''}><svg><use href="#i-upload"/></svg>选择文件上传</button>`;
  let cls = '', icon = 'i-card', title, sub, actions = '';

  if (!configured) {
    cls = 'empty';
    title = '先完成设置';
    sub = '填写服务地址和访问令牌后即可同步';
    actions = '<button class="btn primary large" data-act="settings">去设置</button>';
  } else if (s.running && s.progress) {
    cls = 'syncing';
    icon = 'i-upload';
    const p = s.progress;
    const pct = p.totalBytes ? Math.min(100, p.doneBytes / p.totalBytes * 100) : 0;
    const eta = p.speed ? (p.totalBytes - p.doneBytes) / p.speed : 0;
    title = s.card ? `正在同步「${esc(s.card.name)}」` : '正在上传';
    sub = `第 ${Math.min(p.index + 1, p.count)} / ${p.count} 个 · ${fmtSize(p.doneBytes)} / ${fmtSize(p.totalBytes)}`;
    sub += `<div class="progress"><div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="progress-text"><span>${esc(friendly(p.name || ''))}</span>
      <span>${p.speed ? `${fmtSize(p.speed)}/s${eta > 1 ? ` · 剩余约 ${fmtTime(eta)}` : ''}` : ''}</span></div></div>`;
  } else if (s.running) {
    title = '正在与服务端比对…';
    sub = '已在服务端的录音会直接跳过';
  } else if (!s.card) {
    cls = 'empty';
    title = '插入录音卡后自动同步';
    sub = '也可以选择电脑上的音频文件上传，或在访达（Windows 资源管理器）中选中文件后用 uTools“上传到闻记”';
    actions = uploadBtn;
  } else if (s.checkError) {
    title = `录音卡「${esc(s.card.name)}」`;
    sub = `<span class="warn">${esc(s.checkError)}</span>`;
    actions = '<button class="btn" data-act="recheck">重试</button>';
  } else if (!s.checkedAt) {
    title = `录音卡「${esc(s.card.name)}」`;
    sub = `共 ${s.files.length} 条录音，正在与服务端比对…`;
  } else if (counts.pending || counts.failed) {
    title = `录音卡「${esc(s.card.name)}」`;
    sub = `共 ${s.files.length} 条 · 服务端已有 <b>${counts.synced}</b> 条 · 待上传 <b>${counts.pending + counts.failed}</b> 条（${fmtSize(counts.pendingBytes)}）`;
    actions = `<button class="btn primary large" data-act="sync"><svg><use href="#i-upload"/></svg>立即同步</button>`;
  } else {
    cls = 'alldone';
    icon = 'i-check';
    title = '全部已同步';
    sub = `录音卡「${esc(s.card.name)}」上的 ${s.files.length} 条录音都已在服务端`;
    actions = uploadBtn;
  }
  hero.className = `hero ${cls}`;
  hero.innerHTML = `<span class="hero-icon"><svg><use href="#${icon}"/></svg></span>
    <div class="hero-body"><div class="hero-title">${title}</div><div class="hero-sub">${sub}</div></div>
    <div class="hero-actions">${actions}</div>`;
}

function renderFiles(s) {
  $('#files-wrap').hidden = !s.card || !s.files.length;
  $('#recheck').disabled = s.running;
  $('#files-title').textContent = s.card ? `「${s.card.name}」上的录音 · ${s.files.length} 条` : '';
  // 需要处理的排在前面：上传中 → 失败 → 待上传 → 已完成
  const order = { uploading: 0, failed: 1, pending: 2, unknown: 3, done: 4, synced: 5 };
  const files = [...s.files].sort((a, b) => order[a.state] - order[b.state] || b.name.localeCompare(a.name));
  $('#files').innerHTML = files.map(f => `
    <div class="file">
      <span class="file-icon ${kindIcon(f.name)}"><svg><use href="#${kindIcon(f.name)}"/></svg></span>
      <div class="file-name"><strong>${esc(friendly(f.name))}</strong><small>${esc(f.name)}</small></div>
      <span class="file-size">${fmtSize(f.size)}</span>
      ${badge(f)}
    </div>`).join('');
}

function renderRecent(s) {
  const recent = s.log.slice(0, 6);
  $('#recent').hidden = Boolean(s.card) || !recent.length;
  $('#recent-list').innerHTML = recent.map(item => `
    <li><time>${new Date(item.time).toLocaleTimeString('zh-CN', { hour12: false })}</time>
    <span class="${item.level}">${esc(item.message)}</span></li>`).join('');
}

function renderConn(s) {
  const c = s.connection;
  const el = $('#conn');
  el.className = `conn ${c.status}`;
  $('#conn-text').textContent = {
    ok: `已连接 ${c.message}`, error: '连接失败', unconfigured: '未配置', unknown: '检测中…',
  }[c.status];
  el.title = c.status === 'error' ? `${c.message}（点击重新检测）` : '点击重新检测';
}

function renderLog(s) {
  $('#log').innerHTML = s.log.map(item => `
    <li><time>${new Date(item.time).toLocaleTimeString('zh-CN', { hour12: false })}</time>
    <span class="${item.level}">${esc(item.message)}</span></li>`).join('') || '<li class="empty-text">暂无同步记录</li>';
}

function render(s = services.getState()) {
  const cfg = services.getConfig();
  renderConn(s);
  renderHero(s, cfg);
  renderFiles(s);
  renderRecent(s);
  renderLog(s);
}

function show(next) {
  view = next;
  $$('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  for (const name of ['sync', 'log', 'settings']) $(`#view-${name}`).hidden = name !== view;
  if (view === 'settings') loadForm();
}

function renderServers(cfg = services.getConfig()) {
  $('#servers').hidden = !cfg.servers.length;
  $('#server-list').innerHTML = cfg.servers.map(url => `
    <span class="server-chip${url === cfg.server ? ' current' : ''}">
      <button type="button" data-use-server="${esc(url)}" title="${esc(url)}">${esc(url.replace(/^https?:\/\//, ''))}</button>
      <button type="button" data-remove-server="${esc(url)}" title="移除这个地址">×</button>
    </span>`).join('');
}

$('#server-list').addEventListener('click', async event => {
  const use = event.target.closest('[data-use-server]');
  const remove = event.target.closest('[data-remove-server]');
  try {
    if (use) {
      const cfg = services.saveConfig({ server: use.dataset.useServer });
      form.server.value = cfg.server;
      renderServers(cfg);
      message('已切换，正在检测连接…');
      const conn = await services.checkConnection();
      form.server.value = services.getConfig().server; // 连不上时可能已自动换到其他地址
      renderServers();
      message(conn.status === 'ok' ? `已连接 ${conn.message}` : `连接失败：${conn.message}`, conn.status === 'ok' ? 'ok' : 'error');
    } else if (remove) {
      const cfg = services.removeServer(remove.dataset.removeServer);
      form.server.value = cfg.server;
      renderServers(cfg);
      message('已移除');
    }
  } catch (e) {
    message(e.message, 'error');
  }
});

function loadForm() {
  const cfg = services.getConfig();
  renderServers(cfg);
  form.server.value = cfg.server;
  form.token.value = cfg.token;
  form.prefer.value = cfg.prefer;
  form.autoSync.checked = cfg.autoSync;
  form.ejectAfter.checked = cfg.ejectAfter;
  $('#welcome').hidden = Boolean(cfg.server && cfg.token);
  message('');
}

function formValue() {
  return {
    server: form.server.value, token: form.token.value, prefer: form.prefer.value,
    autoSync: form.autoSync.checked, ejectAfter: form.ejectAfter.checked,
  };
}

function message(text, level = '') {
  const el = $('#settings-msg');
  el.textContent = text;
  el.className = `msg ${level}`;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const before = services.getConfig();
    const cfg = services.saveConfig(formValue());
    renderServers(cfg);
    message('已保存，正在检测连接…');
    const conn = await services.checkConnection();
    message(conn.status === 'ok' ? '已保存，连接正常' : `已保存，但连接失败：${conn.message}`, conn.status === 'ok' ? 'ok' : 'error');
    if (conn.status === 'ok' && (!before.server || !before.token || before.prefer !== cfg.prefer)) {
      await services.inspect();
      if (!before.server || !before.token) setTimeout(() => show('sync'), 600);
    }
  } catch (e) {
    message(e.message, 'error');
  }
});

$('#test').addEventListener('click', async () => {
  message('连接中…');
  try {
    await services.testConnection(formValue());
    message('连接成功，记得点“保存”', 'ok');
  } catch (e) {
    message(e.message, 'error');
  }
});

function act(fn) {
  return () => fn().catch(() => render());
}

document.body.addEventListener('click', event => {
  const el = event.target.closest('[data-act], [data-view]');
  if (!el) return;
  if (el.dataset.view) return show(el.dataset.view);
  ({
    sync: act(() => services.syncNow()),
    pick: act(() => services.pickAndUpload()),
    recheck: act(() => services.inspect()),
    settings: () => show('settings'),
  })[el.dataset.act]?.();
});

$('#recheck').addEventListener('click', act(() => services.inspect()));
$('#conn').addEventListener('click', act(() => services.checkConnection()));
$('#open-web').addEventListener('click', () => services.openWeb());

services.onUpdate(render);

utools.onPluginEnter(({ code, payload }) => {
  const cfg = services.getConfig();
  render();
  show(cfg.server && cfg.token ? (view === 'settings' ? 'sync' : view) : 'settings');
  if (cfg.server && cfg.token) {
    services.checkConnection();
    if (services.getState().card && !services.getState().running) services.inspect();
  }
  if (code === 'upload' && Array.isArray(payload)) {
    show('sync');
    services.uploadPaths(payload.filter(f => f.isFile).map(f => f.path)).catch(() => render());
  }
});
