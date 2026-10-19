'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATUS = { pending: '待处理', queued: '排队中', transcribing: '转写中', summarizing: '生成纪要', done: '已完成', failed: '失败' };
const FILTERS = [
  { key: 'all', label: '全部', test: () => true },
  { key: 'pending', label: '待处理', test: r => r.status === 'pending' && !r.busy },
  { key: 'busy', label: '处理中', test: r => r.busy },
  { key: 'done', label: '已完成', test: r => r.status === 'done' && !r.busy },
  { key: 'failed', label: '失败', test: r => r.status === 'failed' && !r.busy },
];
const PROVIDERS = { 'funasr:paraformer-zh': '本地 Paraformer', 'funasr:sensevoice': '本地 SenseVoice', doubao: '豆包', openai: '兼容接口' };
const state = {
  recordings: [], filter: 'all', q: '', current: null, record: null, signature: '',
  tab: 'summary', follow: true, transcriptQuery: '', listSignature: '', queue: 0,
};

// ── 工具函数 ──────────────────────────────────────────────────────
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function highlight(text, q) {
  const safe = esc(text);
  if (!q) return safe;
  const needle = esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return safe.replace(new RegExp(needle, 'gi'), m => `<mark>${m}</mark>`);
}

function toast(message, { error = false, sticky = false } = {}) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = message;
  const box = $('#toasts');
  box.appendChild(el);
  [...box.querySelectorAll('.toast:not(.sticky)')].slice(0, -3).forEach(old => old.remove()); // 最多保留 3 条
  if (sticky) el.classList.add('sticky');
  else setTimeout(() => el.remove(), error ? 6000 : 2500);
  return el;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (options.body !== undefined && !(options.body instanceof ArrayBuffer) && !(options.body instanceof Blob)) {
    init.body = JSON.stringify(options.body);
    init.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, init);
  if (res.status === 401 && path !== '/api/login') {
    showLogin();
    throw new Error('登录已失效，请重新登录');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const err = new Error(typeof d === 'string' ? d : d?.message || `请求失败（${res.status}）`);
    err.status = res.status;
    err.detail = d;
    throw err;
  }
  return data;
}

function run(fn) {
  return async event => {
    try {
      await fn(event);
    } catch (e) {
      if (e.status !== 401) toast(e.message, { error: true });
    }
  };
}

const NAME_RE = /^(note|call)?(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})/i;

function stem(name) {
  return name.replace(/\.[^.]+$/, '');
}

/** 卡上文件名（note20261019-091446）转成“10月19日 09:14 笔记”；用户改过的标题原样显示。 */
function displayTitle(r) {
  const m = r.title === stem(r.name) && r.title.match(NAME_RE);
  if (!m) return r.title;
  const kind = { note: '笔记', call: '通话' }[(m[1] || '').toLowerCase()] || '录音';
  return `${Number(m[3])}月${Number(m[4])}日 ${m[5]}:${m[6]} ${kind}`;
}

function kindIcon(r) {
  const n = r.name.toLowerCase();
  return n.startsWith('call') ? 'i-phone' : n.startsWith('note') ? 'i-mic' : 'i-file';
}

function fmtDuration(s) {
  if (!s) return '';
  if (s < 60) return `${Math.max(1, Math.round(s))} 秒`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} 分钟` : `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分` : ''}`;
}

function fmtSize(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function when(r) {
  return new Date((r.recorded_at || r.created_at).replace(' ', 'T'));
}

function dayLabel(d) {
  const today = new Date();
  const start = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  const week = '日一二三四五六'[d.getDay()];
  const year = d.getFullYear() === today.getFullYear() ? '' : `${d.getFullYear()}年`;
  return `${year}${d.getMonth() + 1}月${d.getDate()}日 周${week}`;
}

function clock(ms, base) {
  if (base) return new Date(new Date(base.replace(' ', 'T')).getTime() + ms).toTimeString().slice(0, 8);
  const s = Math.floor(ms / 1000);
  return [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map(n => String(n).padStart(2, '0')).join(':');
}

function statusBadge(r) {
  const key = r.busy && r.status === 'pending' ? 'queued' : r.status;
  return `<span class="status ${esc(key)}">${STATUS[key] || esc(key)}</span>`;
}

function speakerName(rec, spk) {
  return rec.speaker_names[spk] || `说话人 ${Number(spk) + 1}`;
}

// ── 登录 ──────────────────────────────────────────────────────────
function showLogin() {
  $('#app').hidden = true;
  $('#login').hidden = false;
  clearInterval(state.timer);
  setTimeout(() => $('#login-token').focus(), 0);
}

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#login-error').textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: { token: $('#login-token').value.trim() } });
    $('#login-token').value = '';
    await start();
  } catch (e) {
    $('#login-error').textContent = e.message;
  }
});

$('#logout').addEventListener('click', run(async () => {
  await api('/api/logout', { method: 'POST' });
  showLogin();
}));

// ── 列表 ──────────────────────────────────────────────────────────
async function loadList() {
  const data = await api('/api/recordings?q=' + encodeURIComponent(state.q));
  state.recordings = data.recordings;
  state.queue = data.queue;
  $('#queue').hidden = !data.queue;
  $('#queue-text').textContent = `${data.queue} 个任务处理中`;
  if (!state.q) $('#nav-count').textContent = data.recordings.length || '';
  renderList();
}

function renderList() {
  const counts = Object.fromEntries(FILTERS.map(f => [f.key, state.recordings.filter(f.test).length]));
  const filter = FILTERS.find(f => f.key === state.filter);
  const rows = state.recordings.filter(filter.test);
  const signature = JSON.stringify([rows, state.filter, state.current, counts]);
  if (signature === state.listSignature) return;
  state.listSignature = signature;

  $('#filters').innerHTML = FILTERS.filter(f => f.key === 'all' || counts[f.key])
    .map(f => `<button class="chip ${f.key === state.filter ? 'active' : ''}" data-filter="${f.key}">${f.label}<b>${counts[f.key]}</b></button>`)
    .join('');

  if (!rows.length) {
    $('#list').innerHTML = state.q
      ? `<div class="empty"><svg><use href="#i-search"/></svg><strong>没有找到“${esc(state.q)}”</strong>换个关键词试试</div>`
      : state.recordings.length
        ? '<div class="empty"><strong>这个分类下没有录音</strong></div>'
        : `<div class="empty"><svg><use href="#i-mic"/></svg><strong>还没有录音</strong>插上录音卡，uTools 插件会自动同步；也可以点右上角“上传”。</div>`;
    return;
  }
  let html = '';
  let group = '';
  for (const r of rows) {
    const label = dayLabel(when(r));
    if (label !== group) {
      group = label;
      html += `<div class="group">${label}</div>`;
    }
    const time = when(r).toTimeString().slice(0, 5);
    html += `
      <button class="item ${r.id === state.current ? 'active' : ''}" data-id="${r.id}">
        <span class="item-icon ${kindIcon(r)}"><svg><use href="#${kindIcon(r)}"/></svg></span>
        <span style="min-width:0">
          <div class="item-title">${highlight(displayTitle(r), state.q)}</div>
          <div class="item-meta">${time}${r.duration_s ? ' · ' + fmtDuration(r.duration_s) : ''}${r.has_summary ? ' · 有纪要' : ''}</div>
        </span>
        ${statusBadge(r)}
        ${r.snippet ? `<span class="item-snippet">${highlight(r.snippet, state.q)}</span>` : ''}
      </button>`;
  }
  $('#list').innerHTML = html;
}

$('#filters').addEventListener('click', event => {
  const chip = event.target.closest('[data-filter]');
  if (!chip) return;
  state.filter = chip.dataset.filter;
  renderList();
});

$('#list').addEventListener('click', run(async event => {
  const item = event.target.closest('[data-id]');
  if (!item) return;
  await openRecord(Number(item.dataset.id));
}));

let searchTimer;
$('#search').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(run(async () => {
    state.q = $('#search').value.trim();
    await loadList();
  }), 250);
});

document.addEventListener('keydown', event => {
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
    event.preventDefault();
    $('#search').focus();
  }
  if (event.key === 'Escape') closeMenu();
  if (event.key === ' ' && $('#player') && !['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(document.activeElement.tagName)) {
    event.preventDefault();
    run(actions.toggle)();
  }
});

// ── 上传（网页端，与插件使用同一套分片续传接口）────────────────
const CHUNK = 8 * 1024 * 1024;

async function uploadFile(file, note) {
  const created = await api('/api/uploads', { method: 'POST', body: { name: file.name, size: file.size, source: '网页上传' } });
  if (created.state === 'synced') return 'skipped';
  let offset = created.offset;
  while (offset < file.size) {
    const body = await file.slice(offset, offset + CHUNK).arrayBuffer();
    try {
      const res = await api(`/api/uploads/${created.upload_id}`, {
        method: 'PUT', body, headers: { 'Upload-Offset': String(offset), 'Content-Type': 'application/octet-stream' },
      });
      offset = res.offset;
    } catch (e) {
      if (e.status === 409 && typeof e.detail?.offset === 'number') {
        offset = e.detail.offset;
        continue;
      }
      throw e;
    }
    note.textContent = `正在上传 ${file.name}：${Math.round(offset / file.size * 100)}%`;
  }
  return 'uploaded';
}

$('#upload-input').addEventListener('change', run(async event => {
  const files = [...event.target.files];
  event.target.value = '';
  if (!files.length) return;
  const note = toast('准备上传…', { sticky: true });
  let uploaded = 0, skipped = 0;
  try {
    for (const file of files) {
      (await uploadFile(file, note)) === 'uploaded' ? uploaded++ : skipped++;
    }
  } finally {
    note.remove();
  }
  toast(`上传完成：新增 ${uploaded} 条${skipped ? `，已存在 ${skipped} 条` : ''}`);
  await loadList();
}));

// ── 详情 ──────────────────────────────────────────────────────────
async function openRecord(id) {
  if (state.current !== id) {
    state.current = id;
    state.record = null;
    state.signature = '';
    state.transcriptQuery = '';
    renderList();
  }
  document.body.classList.add('show-detail');
  await loadDetail();
}

async function loadDetail() {
  const id = state.current;
  if (!id) return;
  const rec = await api(`/api/recordings/${id}`);
  if (state.current !== id) return;
  const fresh = !state.record || state.record.id !== id;
  state.record = rec;
  const signature = JSON.stringify([rec.status, rec.busy, rec.job, rec.error, rec.title, rec.speaker_names,
    rec.segments.length, rec.summary.length, rec.mindmap.length, rec.duration_s]);
  if (signature === state.signature) {
    updateElapsed();
    return;
  }
  state.signature = signature;
  if (fresh) {
    const hasSummary = rec.summary || rec.mindmap;
    state.tab = hasSummary ? 'summary' : rec.segments.length ? 'transcript' : 'summary';
  }
  renderDetail(fresh);
}

function primaryAction(rec) {
  if (rec.busy) return '<button class="btn primary" disabled>处理中…</button>';
  if (!rec.segments.length) return '<button class="btn primary" data-act="full">开始转写</button>';
  return `<button class="btn primary" data-act="summary">${rec.summary ? '重新生成纪要' : '生成纪要'}</button>`;
}

function banner(rec) {
  if (rec.busy) {
    const stage = rec.status === 'transcribing' ? '正在转写' : rec.status === 'summarizing' ? '正在生成纪要和脑图'
      : `排队中${state.queue > 1 ? `（队列中共 ${state.queue} 个任务）` : ''}`;
    const tip = rec.status === 'transcribing' && rec.duration_s
      ? `本地模型约需录音时长的 0.7 倍，这段约 ${fmtDuration(rec.duration_s * 0.7)}` : '可以离开页面，完成后自动更新';
    return `<div class="banner work"><span class="pulse"></span><div class="grow"><strong>${stage}…</strong>
      <small>${tip}<span id="elapsed"></span></small><div class="bar indeterminate"><i></i></div></div></div>`;
  }
  if (rec.status === 'failed') {
    return `<div class="banner fail"><svg><use href="#i-alert"/></svg><div class="grow"><strong>处理失败</strong>
      <small>${esc(rec.error)}</small></div><button class="btn small" data-act="${rec.segments.length ? 'summary' : 'full'}">重试</button></div>`;
  }
  if (!rec.segments.length) {
    return `<div class="banner idle"><svg><use href="#i-file"/></svg><div class="grow"><strong>尚未转写</strong>
      <small>${rec.duration_s ? `录音时长 ${fmtDuration(rec.duration_s)}，` : ''}点击“开始转写”加入处理队列</small></div></div>`;
  }
  return '';
}

function updateElapsed() {
  const el = $('#elapsed');
  const job = state.record?.job;
  if (!el || !job || job.status !== 'running') return;
  const secs = Math.max(0, Math.round((Date.now() - new Date(job.updated_at).getTime()) / 1000));
  el.textContent = ` · 已用时 ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
}

function renderDetail(fresh) {
  const rec = state.record;
  const pane = $('#detail');
  const speakers = [...new Set(rec.segments.map(s => s.speaker).filter(s => s !== null))];
  if (fresh || !$('#d-head')) {
    pane.innerHTML = `
      <div class="detail-inner">
        <button class="btn ghost back" data-act="back"><svg><use href="#i-back"/></svg>返回</button>
        <div id="d-head"></div>
        <div id="d-banner"></div>
        <div class="player">
          <audio id="player" preload="none" src="/api/recordings/${rec.id}/audio"></audio>
          <button class="play" data-act="toggle" title="播放 / 暂停（空格）"><svg><use href="#i-play"/></svg></button>
          <span class="time" id="p-cur">0:00</span>
          <div class="wave" id="wave" title="点击跳转">${waveBars(rec.id)}<div class="wave-done" id="wave-done">${waveBars(rec.id)}</div></div>
          <span class="time muted" id="p-total">${fmtClock(rec.duration_s || 0)}</span>
          <div class="speed">${[1, 1.25, 1.5, 2].map(r => `<button data-rate="${r}" class="${r === 1 ? 'active' : ''}">${r}×</button>`).join('')}</div>
        </div>
        <nav class="tabs" id="d-tabs"></nav>
        <div class="tab-body" id="d-body"></div>
      </div>`;
    pane.scrollTop = 0;
    const player = $('#player');
    player.addEventListener('timeupdate', onTimeUpdate);
    player.addEventListener('timeupdate', updatePlayer);
    player.addEventListener('loadedmetadata', updatePlayer);
    player.addEventListener('play', updatePlayer);
    player.addEventListener('pause', updatePlayer);
    player.addEventListener('waiting', () => $('.player')?.classList.add('loading'));
    player.addEventListener('playing', () => $('.player')?.classList.remove('loading'));
    $('#wave').addEventListener('click', event => {
      const box = event.currentTarget.getBoundingClientRect();
      const total = player.duration || state.record.duration_s || 0;
      if (!total) return;
      player.currentTime = Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)) * total;
      player.play().catch(() => {});
    });
  }
  $('#d-head').innerHTML = `
    <div class="detail-head">
      <div style="min-width:0;flex:1">
        <input class="title-input" id="title" value="${esc(displayTitle(rec))}" aria-label="标题">
        <div class="meta-row">
          ${statusBadge(rec)}
          <span class="meta"><svg><use href="#i-clock"/></svg>${esc((rec.recorded_at || rec.created_at.slice(0, 16).replace('T', ' ')).slice(0, 16))}</span>
          ${rec.duration_s ? `<span class="meta"><svg><use href="#i-wave"/></svg>${fmtDuration(rec.duration_s)}</span>` : ''}
          ${rec.source ? `<span class="meta"><svg><use href="#i-card"/></svg>${esc(rec.source)}</span>` : ''}
        </div>
        <div class="meta-sub">
          <span title="原始文件">${esc(rec.name)} · ${fmtSize(rec.size)}</span>
          ${rec.asr_provider ? `<span>转写 · ${esc(PROVIDERS[rec.asr_provider] || rec.asr_provider)}</span>` : ''}
          ${rec.summary_model ? `<span>纪要 · ${esc(rec.summary_model)}</span>` : ''}
        </div>
      </div>
      <div class="head-actions">
        ${primaryAction(rec)}
        <button class="btn icon" data-act="menu" title="更多操作"><svg><use href="#i-more"/></svg></button>
      </div>
    </div>`;
  $('#d-banner').innerHTML = banner(rec);
  updateElapsed();
  const tabs = [
    ['summary', '纪要'], ['mindmap', '脑图'], ['transcript', '转写', rec.segments.length],
    ...(speakers.length ? [['speakers', '说话人', speakers.length]] : []),
  ];
  if (!tabs.some(t => t[0] === state.tab)) state.tab = 'summary';
  $('#d-tabs').innerHTML = tabs.map(([key, label, n]) =>
    `<button data-tab="${key}" class="${key === state.tab ? 'active' : ''}">${label}${n ? `<span>${n}</span>` : ''}</button>`).join('');
  renderTab();
}

function emptyBody(rec, what) {
  if (rec.busy) return `<div class="empty"><strong>处理中</strong>${what}生成后会自动显示</div>`;
  if (!rec.segments.length) return `<div class="empty"><strong>还没有${what}</strong>先转写这段录音</div>`;
  return `<div class="empty"><strong>还没有${what}</strong>
    在设置中配置纪要模型后，点击“生成纪要”<button class="btn small" data-act="summary" style="margin-top:8px">生成纪要</button></div>`;
}

function renderTab() {
  const rec = state.record;
  const body = $('#d-body');
  if (state.tab === 'summary') {
    body.innerHTML = rec.summary
      ? `<div class="tab-tools"><span class="tab-note">${rec.summary_model ? `由 ${esc(rec.summary_model)} 生成` : ''}</span><button class="btn small" data-act="copy-summary"><svg><use href="#i-copy"/></svg>复制</button></div>
         <article class="prose paper">${markdown(rec.summary)}</article>`
      : emptyBody(rec, '纪要');
  } else if (state.tab === 'mindmap') {
    body.innerHTML = rec.mindmap
      ? `<div class="mindmap" id="mindmap"></div>`
      : emptyBody(rec, '脑图');
    if (rec.mindmap) renderMindmap(rec.mindmap);
  } else if (state.tab === 'transcript') {
    if (!rec.segments.length) {
      body.innerHTML = emptyBody(rec, '转写');
      return;
    }
    body.innerHTML = `
      <div class="tab-tools">
        <div class="search"><svg><use href="#i-search"/></svg><input id="t-search" type="search" placeholder="在转写中查找" value="${esc(state.transcriptQuery)}"></div>
        <label class="check"><input type="checkbox" id="follow" ${state.follow ? 'checked' : ''}> 跟随播放</label>
        <button class="btn small" data-act="copy-transcript"><svg><use href="#i-copy"/></svg>复制</button>
      </div>
      <div id="segments"></div>`;
    renderSegments();
  } else if (state.tab === 'speakers') {
    const stats = {};
    for (const s of rec.segments) {
      if (s.speaker === null) continue;
      stats[s.speaker] ??= { n: 0, ms: 0 };
      stats[s.speaker].n++;
      stats[s.speaker].ms += s.end_ms - s.start_ms;
    }
    const total = Object.values(stats).reduce((sum, st) => sum + st.ms, 0) || 1;
    body.innerHTML = `
      <p class="muted" style="margin-top:0">给说话人命名后，转写、导出和 MCP 查询都会使用新名字。想让纪要也使用新名字，保存后点“重新生成纪要”。</p>
      <div class="speakers">${Object.entries(stats).map(([spk, st]) => `
        <div class="speaker-row">
          ${avatar(rec, spk)}
          <span class="spk-info"><strong>说话人 ${Number(spk) + 1}</strong><small>${st.n} 段 · ${fmtDuration(st.ms / 1000)} · ${Math.round(st.ms / total * 100)}%</small>
            <span class="share"><i class="spk-bg-${Number(spk) % 6}" style="width:${(st.ms / total * 100).toFixed(1)}%"></i></span></span>
          <input data-spk="${esc(spk)}" value="${esc(rec.speaker_names[spk] || '')}" placeholder="输入姓名或角色，如：张三 / 客户">
        </div>`).join('')}</div>
      <div class="tab-tools" style="margin-top:14px"><button class="btn primary" data-act="names">保存命名</button></div>`;
  }
}

function renderSegments() {
  const rec = state.record;
  const q = state.transcriptQuery;
  const rows = rec.segments.map((s, i) => ({ ...s, i })).filter(s => !q || s.text.includes(q));
  $('#segments').innerHTML = rows.map(s => `
    <div class="seg${s.speaker === null ? ' anon' : ''}" data-i="${s.i}">
      ${s.speaker !== null ? avatar(rec, s.speaker) : ''}
      <div class="seg-body">
        <div class="seg-head">${s.speaker !== null ? `<span class="who spk-${Number(s.speaker) % 6}">${esc(speakerName(rec, s.speaker))}</span>` : ''}
          <time data-seek="${s.start_ms}" title="从这里播放">${clock(s.start_ms, rec.recorded_at)}</time></div>
        <p>${highlight(s.text, q)}</p></div>
    </div>`).join('') || `<div class="empty"><strong>没有找到“${esc(q)}”</strong></div>`;
}

function onTimeUpdate(event) {
  if (state.tab !== 'transcript' || !state.record) return;
  const ms = event.target.currentTime * 1000;
  const segs = state.record.segments;
  let lo = 0, hi = segs.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start_ms <= ms) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  const prev = $('.seg.now');
  const next = $(`.seg[data-i="${found}"]`);
  if (prev === next) return;
  prev?.classList.remove('now');
  if (next) {
    next.classList.add('now');
    if (state.follow) next.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

// 只渲染纪要模板用到的 Markdown 子集：标题、无序列表、加粗、段落。
function markdown(src) {
  const inline = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  let html = '', inList = false;
  for (const line of src.split('\n')) {
    const li = line.match(/^\s*[-*•]\s+(.*)$/);
    if (li) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inline(li[1])}</li>`;
      continue;
    }
    if (inList) { html += '</ul>'; inList = false; }
    const h = line.match(/^(#{1,5})\s+(.*)$/);
    const topic = line.match(/^\s*\*\*([^*]+)\*\*\s*$/);
    if (h) html += `<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`;
    else if (topic) html += `<h6>${esc(topic[1])}</h6>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return html + (inList ? '</ul>' : '');
}

// 脑图用 markmap 渲染（需能访问 jsDelivr）；加载失败时显示为大纲。
const MARKMAP_LIBS = [
  'https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js',
  'https://cdn.jsdelivr.net/npm/markmap-lib@0.18.12/dist/browser/index.iife.js',
  'https://cdn.jsdelivr.net/npm/markmap-view@0.18.12/dist/browser/index.js',
];
let markmapReady;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`加载失败：${src}`));
    document.head.appendChild(script);
  });
}

async function renderMindmap(md) {
  const box = $('#mindmap');
  try {
    markmapReady ??= MARKMAP_LIBS.reduce((p, src) => p.then(() => loadScript(src)), Promise.resolve());
    await markmapReady;
  } catch {
    markmapReady = null;
    if (box.isConnected) box.outerHTML = `<p class="muted">脑图组件加载失败，以大纲显示：</p><div class="prose paper">${markdown(md)}</div>`;
    return;
  }
  if (!box.isConnected) return;
  const { Transformer, Markmap } = window.markmap;
  box.innerHTML = '<svg></svg><div class="mm-tools"><button class="btn icon small" data-mm="fit" title="适应窗口"><svg><use href="#i-fit"/></svg></button></div>';
  const { root } = new Transformer().transform(md);
  const mm = Markmap.create(box.querySelector('svg'), { duration: 300, paddingX: 14, spacingVertical: 8, maxWidth: 260 }, root);
  box.querySelector('[data-mm="fit"]').addEventListener('click', () => mm.fit());
}

// 波形只是装饰：按录音 id 生成固定的高低起伏，同一条录音每次看到的一样。
function waveBars(seed) {
  let x = seed * 9301 + 49297;
  const rand = () => (x = (x * 9301 + 49297) % 233280) / 233280;
  let html = '';
  for (let i = 0; i < 88; i++) {
    const envelope = 0.55 + Math.sin(i / 7 + seed) * 0.25 + Math.sin(i / 2.3 + seed * 3) * 0.12;
    html += `<i style="height:${Math.round(Math.max(12, Math.min(100, envelope * (0.45 + rand() * 0.75) * 100)))}%"></i>`;
  }
  return html;
}

function fmtClock(secs) {
  const s = Math.max(0, Math.floor(secs || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

function updatePlayer() {
  const player = $('#player');
  if (!player || !state.record) return;
  const total = player.duration || state.record.duration_s || 0;
  $('#p-cur').textContent = fmtClock(player.currentTime);
  if (player.duration) $('#p-total').textContent = fmtClock(player.duration);
  $('#wave-done').style.clipPath = `inset(0 ${100 - (total ? player.currentTime / total * 100 : 0)}% 0 0)`;
  const playing = !player.paused && !player.ended;
  $('.player').classList.toggle('playing', playing);
  $('.player .play use').setAttribute('href', playing ? '#i-pause' : '#i-play');
}

function avatar(rec, spk) {
  const name = speakerName(rec, spk);
  const initial = rec.speaker_names[spk] ? [...name][0] : Number(spk) + 1;
  return `<span class="avatar spk-bg-${Number(spk) % 6}" title="${esc(name)}">${esc(initial)}</span>`;
}

function closeMenu() {
  $('.menu')?.remove();
}

function openMenu(anchor) {
  closeMenu();
  const rec = state.record;
  const menu = document.createElement('div');
  menu.className = 'menu';
  menu.innerHTML = `
    <button data-act="full" ${rec.busy ? 'disabled' : ''}>重新转写</button>
    <button data-act="summary" ${rec.busy || !rec.segments.length ? 'disabled' : ''}>重新生成纪要</button>
    <hr>
    <button data-act="export" ${rec.segments.length ? '' : 'disabled'}>导出 Markdown</button>
    <button data-act="copy-transcript" ${rec.segments.length ? '' : 'disabled'}>复制转写</button>
    <hr>
    <button data-act="delete" class="danger" ${rec.busy ? 'disabled' : ''}>删除录音</button>`;
  anchor.parentElement.appendChild(menu);
}

document.addEventListener('click', event => {
  if (!event.target.closest('.menu, [data-act="menu"]')) closeMenu();
});

function transcriptText(rec) {
  return rec.segments.map(s => `[${clock(s.start_ms, rec.recorded_at)}] ${s.speaker !== null ? speakerName(rec, s.speaker) + '：' : ''}${s.text}`).join('\n');
}

async function copy(text, what) {
  await navigator.clipboard.writeText(text);
  toast(`已复制${what}`);
}

const actions = {
  back() {
    document.body.classList.remove('show-detail');
  },
  toggle() {
    const player = $('#player');
    if (player.paused) {
      $('.player').classList.add('loading');
      return player.play().catch(e => {
        $('.player').classList.remove('loading');
        throw new Error(`无法播放：${e.message}`);
      });
    }
    player.pause();
  },
  menu(el) {
    $('.menu') ? closeMenu() : openMenu(el);
  },
  async full() {
    const rec = state.record;
    if (rec.segments.length && !confirm('重新转写会覆盖现有转写和说话人命名，确定继续？')) return;
    await api(`/api/recordings/${rec.id}/process`, { method: 'POST', body: { kind: 'full' } });
    toast('已加入处理队列');
    await Promise.all([loadList(), loadDetail()]);
  },
  async summary() {
    await api(`/api/recordings/${state.record.id}/process`, { method: 'POST', body: { kind: 'summary' } });
    toast('已开始生成纪要');
    await Promise.all([loadList(), loadDetail()]);
  },
  async names() {
    const names = Object.fromEntries($$('[data-spk]').map(input => [input.dataset.spk, input.value]));
    await api(`/api/recordings/${state.record.id}`, { method: 'PATCH', body: { speaker_names: names } });
    toast('已保存命名');
    state.signature = '';
    await loadDetail();
  },
  'copy-summary': () => copy(state.record.summary, '纪要'),
  'copy-transcript': () => copy(transcriptText(state.record), '转写'),
  export() {
    const rec = state.record;
    const mind = rec.mindmap ? `\n\n## 脑图\n\n${rec.mindmap.replace(/^#/gm, '###')}` : '';
    const md = `# ${displayTitle(rec)}\n\n${rec.summary || ''}${mind}\n\n## 转写\n\n${transcriptText(rec).split('\n').join('\n\n')}\n`;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
    link.download = `${displayTitle(rec)}.md`;
    link.click();
    URL.revokeObjectURL(link.href);
  },
  async delete() {
    if (!confirm('删除后，服务端的音频、转写和纪要都会移除；卡上的这段录音下次同步时会重新上传。确定删除？')) return;
    await api(`/api/recordings/${state.record.id}`, { method: 'DELETE' });
    state.current = null;
    state.record = null;
    $('#detail').innerHTML = placeholder();
    document.body.classList.remove('show-detail');
    toast('已删除');
    await loadList();
  },
};

$('#detail').addEventListener('click', run(async event => {
  const el = event.target.closest('[data-act], [data-tab], [data-seek], [data-rate]');
  if (!el) return;
  if (el.dataset.tab) {
    state.tab = el.dataset.tab;
    $$('#d-tabs button').forEach(b => b.classList.toggle('active', b === el));
    renderTab();
  } else if (el.dataset.seek) {
    const player = $('#player');
    player.currentTime = Number(el.dataset.seek) / 1000;
    await player.play();
  } else if (el.dataset.rate) {
    $('#player').playbackRate = Number(el.dataset.rate);
    $$('.speed button').forEach(b => b.classList.toggle('active', b === el));
  } else {
    closeMenu();
    await actions[el.dataset.act](el);
  }
}));

$('#detail').addEventListener('input', event => {
  if (event.target.id === 't-search') {
    state.transcriptQuery = event.target.value.trim();
    renderSegments();
  }
  if (event.target.id === 'follow') state.follow = event.target.checked;
});

$('#detail').addEventListener('change', run(async event => {
  if (event.target.id === 'follow') state.follow = event.target.checked;
  if (event.target.id !== 'title') return;
  const title = event.target.value.trim();
  if (!title || title === displayTitle(state.record)) return;
  await api(`/api/recordings/${state.record.id}`, { method: 'PATCH', body: { title } });
  toast('已修改标题');
  await Promise.all([loadList(), loadDetail()]);
}));

$('#detail').addEventListener('keydown', event => {
  if (event.target.id === 'title' && event.key === 'Enter') event.target.blur();
});

function placeholder() {
  return `<div class="empty hero-empty"><span class="logo xl"><svg><use href="#i-logo"/></svg></span><strong>选择一条录音</strong>查看纪要、脑图和转写 · 按 <kbd>/</kbd> 搜索</div>`;
}

// ── 设置 ──────────────────────────────────────────────────────────
const presets = { asr: [], llm: [] };
const presetFields = { asr: ['asr.openai_base_url', 'asr.openai_model', 'asr.openai_api_key'],
  llm: ['summary.base_url', 'summary.model', 'summary.api_key'] };
const SECRET_FIELDS = { 'asr.openai_api_key': 'has_openai_api_key', 'asr.doubao_api_key': 'has_doubao_api_key',
  'asr.doubao_access_key': 'has_doubao_access_key', 'summary.api_key': 'has_api_key' };
const form = $('#settings-form');
const field = name => form.elements[name];

function syncPreset(kind) {
  const url = field(presetFields[kind][0]).value.replace(/\/+$/, '');
  const index = presets[kind].findIndex(p => p.base_url === url);
  $(`[data-preset="${kind}"]`).value = String(index);
  const preset = presets[kind][index];
  const site = $(`[data-site="${kind}"]`);
  site.hidden = !preset?.site;
  if (preset?.site) site.href = preset.site;
  $(`#${kind}-models`).innerHTML = (preset?.models || []).map(m => `<option value="${esc(m)}">`).join('');
  $(`[data-pick="${kind}"]`).hidden = true; // 换了地址，之前拉取的列表不再适用
}

// 从厂商的 /models 拉取模型列表，用当前填写（未保存）的地址和密钥。
async function fetchModels(kind, button) {
  const [urlName, modelName, keyName] = presetFields[kind];
  button.disabled = true;
  try {
    const { models } = await api('/api/settings/models', { method: 'POST', body: {
      target: kind === 'llm' ? 'summary' : 'asr', base_url: field(urlName).value, api_key: field(keyName).value } });
    const pick = $(`[data-pick="${kind}"]`);
    const current = field(modelName).value;
    pick.innerHTML = `<option value="">从 ${models.length} 个可用模型中选择…</option>` +
      models.map(m => `<option value="${esc(m)}"${m === current ? ' selected' : ''}>${esc(m)}</option>`).join('');
    pick.hidden = false;
    $(`#${kind}-models`).innerHTML = models.map(m => `<option value="${esc(m)}">`).join('');
    toast(`获取到 ${models.length} 个模型`);
  } finally {
    button.disabled = false;
  }
}

function toggleProvider() {
  const provider = form.querySelector('[name="asr.provider"]:checked')?.value;
  $$('[data-show]', form).forEach(el => { el.hidden = el.dataset.show !== `asr.provider=${provider}`; });
}

async function loadSettings() {
  if (!presets.llm.length) {
    Object.assign(presets, await api('/api/presets'));
    for (const kind of ['asr', 'llm']) {
      const select = $(`[data-preset="${kind}"]`);
      select.innerHTML = '<option value="-1">自定义</option>' + presets[kind].map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join('');
      select.addEventListener('change', () => {
        const preset = presets[kind][Number(select.value)];
        if (!preset) return;
        field(presetFields[kind][0]).value = preset.base_url;
        field(presetFields[kind][1]).value = preset.models[0] || '';
        syncPreset(kind);
        markDirty();
      });
      field(presetFields[kind][0]).addEventListener('input', () => syncPreset(kind));
      const pick = $(`[data-pick="${kind}"]`);
      pick.addEventListener('change', () => {
        if (!pick.value) return;
        field(presetFields[kind][1]).value = pick.value;
        markDirty();
      });
      const button = $(`[data-models="${kind}"]`);
      button.addEventListener('click', run(() => fetchModels(kind, button)));
    }
  }
  const cfg = await api('/api/settings');
  for (const section of ['general', 'asr', 'summary']) {
    for (const [key, value] of Object.entries(cfg[section])) {
      const input = field(`${section}.${key}`);
      if (!input) continue;
      if (input instanceof RadioNodeList) input.value = value;
      else if (input.type === 'checkbox') input.checked = value;
      else if (input.type === 'password') input.value = '';
      else input.value = value;
    }
  }
  for (const [name, flag] of Object.entries(SECRET_FIELDS)) {
    const [section] = name.split('.');
    field(name).placeholder = cfg[section][flag] ? '已保存（留空保持不变）' : '未设置';
  }
  field('hotwords').value = cfg.hotwords.join('\n');
  field('corrections').value = cfg.corrections.map(c => `${c.from}=${c.to}`).join('\n');
  toggleProvider();
  syncPreset('asr');
  syncPreset('llm');
  $('#settings-msg').textContent = '';
}

function collectSettings() {
  const body = { general: {}, asr: {}, summary: {} };
  for (const input of form.querySelectorAll('[name*="."]')) {
    if (input.type === 'radio' && !input.checked) continue;
    const [section, key] = input.name.split('.');
    body[section][key] = input.type === 'checkbox' ? input.checked : input.value;
  }
  body.hotwords = field('hotwords').value.split('\n').map(s => s.trim()).filter(Boolean);
  body.corrections = field('corrections').value.split('\n').map(line => line.split('='))
    .filter(p => p.length === 2 && p[0].trim()).map(([from, to]) => ({ from: from.trim(), to: to.trim() }));
  return body;
}

async function saveSettings() {
  await api('/api/settings', { method: 'PUT', body: collectSettings() });
  await loadSettings();
}

function markDirty() {
  $('#settings-msg').textContent = '有未保存的修改';
}

form.addEventListener('input', markDirty);
form.addEventListener('change', event => {
  if (event.target.name === 'asr.provider') toggleProvider();
});

form.addEventListener('submit', run(async event => {
  event.preventDefault();
  await saveSettings();
  $('#settings-msg').textContent = '已保存，对之后的任务生效';
  toast('设置已保存');
}));

form.addEventListener('click', run(async event => {
  const test = event.target.closest('[data-test]');
  if (!test) return;
  const result = $(`[data-result="${test.dataset.test}"]`);
  test.disabled = true;
  result.className = 'test-result';
  result.textContent = '保存并测试中…';
  try {
    await saveSettings();
    const r = await api('/api/settings/test', { method: 'POST', body: { target: test.dataset.test } });
    result.className = 'test-result ' + (r.ok ? 'ok' : 'fail');
    result.textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
  } finally {
    test.disabled = false;
  }
}));

// ── 导航与启动 ────────────────────────────────────────────────────
function loadIntegrate() {
  const url = `${location.origin}/mcp`;
  $('#mcp-url').value = url;
  $('#plugin-url').textContent = location.origin;
  $('#mcp-cmd').textContent = `claude mcp add --transport http wenji ${url} \\\n  --header "Authorization: Bearer <你的令牌>"`;
  $('#mcp-json').textContent = JSON.stringify({ mcpServers: { wenji: {
    type: 'http', url, headers: { Authorization: 'Bearer <你的令牌>' } } } }, null, 2);
  $('#mcp-curl').textContent = `curl -s ${url} \\\n  -H "Authorization: Bearer <你的令牌>" \\\n` +
    `  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \\\n` +
    `  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`;
}

// 设置页和接入页里的复制按钮：输入框取 value，代码块取文本。
document.addEventListener('click', run(async event => {
  const btn = event.target.closest('[data-copy]');
  if (!btn) return;
  const el = $(btn.dataset.copy);
  await copy(el.value ?? el.textContent, '');
}));

function navigate(view) {
  $$('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#view-library').hidden = view !== 'library';
  $('#view-settings').hidden = view !== 'settings';
  $('#view-integrate').hidden = view !== 'integrate';
  if (view === 'integrate') loadIntegrate();
  document.body.classList.remove('show-detail');
  if (view === 'settings') loadSettings().catch(e => toast(e.message, { error: true }));
}

$$('[data-view]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.view)));

async function tick() {
  if (document.hidden || $('#view-library').hidden || !$('#login').hidden) return;
  await loadList();
  if (state.record?.busy) await loadDetail();
}

async function start() {
  try {
    await loadList();
  } catch {
    return; // 未登录时 api() 已切换到登录页
  }
  $('#login').hidden = true;
  $('#app').hidden = false;
  if (!state.record) $('#detail').innerHTML = placeholder();
  clearInterval(state.timer);
  state.timer = setInterval(run(tick), 4000);
}

// 页面在后台时不轮询，切回前台立即刷新一次。
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.timer) run(tick)();
});

start();
