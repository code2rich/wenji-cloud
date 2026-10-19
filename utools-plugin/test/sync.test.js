'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { CHUNK, findCards, listCardFiles, Client, pickServer, syncFiles } = require('../lib/sync');

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'wenji-'));
}

/** 按服务端协议实现的内存假服务端，可模拟分片写入后响应丢失。 */
function fakeServer({ synced = [], dropResponseOnce = false, dropFinalResponse = false } = {}) {
  const recordings = new Map(synced.map(k => [k, 1]));
  const uploads = new Map();
  const calls = [];
  let dropped = !dropResponseOnce;
  const json = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const fetchImpl = async (url, init) => {
    const route = url.replace('http://srv', '');
    calls.push(`${init.method} ${route}`);
    if (init.headers.Authorization !== 'Bearer tok') return json(401, { detail: '未登录' });
    if (route === '/api/sync/check') {
      const { items } = JSON.parse(init.body);
      return json(200, { results: items.map(i => {
        const key = `${i.name}:${i.size}`;
        if (recordings.has(key)) return { ...i, state: 'synced' };
        const up = [...uploads.entries()].find(([, u]) => u.key === key);
        return up ? { ...i, state: 'partial', upload_id: up[0], offset: up[1].data.length } : { ...i, state: 'missing' };
      }) });
    }
    if (route === '/api/uploads') {
      const { name, size } = JSON.parse(init.body);
      const key = `${name}:${size}`;
      let id = [...uploads.entries()].find(([, u]) => u.key === key)?.[0];
      if (!id) {
        id = `u${uploads.size + 1}`;
        uploads.set(id, { key, size, data: Buffer.alloc(0) });
      }
      return json(200, { state: 'partial', upload_id: id, offset: uploads.get(id).data.length });
    }
    const m = route.match(/^\/api\/uploads\/(\w+)$/);
    if (m) {
      const up = uploads.get(m[1]);
      if (!up) return json(404, { detail: '上传会话不存在，请重新比对' });
      const offset = Number(init.headers['Upload-Offset']);
      if (offset !== up.data.length) return json(409, { detail: { message: '偏移不一致', offset: up.data.length } });
      up.data = Buffer.concat([up.data, init.body]);
      if (!dropped) {
        dropped = true;
        throw new TypeError('fetch failed'); // 已写入但客户端没收到响应
      }
      if (up.data.length === up.size) {
        recordings.set(up.key, up.data);
        uploads.delete(m[1]);
        if (dropFinalResponse) {
          dropFinalResponse = false;
          throw new TypeError('fetch failed'); // 已入库但客户端没收到响应
        }
        return json(200, { offset: up.data.length, complete: true, recording_id: recordings.size });
      }
      return json(200, { offset: up.data.length, complete: false });
    }
    return json(404, {});
  };
  return { fetchImpl, recordings, calls };
}

test('findCards 只识别含 record 目录的卷', () => {
  const vols = tmpdir();
  fs.mkdirSync(path.join(vols, 'NO NAME', 'record'), { recursive: true });
  fs.mkdirSync(path.join(vols, 'Backup'));
  fs.mkdirSync(path.join(vols, '.timemachine', 'record'), { recursive: true });
  fs.mkdirSync(path.join(vols, 'Macintosh HD', 'record'), { recursive: true });
  assert.deepStrictEqual(findCards(vols).map(c => c.name), ['NO NAME']);
  assert.deepStrictEqual(findCards(path.join(vols, 'missing')), []);
});

test('listCardFiles 同一录音只取首选格式', () => {
  const dir = tmpdir();
  for (const [name, size] of [['a.opus', 2], ['a.wav', 32], ['b.wav', 16], ['c.mp3', 3], ['._a.opus', 1], ['d.txt', 1], ['e.opus', 0]]) {
    fs.writeFileSync(path.join(dir, name), Buffer.alloc(size));
  }
  assert.deepStrictEqual(listCardFiles(dir, 'opus').map(f => f.name), ['a.opus', 'b.wav', 'c.mp3']);
  assert.deepStrictEqual(listCardFiles(dir, 'wav').map(f => f.name), ['a.wav', 'b.wav', 'c.mp3']);
});

test('syncFiles 跳过服务端已有的文件，只上传缺失的', async () => {
  const dir = tmpdir();
  fs.writeFileSync(path.join(dir, 'old.opus'), Buffer.alloc(10, 1));
  fs.writeFileSync(path.join(dir, 'new.opus'), Buffer.alloc(CHUNK + 5, 2));
  const srv = fakeServer({ synced: ['old.opus:10'] });
  const client = new Client({ server: 'http://srv/', token: 'tok', fetchImpl: srv.fetchImpl });
  const events = [];
  const result = await syncFiles({ client, files: listCardFiles(dir), onEvent: e => events.push(e) });
  assert.deepStrictEqual(result.uploaded, ['new.opus']);
  const last = events.filter(e => e.type === 'progress').at(-1);
  assert.strictEqual(last.done, CHUNK + 5); // 最后一片也要报告进度，进度条能到 100%
  assert.strictEqual(result.skipped, 1);
  assert.deepStrictEqual(srv.recordings.get(`new.opus:${CHUNK + 5}`), Buffer.alloc(CHUNK + 5, 2));
  assert.ok(!srv.calls.some(c => c.includes('old.opus')));
  const { results, ...checked } = events[0];
  assert.deepStrictEqual(checked, { type: 'checked', total: 2, synced: 1, pending: 1 });
  assert.deepStrictEqual(results.map(r => r.state), ['missing', 'synced']);

  // 再同步一次：全部跳过，不发起上传
  const again = await syncFiles({ client, files: listCardFiles(dir) });
  assert.deepStrictEqual(again.uploaded, []);
  assert.strictEqual(again.skipped, 2);
});

test('分片已写入但响应丢失时，按服务端偏移继续而不重复写入', async () => {
  const dir = tmpdir();
  const data = Buffer.alloc(CHUNK * 2 + 3, 7);
  fs.writeFileSync(path.join(dir, 'x.wav'), data);
  const srv = fakeServer({ dropResponseOnce: true });
  const client = new Client({ server: 'http://srv', token: 'tok', fetchImpl: srv.fetchImpl });
  const realSetTimeout = global.setTimeout;
  global.setTimeout = (fn) => realSetTimeout(fn, 0); // 跳过重试等待
  try {
    const result = await syncFiles({ client, files: listCardFiles(dir) });
    assert.deepStrictEqual(result.failed, []);
  } finally {
    global.setTimeout = realSetTimeout;
  }
  assert.deepStrictEqual(srv.recordings.get(`x.wav:${data.length}`), data);
});

test('最后一片已入库但响应丢失时，重新比对确认完成而不是报失败', async () => {
  const dir = tmpdir();
  const data = Buffer.alloc(CHUNK + 5, 3);
  fs.writeFileSync(path.join(dir, 'y.wav'), data);
  const srv = fakeServer({ dropFinalResponse: true });
  const client = new Client({ server: 'http://srv', token: 'tok', fetchImpl: srv.fetchImpl });
  const realSetTimeout = global.setTimeout;
  global.setTimeout = (fn) => realSetTimeout(fn, 0);
  try {
    const result = await syncFiles({ client, files: listCardFiles(dir) });
    assert.deepStrictEqual(result.failed, []);
    assert.deepStrictEqual(result.uploaded, ['y.wav']);
  } finally {
    global.setTimeout = realSetTimeout;
  }
  assert.deepStrictEqual(srv.recordings.get(`y.wav:${data.length}`), data);
});

test('令牌错误时停止并给出明确原因', async () => {
  const dir = tmpdir();
  fs.writeFileSync(path.join(dir, 'a.opus'), Buffer.alloc(4));
  const srv = fakeServer();
  const client = new Client({ server: 'http://srv', token: 'bad', fetchImpl: srv.fetchImpl });
  await assert.rejects(syncFiles({ client, files: listCardFiles(dir) }), /访问令牌错误/);
  assert.throws(() => new Client({ server: '', token: '' }), /服务地址/);
});

test('pickServer 当前地址网络不通时切换到其他已保存地址，令牌错误则不切换', async () => {
  const fetchImpl = async (url, init) => {
    if (url.startsWith('http://lan')) throw new TypeError('fetch failed');
    const ok = init.headers.Authorization === 'Bearer tok';
    return { ok, status: ok ? 200 : 401, json: async () => ({}) };
  };
  const servers = ['http://lan:8080', 'https://wan.example.com'];
  assert.strictEqual(await pickServer({ server: 'http://lan:8080', servers, token: 'tok', fetchImpl }), 'https://wan.example.com');
  assert.strictEqual(await pickServer({ server: 'https://wan.example.com', servers, token: 'tok', fetchImpl }), 'https://wan.example.com');
  await assert.rejects(pickServer({ server: 'https://wan.example.com', servers, token: 'bad', fetchImpl }), /令牌错误/);
  await assert.rejects(pickServer({ server: 'http://lan:8080', servers: ['http://lan:8080'], token: 'tok', fetchImpl }), /无法连接/);
});

test('Client 调用 fetch 时不把自身当作 this（浏览器 fetch 会报 Illegal invocation）', async () => {
  const fetchImpl = function () {
    if (this instanceof Client) throw new TypeError("Failed to execute 'fetch' on 'Window': Illegal invocation");
    return { ok: true, status: 200, json: async () => ({ ok: 1 }) };
  };
  const client = new Client({ server: 'http://srv', token: 'tok', fetchImpl });
  assert.deepStrictEqual(await client.request('GET', '/api/recordings'), { ok: 1 });
});
