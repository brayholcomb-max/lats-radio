// LATS Radio listening log. Each player posts a snapshot of the day for its listener (bray, matthew or unknown):
// what was heard, finished, rated, notes, and where playback stopped. The nightly task reads /report with the read key.
const ORIGIN = 'https://brayholcomb-max.github.io';
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const WHO = ['bray', 'matthew', 'unknown'];
const NAME = { bray: 'BRAY', matthew: 'MATTHEW', unknown: 'UNKNOWN LISTENER (device not yet tagged)' };
const clean = (s, n) => String(s == null ? '' : s).slice(0, n);
const num = v => (Number.isFinite(+v) && +v >= 0 ? Math.round(+v) : 0);
const mmss = s => Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');

function merge(old, snap, who) {
  const out = { date: snap.d, who, updated: new Date().toISOString(), speed: snap.sp || (old && old.speed) || 1,
    notes: clean(snap.n, 2000), at: clean(snap.c, 80) || (old && old.at) || '', atSec: snap.c ? num(snap.ct) : (old && old.atSec) || 0,
    allDone: !!snap.fin || !!(old && old.allDone), items: [] };
  const prev = {}; ((old && old.items) || []).forEach(x => { prev[x.id] = x; });
  (snap.items || []).slice(0, 40).forEach(x => {
    const id = clean(x.id, 80); if (!id) return;
    const p = prev[id] || {};
    out.items.push({ id, title: clean(x.t, 120), kind: x.k === 'lesson' ? 'lesson' : 'clip', url: clean(x.u, 300),
      seconds: num(x.L), heard: Math.max(num(x.h), p.heard || 0), finished: !!x.f || !!p.finished,
      rating: x.r === 'useful' || x.r === 'not useful' ? x.r : x.r === '' ? '' : p.rating || '' });
  });
  return out;
}

const heardMin = d => Math.round(d.items.reduce((a, x) => a + Math.min(x.heard, x.seconds || x.heard), 0) / 60);
const totalMin = d => Math.round(d.items.reduce((a, x) => a + (x.seconds || 0), 0) / 60);

function dayText(d) {
  const heard = d.items.filter(x => x.heard >= 20);
  const i = d.items.findIndex(x => x.id === d.at);
  const stop = d.allDone ? 'played to the end of the queue' : i >= 0 ? `stopped at item ${i + 1} of ${d.items.length} at ${mmss(d.atSec)}` : 'no position yet';
  const lines = [`  ${d.date}: heard about ${heardMin(d)} of ${totalMin(d)} min (${heard.length} of ${d.items.length} items touched), ${stop}, speed ${d.speed}x, last update ${d.updated}`];
  d.items.forEach((x, n) => {
    const pct = x.seconds ? Math.min(100, Math.round(100 * x.heard / x.seconds)) : 0;
    const state = x.finished ? 'FINISHED' : x.heard >= 20 ? `PARTLY HEARD ${pct}%` : 'NOT HEARD';
    const title = x.kind === 'lesson' && !/^lesson/i.test(x.title) ? 'Lesson: ' + x.title : x.title;
    lines.push(`    ${n + 1}. [${state}${x.rating ? ', rated ' + x.rating : ''}] ${title} (${x.id}, ${Math.round(x.seconds / 60)} min) ${x.url}`);
  });
  if (d.notes) lines.push(`    Notes: ${d.notes}`);
  return lines.join('\n');
}

function report(days) {
  if (!days.length) return 'No listening reported yet.';
  const out = [];
  for (const who of WHO) {
    const mine = days.filter(d => d.who === who).sort((a, b) => b.date.localeCompare(a.date));
    if (!mine.length) { if (who !== 'unknown') out.push(`${NAME[who]}: no listening reported in this window.`); continue; }
    const recent = mine.slice(0, 5).map(d => `${d.date} ${heardMin(d)} min`).join(', ');
    const withAny = mine.filter(d => heardMin(d) > 0).slice(0, 3);
    const avg = withAny.length ? Math.round(withAny.reduce((a, d) => a + heardMin(d), 0) / withAny.length) : 0;
    out.push(`${NAME[who]}: minutes heard per Radio day: ${recent}. Average of the last ${withAny.length} days with any listening: ${avg} min.\n` + mine.map(dayText).join('\n'));
  }
  return out.join('\n\n');
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const cors = { 'Access-Control-Allow-Origin': ORIGIN, 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type' };
    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    if (req.method === 'POST' && url.pathname === '/hit') {
      const o = req.headers.get('Origin');
      if (o && o !== ORIGIN && !o.startsWith('http://127.0.0.1') && !o.startsWith('http://localhost')) return new Response('origin', { status: 403 });
      const body = await req.text();
      if (body.length > 20000) return new Response('too big', { status: 413, headers: cors });
      let snap; try { snap = JSON.parse(body); } catch { return new Response('json', { status: 400, headers: cors }); }
      if (!DATE.test(snap.d || '')) return new Response('date', { status: 400, headers: cors });
      const who = WHO.includes(snap.w) ? snap.w : 'unknown';
      const key = `day:${snap.d}:${who}`;
      const old = await env.LOG.get(key, 'json');
      await env.LOG.put(key, JSON.stringify(merge(old, snap, who)), { expirationTtl: 60 * 60 * 24 * 120 });
      return new Response('ok', { headers: cors });
    }
    if (req.method === 'GET' && (url.pathname === '/report' || url.pathname === '/report.json')) {
      if (url.searchParams.get('k') !== env.READ_KEY) return new Response('key', { status: 403 });
      const n = Math.min(30, Math.max(1, +url.searchParams.get('days') || 7));
      const list = await env.LOG.list({ prefix: 'day:' });
      const dates = [...new Set(list.keys.map(k => k.name.split(':')[1]))].sort().reverse().slice(0, n);
      const keys = list.keys.map(k => k.name).filter(k => dates.includes(k.split(':')[1]));
      const days = (await Promise.all(keys.map(async k => { const d = await env.LOG.get(k, 'json'); if (d && !d.who) d.who = 'unknown'; return d; }))).filter(Boolean);
      if (url.pathname === '/report.json') return Response.json(days);
      return new Response('LATS Radio listening report, per listener, newest first. Bray is the one the queue is built for.\n\n' + report(days) + '\n', { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
    }
    return new Response('LATS Radio log', { status: 404 });
  },
};
