'use strict';

/* 书中有音 · 前端应用
   设计语言见 static/style.css，本文件为交互与数据层实现。 */

const $ = (id) => document.getElementById(id);
const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

/* 飞牛中继访问时路径形如 /app/shuzhongyouyin/，接口与静态资源需带此前缀 */
const APP_BASE = (function () {
  const m = window.location.pathname.match(/^(\/app\/[^/]+)(?:\/|$)/);
  return m ? m[1] : '';
})();

function appUrl(path) {
  if (!path) return path;
  return APP_BASE && path.charAt(0) === '/' ? APP_BASE + path : path;
}

const audio = $('audio');
const bgmAudio = $('bgm-audio');

const state = {
  token: localStorage.getItem('sy_token') || '',
  user: null,
  users: [],
  books: [],
  bookMap: {},
  roots: [],
  extraRoots: [],
  scannedAt: 0,
  statusSummary: {},
  scanning: false,
  scanError: '',
  scanTimer: null,
  query: '',
  filter: 'all',
  layout: localStorage.getItem('sy_layout') || 'grid',
  view: 'home',
  detailCache: {},
  bookId: null,
  current: null,          // 当前播放书籍详情
  chapter: 0,
  playing: false,
  rate: parseFloat(localStorage.getItem('sy_rate') || '1') || 1,
  sleepMin: parseInt(localStorage.getItem('sy_sleep') || '0', 10) || 0,
  sleepTimer: null,
  saveTimer: null,
  playlists: [],
  playlistId: null,
  publicUrl: '',
  scrapeBase: '',
  fnos: { enabled: false, endpoint: '' },
  bgm: {
    enabled: localStorage.getItem('sy_bgm_enabled') === '1',
    volume: parseFloat(localStorage.getItem('sy_bgm_volume') || '0.15'),
    current: localStorage.getItem('sy_bgm_current') || '',
    list: [],
    dir: '',
    open: localStorage.getItem('sy_bgm_open') === '1',
  },
  queue: [],
  queueIndex: -1,
  theme: localStorage.getItem('sy_theme') || 'light',
  dirRoles: [],
  scanReport: { scanned: [], skipped: [], duplicates: 0 },
  subtitle: { key: '', status: 'idle', format: '', encoding: '', segments: [], error: '' },
  textOn: localStorage.getItem('sy_text') !== '0',
  chapterGroupBook: '',
  chapterGroup: 0,
  bookShown: null,
  outroDone: '',
};

/* ------------------------------------------------------------------ 工具 */

function esc(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
    ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function fmtSize(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function toast(text, ms) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), ms || 2400);
}

function hash32(text) {
  let h = 2166136261;
  for (let i = 0; i < String(text).length; i += 1) {
    h ^= String(text).charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h);
}

function palette(seed) {
  const h = hash32(seed) % 360;
  return {
    c1: 'hsl(' + h + ',34%,34%)',
    c2: 'hsl(' + ((h + 34) % 360) + ',38%,16%)',
  };
}

function glyphOf(title) {
  const text = String(title || '书').trim();
  for (const ch of text) {
    if (/[\u4e00-\u9fa5A-Za-z0-9]/.test(ch)) return ch;
  }
  return '书';
}

function coverHTML(book, extraClass, badge) {
  const p = palette(book.id || book.title || 'book');
  const img = book.cover_url
    ? '<img class="art" loading="lazy" src="' + esc(appUrl(book.cover_url)) + '" alt="" onerror="this.remove()">'
    : '';
  const b = badge ? '<span class="cover-badge">' + esc(badge) + '</span>' : '';
  const fav = book.favorite
    ? '<span class="fav-mark"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 4.6l2.5 5 5.5.8-4 3.9 1 5.5-5-2.7-5 2.7 1-5.5-4-3.9 5.5-.8z"/></svg></span>'
    : '';
  return '<div class="cover ' + (extraClass || '') + '">' +
    '<span class="cover-art" style="--c1:' + p.c1 + ';--c2:' + p.c2 + '"><span>' + esc(glyphOf(book.title)) + '</span></span>' +
    img + b + fav + '</div>';
}

function progressOf(book) {
  const pr = book.progress || {};
  const percent = Number(pr.percent || 0);
  return { percent: Math.max(0, Math.min(100, percent)), chapter: pr.chapter || 0 };
}

function api(path, options) {
  const opts = Object.assign({}, options || {});
  const timeoutMs = Number(opts.timeoutMs || 0);
  delete opts.timeoutMs;
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (state.token) opts.headers.Authorization = 'Bearer ' + state.token;
  let timer = null;
  if (timeoutMs > 0 && typeof AbortController !== 'undefined') {
    const controller = new AbortController();
    opts.signal = controller.signal;
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  const clear = () => { if (timer) { clearTimeout(timer); timer = null; } };
  const request = fetch(appUrl(path), opts).then((res) => res.json().then((data) => {
    if (res.status === 401 && path.indexOf('/api/auth/login') < 0) {
      onUnauthorized();
      throw new Error('登录已过期');
    }
    if (!res.ok) throw new Error((data && data.error) || ('请求失败 ' + res.status));
    return data;
  }));
  return request.then((value) => { clear(); return value; }, (err) => { clear(); throw err; });
}

function bookById(id) {
  return state.bookMap[id] || null;
}

/* ------------------------------------------------------------------ 主题 */

function applyTheme(theme) {
  state.theme = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', state.theme);
  localStorage.setItem('sy_theme', state.theme);
  const label = $('theme-label');
  if (label) label.textContent = state.theme === 'dark' ? '深色' : '浅色';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', state.theme === 'dark' ? '#101211' : '#f5f3ee');
}

/* ------------------------------------------------------------------ 登录 */

function showLogin(message) {
  $('login').classList.remove('hidden');
  $('app').classList.add('hidden');
  $('boot').classList.add('hidden');
  if (message) {
    $('login-error').textContent = message;
    $('login-error').classList.remove('hidden');
  }
  setTimeout(() => { const u = $('login-user'); if (u) u.focus(); }, 80);
}

function hideLogin() {
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  $('login-error').classList.add('hidden');
}

function onUnauthorized() {
  state.token = '';
  state.user = null;
  localStorage.removeItem('sy_token');
  stopScanWatch();
  showLogin('登录已过期，请重新登录');
}

function doLogin(ev) {
  if (ev) ev.preventDefault();
  const username = $('login-user').value.trim();
  const password = $('login-pass').value;
  if (!username || !password) {
    $('login-error').textContent = '请输入账号和密码';
    $('login-error').classList.remove('hidden');
    return;
  }
  const btn = $('login-go');
  btn.disabled = true;
  fetch(appUrl('/api/auth/login'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: username, password: password }),
  }).then((res) => res.json().then((data) => {
    if (!res.ok || !data.token) throw new Error((data && data.error) || '登录失败');
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem('sy_token', data.token);
    $('login-pass').value = '';
    hideLogin();
    toast('欢迎回来，' + (data.user.username || username));
    startApp();
  })).catch((err) => {
    $('login-error').textContent = err.message || '登录失败';
    $('login-error').classList.remove('hidden');
  }).then(() => { btn.disabled = false; });
}

function logout() {
  api('/api/auth/logout', { method: 'POST', body: '{}' }).catch(() => {}).then(() => {
    state.token = '';
    state.user = null;
    state.books = [];
    state.bookMap = {};
    state.playlists = [];
    state.current = null;
    audio.pause();
    audio.removeAttribute('src');
    bgmAudio.pause();
    bgmAudio.removeAttribute('src');
    localStorage.removeItem('sy_token');
    stopScanWatch();
    $('mini').classList.add('hidden');
    showLogin('');
  });
}

function paintUser() {
  const u = state.user || {};
  const avatar = u.avatar_url ? appUrl(u.avatar_url) :
    'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#20564b"/>' +
      '<circle cx="32" cy="26" r="11" fill="#9fd8c6"/><path d="M12 58c4-11 11-16 20-16s16 5 20 16" fill="#9fd8c6"/></svg>');
  $('rail-avatar').src = avatar;
  $('rail-name').textContent = u.username || '未登录';
  $('rail-role').textContent = u.role === 'admin' ? '管理员' : '书友';
}

/* ------------------------------------------------------------------ 数据加载 */

function loadLibrary() {
  /* 25 秒超时：FUSE 挂死时页面不再无限转圈，改为提示并继续轮询扫描状态 */
  return api('/api/library', { timeoutMs: 25000 }).then((data) => {
    state.books = data.books || [];
    state.bookMap = {};
    state.books.forEach((b) => { state.bookMap[b.id] = b; });
    state.roots = data.roots || [];
    state.scannedAt = data.scanned_at || 0;
    state.statusSummary = data.platform || {};
    state.scanning = !!data.scanning;
    state.scanError = data.error || '';
    state.scanReport = {
      scanned: data.roots_scanned || [],
      skipped: data.roots_skipped || [],
      duplicates: data.duplicates || 0,
    };
    syncScanNote();
    return data;
  });
}

function loadSettings() {
  return api('/api/settings').then((data) => {
    state.extraRoots = data.extra_roots || [];
    state.dirRoles = data.dir_roles || state.dirRoles;
    if (data.scan_report) state.scanReport = data.scan_report;
    state.roots = data.roots || state.roots;
    state.publicUrl = data.public_url || '';
    state.bgm.dir = data.bgm_dir || '';
    state.scrapeBase = (data.scrape && data.scrape.api_base) || '';
    state.fnos = data.fnos_login || state.fnos;
    return data;
  });
}

function loadPlaylists() {
  return api('/api/playlists').then((data) => {
    state.playlists = data.playlists || data.data || [];
    return state.playlists;
  }).catch(() => { state.playlists = []; });
}

function loadBgm() {
  return api('/api/bgm/list').then((data) => {
    state.bgm.dir = data.dir || state.bgm.dir;
    state.bgm.list = data.bgm || [];
    /* 当前曲目已被删除/移走时清掉记忆，避免点"选用"落到一个不存在的地址 */
    if (state.bgm.current && !state.bgm.list.some((t) => t.url === state.bgm.current)) {
      state.bgm.current = '';
      localStorage.removeItem('sy_bgm_current');
    }
    if (!state.bgm.current && state.bgm.list.length) {
      state.bgm.current = state.bgm.list[0].url;
    }
    return state.bgm;
  }).catch(() => state.bgm);
}

function loadUsers() {
  return api('/api/users').then((data) => {
    state.users = data.users || [];
    if (data.current) state.user = data.current;
    paintUser();
    return state.users;
  }).catch(() => state.users);
}

function detailOf(id) {
  if (state.detailCache[id]) return Promise.resolve(state.detailCache[id]);
  return api('/api/book/' + encodeURIComponent(id)).then((data) => {
    state.detailCache[id] = data;
    return data;
  });
}

/* ------------------------------------------------------------------ 扫描 */

function syncScanNote() {
  const note = $('scan-note');
  if (!note) return;
  if (state.scanning) {
    note.classList.remove('hidden');
    $('scan-text').textContent = '正在扫描书库…';
  } else if (state.scanError) {
    note.classList.remove('hidden');
    $('scan-text').textContent = '扫描失败：' + state.scanError;
  } else {
    note.classList.add('hidden');
  }
}

function startScanWatch() {
  stopScanWatch();
  state.scanTimer = setInterval(() => {
    api('/api/scan/status').then((data) => {
      const was = state.scanning;
      state.scanning = !!data.scanning;
      state.scanError = data.error || '';
      syncScanNote();
      if (was && !state.scanning) {
        loadLibrary().then(() => { render(); toast('书库已更新'); });
      } else if (!state.scanning && !state.books.length) {
        /* 首屏加载超时/失败后的补偿：每 30 秒重试一次，避免无限转圈 */
        const now = Date.now();
        if (!state.libraryRetryAt || now - state.libraryRetryAt > 30000) {
          state.libraryRetryAt = now;
          loadLibrary().then(() => render()).catch(() => {});
        }
      }
    }).catch(() => {});
  }, 3000);
}

function stopScanWatch() {
  if (state.scanTimer) clearInterval(state.scanTimer);
  state.scanTimer = null;
}

function rescan() {
  api('/api/scan', { method: 'POST', body: '{}' }).then((data) => {
    state.scanning = true;
    syncScanNote();
    startScanWatch();
    toast('已开始重新扫描');
  }).catch((err) => toast(err.message));
}

/* ------------------------------------------------------------------ 路由 */

function route() {
  const raw = (window.location.hash || '#/home').replace(/^#\/?/, '');
  const parts = raw.split('/').filter(Boolean);
  return { name: parts[0] || 'home', arg: parts[1] || '' };
}

function go(hash) {
  if (window.location.hash === hash) render();
  else window.location.hash = hash;
}

function render() {
  const r = route();
  state.view = r.name;
  if (r.name !== 'book') state.chapterGroupBook = '';
  document.body.classList.toggle('on-player', r.name === 'player');
  if (r.name !== 'player' && state.current && $('mini').classList.contains('hidden')) {
    showMini(state.current);
    syncMini();
  }
  $$('#nav .nav-item').forEach((el) => {
    el.classList.toggle('active', el.getAttribute('data-nav') === r.name ||
      (r.name === 'book' && el.getAttribute('data-nav') === 'library') ||
      (r.name === 'player' && el.getAttribute('data-nav') === 'library') ||
      (r.name === 'playlist' && el.getAttribute('data-nav') === 'playlists'));
  });
  const view = $('view');
  switch (r.name) {
    case 'library': view.innerHTML = viewLibrary(); break;
    case 'favorites': view.innerHTML = viewFavorites(); break;
    case 'playlists': view.innerHTML = viewPlaylists(); break;
    case 'playlist': view.innerHTML = viewPlaylistDetail(r.arg); break;
    case 'book': renderBook(r.arg); return;
    case 'player': view.innerHTML = viewPlayer(); syncPlayerUI(); ensureSubtitle(); return;
    case 'mine': view.innerHTML = viewMine(); ensureMineData(); return;
    default: view.innerHTML = viewHome();
  }
  scrollTop();
}

function scrollTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ------------------------------------------------------------------ 首页 */

function authorLabel(book) {
  const raw = String((book && book.author) || '').trim();
  if (!raw) return '';
  const cut = raw.split(/[。．，,；;、\n|]/)[0].trim();
  return cut.slice(0, 20);
}

function viewHome() {
  const recent = recentBooks();
  const hero = recent[0] || null;
  const favCount = state.books.filter((b) => b.favorite).length;
  const chapterCount = state.books.reduce((sum, b) => sum + (b.chapter_count || 0), 0);
  const heroHTML = hero ? heroCard(hero) : '';
  const stats = '<div class="stats">' +
    '<div class="stat"><b>' + state.books.length + '</b><span>本书</span></div>' +
    '<div class="stat"><b>' + chapterCount + '</b><span>章节目录</span></div>' +
    '<div class="stat"><b>' + favCount + '</b><span>收藏</span></div>' +
    '<div class="stat"><b>' + state.playlists.length + '</b><span>书单</span></div>' +
    '</div>';
  const recentList = recent.slice(hero ? 1 : 0, 6).map((b) =>
    '<div class="list-row" data-act="open-book" data-id="' + esc(b.id) + '" style="cursor:pointer">' +
    '<div class="list-thumb" style="--c1:' + palette(b.id).c1 + ';--c2:' + palette(b.id).c2 + ';position:relative">' + esc(glyphOf(b.title)) +
    (b.cover_url ? '<img src="' + esc(appUrl(b.cover_url)) + '" alt="" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.remove()">' : '') +
    '</div>' +
    '<div class="grow"><div class="name">' + esc(b.title) + '</div><div class="sub">' +
    (b.author ? esc(authorLabel(b)) + ' · ' : '') + (b.chapter_count || 0) + ' 章 · 已听 ' + progressOf(b).percent + '%</div></div>' +
    '<span class="tail">' + fmtSize(b.size) + '</span></div>'
  ).join('');
  const plRows = state.playlists.slice(0, 3).map((p) =>
    '<div class="list-row" data-act="open-playlist" data-id="' + esc(p.id) + '" style="cursor:pointer">' +
    '<div class="list-thumb" style="--c1:#b08a3e;--c2:#6d5323">' + esc(glyphOf(p.title)) + '</div>' +
    '<div class="grow"><div class="name">' + esc(p.title) + '</div><div class="sub">' + (p.book_count || 0) + ' 本 · 更新于 ' + esc(fmtDate(tsOf(p.updated_at))) + '</div></div>' +
    '<span class="tail">全部播放</span></div>'
  ).join('');

  if (!state.books.length) {
    return '<div class="page-title">今天想听点什么</div>' +
      '<div class="page-sub">书库还没有内容，先在「我的 → 书库」里授权存放有声书的目录。</div>' +
      emptyState('书库还是空的', '授权目录后点重新扫描，扫描完成即可在这里看到书籍。', '去设置',
        'data-act="goto-mine"') ;
  }

  return '<div class="page-title">今天想听点什么</div>' +
    '<div class="page-sub">' + (hero ? '接着上次的地方继续，或者从最近加入的书里挑一本。' : '书库已就绪，挑一本开始。') + '</div>' +
    '<div class="section">' + heroHTML + '</div>' +
    '<div class="section">' + stats + '</div>' +
    '<div class="section"><div class="section-head"><div class="section-title">最近添加</div>' +
    '<a class="section-link" href="#/library">全部书库 →</a></div><div class="list">' +
    (recentList || '<div style="padding:18px" class="hint">暂无更多书籍</div>') + '</div></div>' +
    '<div class="section"><div class="section-head"><div class="section-title">我的书单</div>' +
    '<a class="section-link" href="#/playlists">管理书单 →</a></div><div class="list">' +
    (plRows || '<div style="padding:18px" class="hint">还没有书单，去「书单」页新建一个。</div>') + '</div></div>';
}

function tsOf(text) {
  if (!text) return 0;
  const d = new Date(text);
  return isNaN(d.getTime()) ? 0 : d.getTime() / 1000;
}

function recentBooks() {
  const listening = state.books.filter((b) => (b.progress && b.progress.updated));
  listening.sort((a, b) => (b.progress.updated || 0) - (a.progress.updated || 0));
  const others = state.books.slice().sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  const ids = {};
  const out = [];
  listening.forEach((b) => { if (!ids[b.id]) { ids[b.id] = 1; out.push(b); } });
  others.forEach((b) => { if (!ids[b.id]) { ids[b.id] = 1; out.push(b); } });
  return out;
}

function heroCard(book) {
  const pr = progressOf(book);
  const chapterTitle = (book.chapters && book.chapters[pr.chapter]) ? book.chapters[pr.chapter].title : '';
  const p = palette(book.id);
  return '<div class="hero">' +
    '<div class="hero-main">' +
    '<div class="eyebrow">继续收听</div>' +
    '<h2>' + esc(book.title) + '</h2>' +
    '<div class="narrator">' + (book.author ? esc(authorLabel(book)) + ' 著' : '') +
    (book.narrator ? ' · ' + esc(book.narrator) + ' 演播' : '') + '</div>' +
    '<div class="hero-progress"><div class="scrub"><i style="width:' + pr.percent + '%"></i></div>' +
    '<div class="scrub-time"><span>' + esc(chapterTitle || ('第 ' + (pr.chapter + 1) + ' 章')) + '</span><span>' + pr.percent + '%</span></div></div>' +
    '<div class="hero-actions">' +
    '<button class="play-fab" data-act="resume" data-id="' + esc(book.id) + '" aria-label="继续播放">' +
    '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5l11 6.5-11 6.5z"/></svg></button>' +
    '<a class="btn btn-ghost btn-sm" href="#/book/' + esc(book.id) + '">查看详情</a>' +
    '<span class="hint">' + (book.chapter_count || 0) + ' 章 · ' + fmtSize(book.size) + '</span>' +
    '</div></div>' +
    '<div class="hero-cover" style="background:linear-gradient(150deg,' + p.c1 + ',' + p.c2 + ')">' +
    (book.cover_url
      ? '<img class="art" loading="lazy" src="' + esc(appUrl(book.cover_url)) + '" alt="" onerror="this.remove()">'
      : '<div class="disc"><span class="glyph">' + esc(glyphOf(book.title)) + '</span></div>') +
    '<div class="side-note">SHU ZHONG YOU YIN</div></div></div>';
}

function emptyState(title, desc, actionText, actionAttr) {
  const action = actionText && actionAttr
    ? '<button class="btn btn-soft btn-sm" ' + actionAttr + '>' + esc(actionText) + '</button>'
    : '';
  return '<div class="empty-state"><div class="art">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8 9h8M8 13h6"/></svg></div>' +
    '<h3>' + esc(title) + '</h3><p>' + esc(desc) + '</p>' + action + '</div>';
}

/* ------------------------------------------------------------------ 书库 */

function filteredBooks() {
  let items = state.books;
  const q = state.query.trim().toLowerCase();
  if (q) {
    items = items.filter((b) => {
      const hay = [b.title, b.author, b.narrator, b.description].join(' ').toLowerCase();
      if (hay.indexOf(q) >= 0) return true;
      return (b.chapters || []).some((c) => String(c.title || '').toLowerCase().indexOf(q) >= 0);
    });
  }
  if (state.filter === 'listening') {
    items = items.filter((b) => b.progress && b.progress.updated && !b.progress.finished);
  } else if (state.filter === 'finished') {
    items = items.filter((b) => b.progress && b.progress.finished);
  } else if (state.filter === 'fav') {
    items = items.filter((b) => b.favorite);
  }
  return items;
}

function platformBadgeHtml(book) {
  const p = book.platform;
  if (!p || !p.source) return '';
  const local = Number(book.chapter_count || 0);
  const count = Number(p.count || 0);
  const chips = [];
  if (p.finished === true) chips.push('<i class="plat-badge done">已完结</i>');
  else if (p.finished === false) chips.push('<i class="plat-badge live">连载</i>');
  if (count > local) chips.push('<i class="plat-badge upd">更新 +' + (count - local) + ' 章</i>');
  else if (!chips.length && count) chips.push('<i class="plat-badge info">平台 ' + count + ' 章</i>');
  return chips.length ? '<div class="plat-badges">' + chips.join('') + '</div>' : '';
}

function platformDetailHtml(book) {
  const p = book.platform;
  const updated = p && p.checked_at ? esc(fmtDate(p.checked_at)) : '—';
  const local = Number(book.chapter_count || 0);
  let text = '未匹配到平台书籍，不显示状态角标';
  if (p && p.source) {
    const parts = [];
    if (p.finished === true) parts.push('已完结');
    else if (p.finished === false) parts.push('连载中');
    if (p.count) parts.push('平台 ' + Number(p.count) + ' 章');
    if (Number(p.count) > local) parts.push('本地 ' + local + ' 章 · 更新 +' + (Number(p.count) - local) + ' 章');
    else parts.push('本地 ' + local + ' 章');
    text = parts.join(' · ') + ' · 来源 ' + ({ lanren: '懒人听书', missevan: '猫耳 FM' }[p.source] || '喜马拉雅');
  } else if (p && p.error) {
    text = '未匹配到平台书籍（' + p.error + '）';
  }
  return '<div class="hint">' + text + '<br>上次检查 ' + updated +
    ' <button class="btn btn-ghost btn-sm" data-act="status-refresh-one" data-id="' + esc(book.id) + '">刷新状态</button></div>';
}

function cardFor(book) {
  const pr = progressOf(book);
  return '<a class="book-card" href="#/book/' + esc(book.id) + '">' +
    platformBadgeHtml(book) +
    coverHTML(book, '', (book.chapter_count || 0) + ' 章') +
    '<h3>' + esc(book.title) + '</h3>' +
    '<div class="by">' + esc(book.author || book.narrator || '未知作者') + '</div>' +
    (pr.percent > 0 ? '<div class="progress-line"><i style="width:' + pr.percent + '%"></i></div>' : '') +
    '</a>';
}

function viewLibrary() {
  const items = filteredBooks();
  const chips = [
    ['all', '全部'], ['listening', '在听'], ['finished', '听完'], ['fav', '收藏'],
  ].map(([key, label]) =>
    '<button class="chip ' + (state.filter === key ? 'active' : '') + '" data-act="filter" data-key="' + key + '">' + label + '</button>'
  ).join('');
  const layout = '<div class="segmented">' +
    '<button class="' + (state.layout === 'grid' ? 'active' : '') + '" data-act="layout" data-key="grid">网格</button>' +
    '<button class="' + (state.layout === 'list' ? 'active' : '') + '" data-act="layout" data-key="list">列表</button></div>';
  return '<div class="page-title">书库</div>' +
    '<div class="page-sub">' + state.books.length + ' 本书 · ' +
    (state.scannedAt ? '最近扫描 ' + esc(fmtDate(state.scannedAt)) : '尚未扫描') +
    ' <button class="btn btn-ghost btn-sm" data-act="status-refresh-all">刷新平台状态</button>' +
    (state.statusSummary && state.statusSummary.updated_at
      ? ' <span class="hint">状态更新于 ' + esc(fmtDate(state.statusSummary.updated_at)) + '</span>' : '') +
    '</div>' +
    '<div class="section"><div class="lib-toolbar">' +
    '<div class="chip-row">' + chips + '</div><div class="grow"></div>' + layout + '</div>' +
    '<div class="book-grid' + (state.layout === 'list' ? ' as-list' : '') + '">' +
    (items.length ? items.map(cardFor).join('') : emptyState('没有匹配的书', '换个关键词，或切回「全部」再试试。', '', '')) +
    '</div></div>';
}

function viewFavorites() {
  const items = state.books.filter((b) => b.favorite);
  return '<div class="page-title">收藏</div>' +
    '<div class="page-sub">' + items.length + ' 本已收藏的书</div>' +
    '<div class="section"><div class="book-grid' + (state.layout === 'list' ? ' as-list' : '') + '">' +
    (items.length ? items.map(cardFor).join('') : emptyState('还没有收藏', '在书籍详情页点「收藏」，之后就能在这里快速找到。', '', '')) +
    '</div></div>';
}

/* ------------------------------------------------------------------ 书单 */

function viewPlaylists() {
  const cards = state.playlists.map((p) => {
    const covers = (p.books || []).slice(0, 4).map((b) => {
      const c = palette(b.id);
      return '<i style="--c1:' + c.c1 + ';--c2:' + c.c2 + '"></i>';
    }).join('');
    return '<div class="pl-card" data-act="open-playlist" data-id="' + esc(p.id) + '">' +
      '<div class="eyebrow">' + (p.book_count || 0) + ' 本</div>' +
      '<h3>' + esc(p.title) + '</h3>' +
      '<div class="sub">' + esc(p.description || '私人书单') + '</div>' +
      '<div class="thumbs">' + (covers || '<i></i><i></i><i></i>') + '</div></div>';
  }).join('');
  return '<div class="page-title">我的书单</div>' +
    '<div class="page-sub">把想连着听的几本书排成一条线，整单播放会按顺序接续。</div>' +
    '<div class="section"><div class="panel"><div class="panel-head"><div><h2>新建书单</h2>' +
    '<p>书单与账号绑定，可随时改名、移出书籍或删除。</p></div></div>' +
    '<div class="inline-form"><input id="pl-new" placeholder="例如：睡前必听" maxlength="80" autocomplete="off">' +
    '<button class="btn btn-primary btn-sm" data-act="pl-create">创建</button></div></div></div>' +
    '<div class="section"><div class="pl-list">' +
    (state.playlists.length ? cards : emptyState('还没有书单', '先在上方创建一个，再在书籍详情里把书加进来。', '', '')) +
    '</div></div>';
}

function viewPlaylistDetail(id) {
  const p = state.playlists.find((x) => x.id === id);
  if (!p) {
    return '<div class="page-head"><a class="book-back" href="#/playlists">← 返回书单</a></div>' +
      emptyState('书单不存在', '它可能已被删除，返回列表看看其他书单。', '返回书单', 'data-act="goto-playlists"');
  }
  const rows = (p.items || []).map((it, idx) => {
    const b = it.book || bookById(it.item_id);
    if (!b) return '';
    const pr = progressOf(b);
    return '<div class="list-row"><span class="tail">' + (idx + 1) + '</span>' +
      '<div class="list-thumb" style="--c1:' + palette(b.id).c1 + ';--c2:' + palette(b.id).c2 + '">' + esc(glyphOf(b.title)) + '</div>' +
      '<div class="grow" data-act="open-book" data-id="' + esc(b.id) + '" style="cursor:pointer">' +
      '<div class="name">' + esc(b.title) + '</div><div class="sub">' + (b.chapter_count || 0) + ' 章 · 已听 ' + pr.percent + '%</div></div>' +
      '<button class="icon-btn tiny" data-act="pl-remove" data-id="' + esc(p.id) + '" data-book="' + esc(b.id) + '" title="移出书单">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      '</div>';
  }).join('');
  return '<a class="book-back" href="#/playlists">← 返回书单</a>' +
    '<div class="page-head"><div class="grow"><h1>' + esc(p.title) + '</h1>' +
    '<div class="page-sub">' + (p.book_count || 0) + ' 本 · 更新于 ' + esc(fmtDate(tsOf(p.updated_at))) + '</div></div>' +
    '<button class="btn btn-primary" data-act="pl-play" data-id="' + esc(p.id) + '">整单播放</button>' +
    '<button class="btn btn-ghost" data-act="pl-rename" data-id="' + esc(p.id) + '">改名</button>' +
    '<button class="btn btn-ghost danger-text" data-act="pl-delete" data-id="' + esc(p.id) + '">删除书单</button></div>' +
    '<div class="section"><div class="list">' +
    (rows || '<div style="padding:18px" class="hint">书单里还没有书。打开任意书籍详情，点「加入书单」。</div>') + '</div></div>';
}

/* ------------------------------------------------------------------ 书籍详情 */

function renderBook(id) {
  state.bookId = id;
  const view = $('view');
  const cached = state.detailCache[id];
  if (cached) {
    view.innerHTML = viewBook(cached);
    scrollTop();
    focusChapterBar();
    focusCurrentChapter();
    setupClamps();
    return;
  }
  view.innerHTML = '<div class="boot" style="min-height:36vh"><div class="spinner"></div><p>正在读取书籍…</p></div>';
  detailOf(id).then((data) => {
    if (route().arg !== id) return;
    view.innerHTML = viewBook(data);
    focusChapterBar();
    focusCurrentChapter();
    setupClamps();
  }).catch((err) => {
    view.innerHTML = '<a class="book-back" href="#/library">← 返回书库</a>' +
      emptyState('打不开这本书', err.message || '请重新扫描书库后再试。', '返回书库', 'data-act="goto-library"');
  });
}

function chapterDuration(book, index) {
  const durations = (book.progress && book.progress.durations) || {};
  return Number(durations[String(index)] || 0);
}

const CHAPTER_GROUP_SIZE = 100;

function currentChapterIndex(book) {
  if (state.current && state.current.id === book.id) {
    return Math.max(0, Number(state.chapter) || 0);
  }
  const progress = book.progress || {};
  return Math.max(0, Number(progress.chapter) || 0);
}

function chapterRowHtml(book, chapter, index) {
  const shown = currentChapterIndex(book);
  const current = shown === index;
  const playing = current && state.current && state.current.id === book.id && state.playing;
  const dur = chapterDuration(book, index);
  return '<div class="chapter ' + (current ? 'current' : '') + '"' +
    ' data-act="play-chapter" data-id="' + esc(book.id) + '" data-index="' + index + '">' +
    '<span class="idx">' + String(index + 1).padStart(2, '0') + '</span>' +
    '<span class="title">' + esc(chapter.title || chapter.file_name || ('第 ' + (index + 1) + ' 章')) + '</span>' +
    (playing ? '<span class="playing"><i></i><i></i><i></i></span>' : '') +
    (dur ? '<span class="dur">' + fmtTime(dur) + '</span>' : '') +
    '<a class="dl" href="' + esc(appUrl('/api/stream/' + book.id + '_' + index)) + '" download title="下载本章" onclick="event.stopPropagation()">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 4v11M7.5 11l4.5 4.5 4.5-4.5M5 20h14"/></svg></a>' +
    '</div>';
}

function chapterRowsHtml(book, chapters, start, end) {
  let html = '';
  for (let i = start; i < end; i += 1) html += chapterRowHtml(book, chapters[i], i);
  return html;
}

function prepareChapterGroup(book) {
  if (state.chapterGroupBook !== book.id) {
    state.chapterGroupBook = book.id;
    state.chapterGroup = Math.floor(currentChapterIndex(book) / CHAPTER_GROUP_SIZE);
  }
}

function chapterBarHtml(book, chapters) {
  const current = currentChapterIndex(book);
  const total = Math.ceil(chapters.length / CHAPTER_GROUP_SIZE);
  if (total <= 1) return '';
  let html = '';
  for (let group = 0; group < total; group += 1) {
    const start = group * CHAPTER_GROUP_SIZE;
    const end = Math.min(chapters.length, start + CHAPTER_GROUP_SIZE);
    const active = group === state.chapterGroup;
    const here = current >= start && current < end;
    html += '<button class="ch-pill' + (active ? ' active' : '') + (here ? ' here' : '') + '"' +
      ' type="button" data-act="chapter-group-pick" data-group="' + group + '" aria-pressed="' + (active ? 'true' : 'false') + '">' +
      (start + 1) + '-' + end + '<span>· ' + (end - start) + ' 章</span></button>';
  }
  return '<div class="ch-bar" data-ch-bar>' + html + '</div>';
}

function chapterGroupBodyHtml(book, chapters) {
  const start = state.chapterGroup * CHAPTER_GROUP_SIZE;
  const end = Math.min(chapters.length, start + CHAPTER_GROUP_SIZE);
  return chapterRowsHtml(book, chapters, start, end);
}

function pickChapterGroup(el) {
  const book = state.bookShown;
  const chapters = (book && book.chapters) || [];
  if (!book || !chapters.length) return;
  const group = Math.max(0, Number(el.dataset.group || 0));
  if (group === state.chapterGroup) return;
  state.chapterGroup = group;
  $$('[data-ch-bar] .ch-pill').forEach((pill) => {
    const on = Number(pill.dataset.group) === group;
    pill.classList.toggle('active', on);
    pill.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  const list = document.querySelector('#view .chapter-list');
  if (list) list.innerHTML = chapterGroupBodyHtml(book, chapters);
  const bar = el.closest('.ch-bar');
  if (bar) {
    try { bar.scrollTo({ left: el.offsetLeft - 16, behavior: 'smooth' }); } catch (err) { /* 忽略 */ }
  }
}

function focusChapterBar() {
  const bar = document.querySelector('#view .ch-bar');
  const pill = bar && bar.querySelector('.ch-pill.active');
  if (!bar || !pill) return;
  bar.scrollLeft = Math.max(0, pill.offsetLeft - bar.clientWidth / 2 + pill.clientWidth / 2);
}

function focusCurrentChapter() {
  setTimeout(() => {
    const el = document.querySelector('#view .chapter.current');
    if (!el || !el.isConnected) return;
    try {
      const rect = el.getBoundingClientRect();
      if (rect.top < 80 || rect.bottom > window.innerHeight - 20) {
        el.scrollIntoView({ block: 'center' });
      }
    } catch (err) { /* 忽略 */ }
  }, 160);
}

function clampHtml(text, lines) {
  const value = Number(lines) || 3;
  return '<div class="clamp" data-lines="' + value + '" style="--clamp-lines:' + value + '">' +
    '<div class="clamp-body">' + esc(text) + '</div>' +
    '<span class="clamp-mask" aria-hidden="true"></span>' +
    '<button class="clamp-toggle" type="button" data-act="clamp-toggle" aria-expanded="false">展开全部</button>' +
    '</div>';
}

function setupClamps() {
  $$('.clamp').forEach((box) => {
    const body = box.querySelector('.clamp-body');
    if (!body) return;
    const lines = Number(box.dataset.lines || 3);
    const lineHeight = parseFloat(getComputedStyle(body).lineHeight) || 22;
    const limit = Math.round(lineHeight * lines);
    body.style.maxHeight = 'none';
    const full = body.scrollHeight;
    if (full <= limit + 2) {
      box.classList.remove('has-more');
      body.style.maxHeight = '';
      return;
    }
    box.classList.add('has-more');
    body.style.maxHeight = limit + 'px';
    box.dataset.full = String(full);
    box.dataset.limit = String(limit);
  });
}

function toggleClamp(button) {
  const box = button.closest('.clamp');
  if (!box) return;
  const body = box.querySelector('.clamp-body');
  if (!body) return;
  const open = !box.classList.contains('is-open');
  box.classList.toggle('is-open', open);
  body.style.maxHeight = open ? (box.dataset.full || body.scrollHeight) + 'px' : (box.dataset.limit || '') + 'px';
  button.textContent = open ? '收起' : '展开全部';
  button.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function viewBook(book) {
  state.bookShown = book;
  const pr = progressOf(book);
  const tab = state.bookTab === 'info' ? 'info' : 'chapters';
  const chapters = book.chapters || [];
  const currentIdx = Number((book.progress && book.progress.chapter) || 0);
  const metaBits = [];
  if (book.author) metaBits.push(esc(book.author) + ' 著');
  if (book.narrator) metaBits.push(esc(book.narrator) + ' 演播');
  metaBits.push(esc(String(book.chapter_count || 0)) + ' 章');
  metaBits.push(fmtSize(book.size));
  if (book.year) metaBits.push(esc(book.year));
  const tabs = '<div class="tabs">' +
    '<button class="tab ' + (tab === 'chapters' ? 'active' : '') + '" data-act="book-tab" data-key="chapters">章节目录</button>' +
    '<button class="tab ' + (tab === 'info' ? 'active' : '') + '" data-act="book-tab" data-key="info">简介与信息</button></div>';
  let body = '';
  if (tab === 'chapters') {
    prepareChapterGroup(book);
    if (chapters.length > CHAPTER_GROUP_SIZE) {
      body = chapterBarHtml(book, chapters) +
        '<div class="chapter-list">' + chapterGroupBodyHtml(book, chapters) + '</div>';
    } else {
      body = '<div class="chapter-list">' +
        (chapterRowsHtml(book, chapters, 0, chapters.length) || '<div class="hint" style="padding:18px">没有解析到章节文件</div>') +
        '</div>';
    }
  } else {
    body = '<div class="panel" style="margin-top:18px">' +
      '<div class="form-row"><div><div class="label">简介</div></div>' +
      '<div class="detail-desc-wrap">' + clampHtml(book.description || '暂无简介，可在「在线刮削」中补全。', 3) + '</div></div>' +
      '<div class="form-row"><div class="label">分类标签</div><div class="hint">' + esc(book.tags || book.genre || '—') + '</div></div>' +
      '<div class="form-row"><div class="label">出版信息</div><div class="hint">' + esc(book.publisher || '—') + ' · ' + esc(book.year || '—') + '</div></div>' +
      '<div class="form-row"><div class="label">目录路径</div><div class="hint mono" style="max-width:560px;word-break:break-all">' + esc(book.path || '—') + '</div></div>' +
      '<div class="form-row"><div class="label">最近更新</div><div class="hint">' + esc(fmtDate(book.mtime)) + '</div></div>' +
      '<div class="form-row"><div class="label">平台状态</div>' + platformDetailHtml(book) + '</div>' +
      '</div>';
  }
  const skipOpts = skipOptionsHtml(book, 'skip_intro');
  const skipOutOpts = skipOptionsHtml(book, 'skip_outro');
  return '<a class="book-back" href="#/library">← 返回书库</a>' +
    '<div class="detail-head">' +
    '<div class="detail-cover" style="background:linear-gradient(150deg,' + palette(book.id).c1 + ',' + palette(book.id).c2 + ')">' +
    (book.cover_url ? '<img class="art" src="' + esc(appUrl(book.cover_url)) + '" alt="" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover" onerror="this.remove()">' : '') +
    '<span class="glyph" style="position:relative;z-index:1">' + esc(glyphOf(book.title)) + '</span>' +
    '<span class="ring"></span></div>' +
    '<div class="detail-info">' +
    '<h1>' + esc(book.title) + '</h1>' +
    '<div class="detail-meta">' + metaBits.join(' <span class="dot"></span> ') + '</div>' +
    (book.description
      ? '<div class="detail-desc-wrap detail-desc-top">' + clampHtml(book.description, 3) + '</div>'
      : '<div class="detail-desc">' + esc('这本书还没有简介。你可以在书籍目录里放一份 metadata.json，或用「在线刮削」补全作者与封面。') + '</div>') +
    '<div class="detail-actions">' +
    '<button class="btn btn-primary" data-act="resume" data-id="' + esc(book.id) + '">' + (pr.percent > 0 ? '继续播放' : '开始播放') + '</button>' +
    '<button class="btn btn-ghost" data-act="restart" data-id="' + esc(book.id) + '">从头播放</button>' +
    '<button class="btn btn-soft" data-act="toggle-fav" data-id="' + esc(book.id) + '">' + (book.favorite ? '已收藏' : '收藏') + '</button>' +
    '<button class="btn btn-ghost" data-act="add-playlist" data-id="' + esc(book.id) + '">加入书单</button>' +
    '<button class="btn btn-ghost" data-act="scrape" data-id="' + esc(book.id) + '">在线刮削</button>' +
    '</div>' +
    '<div class="pref"><span>跳过片头</span><select data-skip="intro" data-id="' + esc(book.id) + '">' + skipOpts + '</select>' +
    '<span>跳过片尾</span><select data-skip="outro" data-id="' + esc(book.id) + '">' + skipOutOpts + '</select>' +
    '<span class="hint">已听 ' + pr.percent + '% · ' + esc(chapterTitleOf(book, currentIdx)) + '</span></div>' +
    '</div></div>' + tabs + body;
}

const SKIP_PRESETS = [0, 5, 10, 15, 20, 30];

function fmtSkip(value) {
  const num = Number(value) || 0;
  return Number.isInteger(num) ? String(num) : String(Number(num.toFixed(1)));
}

function parseSkipSeconds(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;
  const mm = raw.match(/^(\d{1,3})\s*[:：]\s*(\d{1,2}(?:\.\d+)?)$/);
  if (mm) return Number(mm[1]) * 60 + Number(mm[2]);
  if (!/^\d{1,4}(?:\.\d{1,2})?$/.test(raw)) return null;
  return Number(raw);
}

function skipOptionsHtml(book, key) {
  const value = Number(book[key] || 0);
  let html = SKIP_PRESETS.map((v) =>
    '<option value="' + v + '"' + (value === v ? ' selected' : '') + '>' +
    (v ? v + ' 秒' : '不跳过') + '</option>').join('');
  if (value > 0 && !SKIP_PRESETS.includes(value)) {
    html += '<option value="' + value + '" selected>自定义 · ' + fmtSkip(value) + ' 秒</option>';
  }
  html += '<option value="custom">自定义…</option>';
  return html;
}

function knownChapterDuration(book) {
  if (!book) return 0;
  if (state.current && state.current.id === book.id && audio.duration) return audio.duration;
  const durations = (book.progress && book.progress.durations) || {};
  const values = Object.keys(durations).map((k) => Number(durations[k]) || 0).filter((v) => v > 0);
  return values.length ? Math.max.apply(null, values) : 0;
}

function platformToast(record) {
  if (!record || !record.source) return '未匹配到平台书籍';
  const parts = [];
  if (record.finished === true) parts.push('已完结');
  else if (record.finished === false) parts.push('连载中');
  if (record.count) parts.push('平台 ' + Number(record.count) + ' 章');
  return parts.join(' · ') || '刷新完成';
}

function refreshBookStatus(id) {
  if (!id) return;
  toast('正在刷新平台状态…');
  api('/api/books/' + encodeURIComponent(id) + '/status/refresh?force=1', { method: 'POST', body: '{}' })
    .then((data) => {
      const book = state.bookMap[id] || state.detailCache[id] || bookById(id);
      if (book) book.platform = data.platform;
      if (data.summary) state.statusSummary = data.summary;
      toast(platformToast(data.platform));
      render();
    })
    .catch((err) => toast(err.message || '刷新失败'));
}

function refreshAllStatus() {
  api('/api/status/refresh', { method: 'POST', body: '{}' })
    .then((data) => {
      state.statusSummary = data.platform || state.statusSummary;
      toast(data.started ? '已开始后台刷新 ' + (data.platform.total || 0) + ' 本书' : '已有刷新任务在进行');
      render();
    })
    .catch((err) => toast(err.message || '刷新失败'));
}

function openSkipCustom(book, key) {
  const label = key === 'skip_intro' ? '片头' : '片尾';
  const current = Number(book[key] || 0);
  const limit = knownChapterDuration(book);
  const maxHint = limit ? '当前可用章节时长约 ' + fmtTime(limit) + '，上限为 ' + fmtSkip(limit / 2) + ' 秒。' : '章节时长未知时按 600 秒上限校验。';
  openModal('自定义跳过' + label,
    '<div class="skip-custom">' +
    '<p class="hint">输入秒数，支持小数（12.5）或「分:秒」（1:30）。' + maxHint + '</p>' +
    '<input class="input" id="skip-input" inputmode="decimal" autocomplete="off" spellcheck="false" ' +
    'value="' + (current > 0 ? fmtSkip(current) : '') + '" placeholder="例如 12.5 或 1:30">' +
    '<div class="hint skip-error" id="skip-error"></div>' +
    '<div class="skip-actions">' +
    '<button class="btn btn-ghost" type="button" data-close="modal">取消</button>' +
    '<button class="btn btn-primary" type="button" data-act="skip-save" data-id="' + esc(book.id) + '" data-key="' + key + '">确定</button>' +
    '</div></div>');
  setTimeout(() => {
    const input = $('skip-input');
    if (input) { input.focus(); input.select(); }
  }, 80);
}

function saveSkipCustom(button) {
  const book = state.detailCache[button.dataset.id] || bookById(button.dataset.id);
  if (!book) return;
  const key = button.dataset.key === 'skip_outro' ? 'skip_outro' : 'skip_intro';
  const input = $('skip-input');
  const error = $('skip-error');
  const value = parseSkipSeconds(input ? input.value : '');
  const limit = knownChapterDuration(book);
  const max = limit ? Math.min(600, limit * 0.5) : 600;
  let message = '';
  if (value === null) message = '请输入正数秒数，或「分:秒」格式（如 1:30）。';
  else if (value <= 0) message = '必须为正数。';
  else if (value > max) message = '不能超过 ' + fmtSkip(max) + ' 秒（章节时长的 50%）。';
  if (message) {
    if (error) error.textContent = message;
    if (input) input.focus();
    return;
  }
  const rounded = Number(value.toFixed(1));
  api('/api/books/' + encodeURIComponent(book.id), {
    method: 'PUT', body: JSON.stringify({ [key]: rounded }),
  }).then(() => {
    book[key] = rounded;
    closeModal();
    toast('已保存' + (key === 'skip_intro' ? '片头' : '片尾') + '跳过 ' + fmtSkip(rounded) + ' 秒');
    render();
  }).catch((err) => {
    if (error) error.textContent = err.message || '保存失败';
  });
}

function maybeSkipOutro() {
  const book = state.current;
  if (!book || !state.playing) return;
  const outro = Number(book.skip_outro || 0);
  const duration = Number(audio.duration || 0);
  if (outro <= 0 || duration <= 0) return;
  const key = book.id + ':' + state.chapter;
  if (state.outroDone === key) return;
  if (duration - audio.currentTime > outro) return;
  state.outroDone = key;
  const chapters = book.chapters || [];
  if (state.chapter + 1 < chapters.length) {
    nextChapter();
  } else if (state.queue.length && state.queueIndex >= 0 && state.queueIndex + 1 < state.queue.length) {
    nextChapter();
  }
}

function chapterTitleOf(book, index) {
  const c = (book.chapters || [])[index];
  if (c) return c.title || c.file_name || ('第 ' + (index + 1) + ' 章');
  return index >= 0 ? ('第 ' + (index + 1) + ' 章') : '尚未开始';
}

/* ------------------------------------------------------------------ 播放页 */

function viewPlayer() {
  const book = state.current;
  if (!book) {
    return '<div class="page-title">播放</div>' +
      '<div class="page-sub">还没有正在播放的内容。</div>' +
      emptyState('从一本书开始', '打开任意书籍，点「开始播放」，这里会显示完整的播放控制。', '去书库', 'data-act="goto-library"');
  }
  const pr = progressOf(book);
  const chapter = book.chapters[state.chapter] || {};
  const total = (book.chapters || []).length;
  const hasText = !!chapter.has_subtitle;
  const pal = palette(book.id || book.title || 'book');
  const coverImg = book.cover_url
    ? '<img class="np-cover-art" src="' + esc(appUrl(book.cover_url)) + '" alt="" onerror="this.remove()">' : '';
  const favText = book.favorite ? '已收藏' : '收藏';
  return '<a class="book-back" href="#/book/' + esc(book.id) + '">← 返回书籍</a>' +
    '<div class="np-shell">' +
      '<div class="np-main">' +
        '<div class="np-left">' +
          '<div class="np-kicker">正在播放 · 共 ' + total + ' 章</div>' +
          '<h1 class="np-title">' + esc(book.title) + '</h1>' +
          '<div class="np-chapter" id="p-chapter">第 ' + (state.chapter + 1) + ' 章 · ' + esc(chapter.title || chapter.file_name || '') + '</div>' +
          '<div class="np-meta">已听 ' + pr.percent + '% · <span id="p-rate-label">' + state.rate + 'x</span>' +
            (book.author ? ' · ' + esc(book.author) : '') + '</div>' +
          (hasText ?
            '<div class="np-text" id="p-text">' +
              '<div class="np-text-head"><span class="np-text-title">同步文字</span>' +
              '<span class="np-sub-badge" id="p-sub-badge"></span>' +
              '<button class="np-text-toggle" id="p-text-toggle" data-act="player-text">' +
              (state.textOn ? '隐藏文字' : '显示文字') + '</button></div>' +
              '<div class="np-lines" id="p-lines"></div>' +
            '</div>' : '') +
        '</div>' +
        '<div class="np-right">' +
          '<div class="np-cover" style="--c1:' + pal.c1 + ';--c2:' + pal.c2 + '">' +
            '<span class="np-cover-glyph">' + esc(glyphOf(book.title)) + '</span>' + coverImg +
          '</div>' +
          '<div class="np-cover-meta">' + total + ' 章 · ' + favText + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="np-dock">' +
        '<div class="np-timeline">' +
          '<span class="np-time" id="p-now">00:00</span>' +
          '<div class="np-bar" id="p-bar"><i id="p-fill"></i><u id="p-knob"></u></div>' +
          '<span class="np-time" id="p-total">00:00</span>' +
        '</div>' +
        '<div class="np-controls">' +
          '<div class="np-side">' +
            '<button class="np-util" data-act="player-chapters" title="跳到章节">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 6h16M4 12h16M4 18h10"/></svg><span>章节</span></button>' +
            '<button class="np-util" id="p-fav" data-act="toggle-fav" data-id="' + esc(book.id) + '" title="收藏">' +
            '<svg viewBox="0 0 24 24" fill="' + (book.favorite ? 'currentColor' : 'none') + '" stroke="currentColor" stroke-width="1.6"><path d="M12 4.6l2.5 5 5.5.8-4 3.9 1 5.5-5-2.7-5 2.7 1-5.5-4-3.9 5.5-.8z"/></svg><span>' + favText + '</span></button>' +
          '</div>' +
          '<div class="np-transport">' +
            '<button class="np-ctrl" data-act="prev" aria-label="上一章"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 6v12M19 7l-8 5 8 5z"/></svg></button>' +
            '<button class="np-ctrl" data-act="seek-back" aria-label="后退15秒"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M11 7a6 6 0 1 1-5.7 4.2"/><path d="M11 3v4.2H6.8"/><text x="10" y="17" font-size="8" fill="currentColor" stroke="none">15</text></svg></button>' +
            '<button class="np-ctrl np-play" id="p-play" data-act="toggle" aria-label="播放或暂停">' +
            '<svg class="ico-play" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5l11 6.5-11 6.5z"/></svg>' +
            '<svg class="ico-pause" viewBox="0 0 24 24" fill="currentColor"><rect x="7.5" y="5.5" width="3.2" height="13" rx="1.2"/><rect x="13.3" y="5.5" width="3.2" height="13" rx="1.2"/></svg>' +
            '</button>' +
            '<button class="np-ctrl" data-act="seek-fwd" aria-label="前进15秒"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M13 7a6 6 0 1 0 5.7 4.2"/><path d="M13 3v4.2h4.2"/><text x="9" y="17" font-size="8" fill="currentColor" stroke="none">15</text></svg></button>' +
            '<button class="np-ctrl" data-act="next" aria-label="下一章"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M17 6v12M5 7l8 5-8 5z"/></svg></button>' +
          '</div>' +
          '<div class="np-side right">' +
            '<button class="np-util" data-act="player-rate" title="播放速度">' +
            '<span id="p-rate">' + state.rate + 'x</span></button>' +
            '<span class="np-vol"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 9v6h4l5 4V5l-5 4z"/><path d="M16 8.5a5 5 0 0 1 0 7"/></svg>' +
            '<input id="volume" type="range" min="0" max="1" step="0.02" value="' + (audio.volume || 1) + '" aria-label="音量"></span>' +
            '<button class="np-util ' + (state.bgm.enabled ? 'on' : '') + '" data-act="bgm-toggle" title="背景音乐开关">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18.5" r="2.5"/><circle cx="16.5" cy="16.5" r="2.5"/></svg><span>BGM</span></button>' +
            '<button class="np-util" data-act="player-more" title="更多播放选项">' +
            '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg><span>更多</span></button>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
}

/* 章节字幕：仅当扫描阶段检测到 .lrc/.srt/.vtt 时才会渲染文字区 */
function subtitleKeyOf() {
  return state.current ? (state.current.id + ':' + state.chapter) : '';
}

function currentChapter() {
  return (state.current && state.current.chapters && state.current.chapters[state.chapter]) || {};
}

function ensureSubtitle() {
  const panel = $('p-text');
  if (!panel || !state.current) return Promise.resolve();
  const chapter = currentChapter();
  if (!chapter.has_subtitle) {
    state.subtitle = { key: '', status: 'none', format: '', encoding: '', segments: [], error: '' };
    return Promise.resolve();
  }
  const key = subtitleKeyOf();
  if (state.subtitle.key === key && ['ready', 'loading'].indexOf(state.subtitle.status) >= 0) {
    paintPlayerText();
    return Promise.resolve();
  }
  state.subtitle = { key: key, status: 'loading', format: '', encoding: '', segments: [], error: '' };
  paintPlayerText();
  return api('/api/books/' + encodeURIComponent(state.current.id) +
    '/chapters/' + state.chapter + '/subtitle').then((data) => {
    if (state.subtitle.key !== key) return;
    state.subtitle = {
      key: key, status: 'ready',
      format: data.format || '', encoding: data.encoding || '',
      segments: data.segments || [], error: '',
    };
    paintPlayerText();
    syncPlayerText();
  }).catch((err) => {
    if (state.subtitle.key !== key) return;
    state.subtitle = { key: key, status: 'error', format: '', encoding: '', segments: [], error: err.message || '读取失败' };
    paintPlayerText();
  });
}

function paintPlayerText() {
  const panel = $('p-text');
  const lines = $('p-lines');
  if (!panel || !lines) return;
  const chapter = currentChapter();
  if (!chapter.has_subtitle) { panel.hidden = true; return; }
  panel.hidden = false;
  const sub = state.subtitle;
  const badge = $('p-sub-badge');
  if (badge) {
    badge.textContent = sub.status === 'ready'
      ? ((sub.format || 'srt').toUpperCase() + ' · ' + sub.segments.length + ' 句')
      : (sub.status === 'loading' ? '载入中…' : (sub.status === 'error' ? '读取失败' : ''));
  }
  const toggle = $('p-text-toggle');
  if (toggle) toggle.textContent = state.textOn ? '隐藏文字' : '显示文字';
  if (!state.textOn) { lines.hidden = true; lines.innerHTML = ''; return; }
  lines.hidden = false;
  if (sub.status === 'loading') {
    lines.innerHTML = '<div class="np-lines-note">正在载入字幕…</div>';
    return;
  }
  if (sub.status === 'error') {
    lines.innerHTML = '<div class="np-lines-note">字幕读取失败：' + esc(sub.error) + '</div>';
    return;
  }
  if (!sub.segments.length) {
    lines.innerHTML = '<div class="np-lines-note">字幕为空或格式无法识别</div>';
    return;
  }
  lines.innerHTML = sub.segments.map((seg, i) =>
    '<button class="np-line" data-act="player-seek" data-index="' + i + '">' +
    '<span class="np-line-t">' + fmtTime(seg.start) + '</span>' +
    '<span class="np-line-x">' + esc(seg.text) + '</span></button>').join('');
  syncPlayerText();
}

function syncPlayerText() {
  const lines = $('p-lines');
  if (!lines || !state.textOn) return;
  const segs = (state.subtitle && state.subtitle.segments) || [];
  if (!segs.length) return;
  const now = Number(audio.currentTime || 0);
  let cur = -1;
  for (let i = 0; i < segs.length; i += 1) {
    if (now >= Number(segs[i].start || 0)) cur = i;
    if (now < Number(segs[i].end || 0) && now >= Number(segs[i].start || 0)) { cur = i; break; }
  }
  const rows = $$('.np-line', lines);
  rows.forEach((row, i) => {
    row.classList.toggle('cur', i === cur);
    row.classList.toggle('next', i === cur + 1);
  });
  if (cur >= 0 && rows[cur]) {
    const target = rows[cur].offsetTop - lines.clientHeight / 2 + rows[cur].clientHeight / 2;
    if (Math.abs(lines.scrollTop - target) > rows[cur].clientHeight) {
      lines.scrollTop = Math.max(0, target);
    }
  }
}

function openPlayerChapters() {
  const book = state.current;
  if (!book) return;
  openModal('跳到章节 · ' + book.title,
    '<div class="np-modal">' +
    '<input id="pl-ch-filter" class="np-filter" placeholder="输入章节序号或关键字筛选" autocomplete="off">' +
    '<div class="np-ch-list" id="pl-ch-list"></div></div>');
  paintPlayerChapterList('');
}

function paintPlayerChapterList(query) {
  const box = $('pl-ch-list');
  if (!box || !state.current) return;
  const list = state.current.chapters || [];
  const needle = String(query || '').trim().toLowerCase();
  const rows = [];
  for (let i = 0; i < list.length && rows.length < 200; i += 1) {
    const chapter = list[i] || {};
    const title = chapter.title || chapter.file_name || ('第 ' + (i + 1) + ' 章');
    const hay = ((i + 1) + ' ' + title).toLowerCase();
    if (needle && hay.indexOf(needle) < 0) continue;
    rows.push('<button class="np-ch-row ' + (i === state.chapter ? 'cur' : '') + '" data-act="player-chapter" data-index="' + i + '">' +
      '<span class="np-ch-idx">' + (i + 1) + '</span>' +
      '<span class="np-ch-title">' + esc(title) + '</span>' +
      (chapter.has_subtitle ? '<span class="np-ch-sub">字幕</span>' : '') + '</button>');
  }
  box.innerHTML = rows.length
    ? rows.join('') + (list.length > rows.length ? '<div class="np-lines-note">仅显示前 ' + rows.length + ' 条，输入关键字继续筛选</div>' : '')
    : '<div class="np-lines-note">没有匹配的章节</div>';
}

function openPlayerMore() {
  const book = state.current;
  if (!book) return;
  const sleepText = state.sleepMin ? ('定时 ' + state.sleepMin + ' 分钟') : '定时关闭';
  const queue = state.queue.length ? state.queue : (book.chapters || []).map((c, i) =>
    ({ bookId: book.id, index: i, title: c.title, bookTitle: book.title }));
  openModal('播放选项 · ' + book.title,
    '<div class="np-modal">' +
    '<div class="np-more-row"><span>播放速度</span><div class="np-mini-seg">' +
    [0.75, 1, 1.25, 1.5, 2].map((v) =>
      '<button class="' + (Math.abs(state.rate - v) < 0.01 ? 'on' : '') + '" data-act="rate" data-rate="' + v + '">' + v + 'x</button>').join('') +
    '</div></div>' +
    '<div class="np-more-row"><span>定时关闭</span><button class="np-util" data-act="sleep" data-min="' + (state.sleepMin || 0) + '">' + sleepText + '</button></div>' +
    '<div class="np-more-title">播放队列（前 40 条）</div>' +
    '<div class="np-ch-list">' +
    queue.slice(0, 40).map((q, i) =>
      '<button class="np-ch-row ' + (i === state.queueIndex && state.queue.length ? 'cur' : '') + '" data-act="queue-jump" data-index="' + i + '">' +
      '<span class="np-ch-idx">' + (i + 1) + '</span><span class="np-ch-title">' + esc(q.title || ('第 ' + (i + 1) + ' 章')) + '</span></button>').join('') +
    '</div></div>');
}

function cyclePlayerRate() {
  const seq = [0.75, 1, 1.25, 1.5, 2];
  const idx = seq.findIndex((v) => Math.abs(state.rate - v) < 0.01);
  applyRate(seq[(idx + 1) % seq.length]);
  if ($('p-rate')) $('p-rate').textContent = state.rate + 'x';
  if ($('p-rate-label')) $('p-rate-label').textContent = state.rate + 'x';
}

/* ------------------------------------------------------------------ 播放引擎 */

function openPlayer() {
  go('#/player');
}

function showMini(book) {
  const mini = $('mini');
  mini.classList.remove('hidden');
  $('mini-cover').src = book.cover_url ? appUrl(book.cover_url) : '';
  $('mini-cover').style.visibility = book.cover_url ? 'visible' : 'hidden';
  $('mini-title').textContent = book.title;
  $('mini-chapter').textContent = '第 ' + (state.chapter + 1) + ' 章 · ' + chapterTitleOf(book, state.chapter);
}

function syncMini() {
  const mini = $('mini');
  if (!mini || mini.classList.contains('hidden')) return;
  mini.classList.toggle('playing', state.playing);
  if (state.current) {
    $('mini-chapter').textContent = '第 ' + (state.chapter + 1) + ' 章 · ' + chapterTitleOf(state.current, state.chapter);
  }
}

function playEntry(bookId, index, options) {
  const opts = options || {};
  return detailOf(bookId).then((book) => {
    state.current = book;
    state.chapter = Math.max(0, Math.min((book.chapters || []).length - 1, Number(index) || 0));
    state.outroDone = '';
    if (opts.queue) {
      state.queue = opts.queue;
      state.queueIndex = opts.queueIndex == null ? -1 : opts.queueIndex;
    } else if (!state.queue.length || state.queue[0].bookId !== bookId) {
      state.queue = (book.chapters || []).map((c, i) => ({ bookId: book.id, index: i, title: c.title, bookTitle: book.title }));
      state.queueIndex = state.chapter;
    }
    const url = appUrl('/api/stream/' + book.id + '_' + state.chapter);
    audio.src = url;
    const pr = book.progress || {};
    const sameChapter = Number(pr.chapter || 0) === state.chapter && Number(pr.position || 0) > 3;
    const resumeAt = opts.resume || sameChapter ? Number(pr.position || 0) : 0;
    audio.load();
    const onLoaded = () => {
      audio.removeEventListener('loadedmetadata', onLoaded);
      if (resumeAt > 0 && resumeAt < (audio.duration || 0) - 3) audio.currentTime = resumeAt;
      const skip = Number(book.skip_intro || 0);
      if (skip > 0 && audio.currentTime < 2 && resumeAt <= 0) audio.currentTime = Math.min(skip, (audio.duration || skip) - 1);
      audio.playbackRate = state.rate;
      audio.play().catch(() => {});
    };
    audio.addEventListener('loadedmetadata', onLoaded);
    showMini(book);
    syncMini();
    state.playing = true;
    startProgressSaver();
    if (route().name === 'player') render();
    else if (route().name === 'book') render();
  }).catch((err) => toast('播放失败：' + err.message));
}

function resumeBook(bookId) {
  const book = state.detailCache[bookId] || bookById(bookId);
  const pr = (book && book.progress) || {};
  playEntry(bookId, Number(pr.chapter || 0), { resume: true });
}

function restartBook(bookId) {
  playEntry(bookId, 0, {});
}

function togglePlay() {
  if (!state.current) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}

function nextChapter() {
  if (!state.current) return;
  const list = state.queue.length ? state.queue : null;
  if (list && state.queueIndex >= 0 && state.queueIndex < list.length - 1) {
    const q = list[state.queueIndex + 1];
    state.queueIndex += 1;
    if (q.bookId !== (state.current && state.current.id)) {
      playEntry(q.bookId, q.index, { queue: list, queueIndex: state.queueIndex });
    } else {
      playEntry(q.bookId, q.index, { queue: list, queueIndex: state.queueIndex });
    }
    return;
  }
  if (state.chapter + 1 < (state.current.chapters || []).length) {
    playEntry(state.current.id, state.chapter + 1, { queue: list, queueIndex: state.queueIndex + 1 });
  } else {
    audio.pause();
    toast('已经是最后一章');
  }
}

function prevChapter() {
  if (!state.current) return;
  if (audio.currentTime > 4) {
    audio.currentTime = 0;
    return;
  }
  if (state.chapter > 0) {
    state.queueIndex = Math.max(0, state.queueIndex - 1);
    playEntry(state.current.id, state.chapter - 1, { queue: state.queue, queueIndex: state.queueIndex });
  } else {
    audio.currentTime = 0;
  }
}

function seekBy(seconds) {
  if (!audio.duration) return;
  audio.currentTime = Math.max(0, Math.min(audio.duration - 0.5, audio.currentTime + seconds));
}

let progressSavedAt = 0;
let progressLastWrite = 0;

function startProgressSaver() {
  if (state.saveTimer) clearInterval(state.saveTimer);
  state.saveTimer = setInterval(() => { saveProgress(false); }, 10000);
}

function saveProgress(useBeacon) {
  if (!state.current) return;
  const book = state.current;
  const now = Date.now();
  if (!useBeacon && now - progressLastWrite < 3000) return;
  const payload = {
    book_id: book.id,
    chapter: state.chapter,
    position: Number(audio.currentTime || 0),
    duration: Number(audio.duration || 0) || 0,
    finished: false,
  };
  progressLastWrite = now;
  if (useBeacon && navigator.sendBeacon) {
    const url = appUrl('/api/progress?token=' + encodeURIComponent(state.token));
    navigator.sendBeacon(url, new Blob([JSON.stringify(payload)], { type: 'application/json' }));
    return;
  }
  api('/api/progress', { method: 'POST', body: JSON.stringify(payload) }).catch(() => {});
  const cached = state.detailCache[book.id] || book;
  cached.progress = Object.assign({}, cached.progress, {
    chapter: state.chapter, position: payload.position, duration: payload.duration, updated: Math.floor(now / 1000),
  });
  const row = state.bookMap[book.id];
  if (row) row.progress = Object.assign({}, row.progress, cached.progress);
}

function syncPlayerUI() {
  const has = !!state.current;
  const playBtn = $('p-play');
  if (playBtn) playBtn.classList.toggle('playing', state.playing);
  if (!has) return;
  const dur = Number(audio.duration || 0);
  const cur = Number(audio.currentTime || 0);
  const pct = dur > 0 ? Math.min(100, (cur / dur) * 100) : 0;
  if ($('p-now')) $('p-now').textContent = fmtTime(cur);
  if ($('p-total')) $('p-total').textContent = fmtTime(dur);
  if ($('p-fill')) $('p-fill').style.width = pct + '%';
  if ($('p-knob')) $('p-knob').style.left = pct + '%';
  if ($('p-chapter')) $('p-chapter').textContent = '第 ' + (state.chapter + 1) + ' 章 · ' + chapterTitleOf(state.current, state.chapter);
  if ($('p-rate')) $('p-rate').textContent = state.rate + 'x';
  if ($('p-rate-label')) $('p-rate-label').textContent = state.rate + 'x';
  if ($('mini-progress')) $('mini-progress').style.width = pct + '%';
}

function applyRate(rate) {
  state.rate = Number(rate) || 1;
  audio.playbackRate = state.rate;
  localStorage.setItem('sy_rate', String(state.rate));
}

function setupSleep(minutes) {
  state.sleepMin = Math.max(0, Number(minutes) || 0);
  localStorage.setItem('sy_sleep', String(state.sleepMin));
  if (state.sleepTimer) clearInterval(state.sleepTimer);
  state.sleepTimer = null;
  if (state.sleepMin > 0) {
    const until = Date.now() + state.sleepMin * 60000;
    state.sleepTimer = setInterval(() => {
      if (Date.now() >= until) {
        audio.pause();
        clearInterval(state.sleepTimer);
        state.sleepTimer = null;
        state.sleepMin = 0;
        localStorage.removeItem('sy_sleep');
        toast('定时关闭已生效');
        if (route().name === 'player') render();
      }
    }, 1000);
    toast(state.sleepMin + ' 分钟后自动停止');
  } else {
    toast('已取消定时关闭');
  }
}

function cycleSleep() {
  const seq = [0, 15, 30, 60, 90];
  const idx = seq.indexOf(state.sleepMin);
  setupSleep(seq[(idx + 1) % seq.length]);
  if (route().name === 'player') render();
}

audio.addEventListener('timeupdate', () => {
  maybeSkipOutro();
  syncPlayerUI();
  syncPlayerText();
  if (state.playing && Date.now() - progressSavedAt > 10000) {
    progressSavedAt = Date.now();
    saveProgress(false);
  }
});
audio.addEventListener('play', () => {
  state.playing = true;
  syncMini();
  syncPlayerUI();
  syncBgmWithPlayback(true);
  // 播放页不整页重绘，避免把已加载的字幕 DOM 与滚动位置清掉
  if (route().name === 'book') render();
});
audio.addEventListener('pause', () => {
  state.playing = false;
  syncMini();
  syncPlayerUI();
  syncBgmWithPlayback(false);
  saveProgress(false);
});
audio.addEventListener('durationchange', syncPlayerUI);
audio.addEventListener('loadedmetadata', syncPlayerUI);
audio.addEventListener('ended', () => {
  const book = state.current;
  const outro = Number((book && book.skip_outro) || 0);
  saveProgress(false);
  if (book && state.chapter + 1 < (book.chapters || []).length) {
    nextChapter();
  } else if (state.queue.length && state.queueIndex >= 0 && state.queueIndex + 1 < state.queue.length) {
    nextChapter();
  } else {
    state.playing = false;
    syncMini();
    if (route().name === 'player') render();
  }
  void outro;
});
window.addEventListener('pagehide', () => { if (state.current) saveProgress(true); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && state.current) saveProgress(true);
});

/* ------------------------------------------------------------------ 我的 / 设置 */

function bgmTrackTitle(url) {
  if (!url) return '';
  const hit = state.bgm.list.find((t) => t.url === url);
  return hit ? hit.title : '';
}

function bgmSummary() {
  const b = state.bgm;
  const count = b.list.length;
  const stateText = b.enabled ? '已启用' : '已关闭';
  let text = '背景音乐 · ' + stateText;
  if (count) text += ' / ' + count + ' 首';
  const title = bgmTrackTitle(b.current);
  if (b.enabled && title) text += ' · 当前：' + title;
  if (b.enabled) text += ' · 音量 ' + Math.round(b.volume * 100) + '%';
  return text;
}

function viewMine() {
  const u = state.user || {};
  const isAdmin = u.role === 'admin';
  const bgm = state.bgm;
  const bgmEmpty = !bgm.dir;
  const bgmNoTracks = !!bgm.dir && !bgm.list.length;
  const userRows = state.users.filter((item) => item.id !== u.id).map((item) => {
    return '<div class="user-row"><img src="' + esc(appUrl(item.avatar_url || ('/api/avatar/' + item.id))) + '" alt="">' +
      '<div class="grow"><div class="name">' + esc(item.username) + '</div>' +
      '<div class="sub">' + (item.role === 'admin' ? '管理员' : '书友') + '</div></div>' +
      (isAdmin ? '<button class="icon-btn tiny danger-text" data-act="user-del" data-id="' + esc(item.id) + '" title="删除账号">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13"/></svg></button>' : '') +
      '</div>';
  }).join('');
  const roleLabels = { book: '有声书库', bgm: '背景音乐', media: '通用媒体' };
  const roleEntry = (path) => (state.dirRoles || []).find((item) =>
    String(item.path || '').replace(/[\\/]+$/, '') === String(path || '').replace(/[\\/]+$/, ''));
  const roleBadge = (role) => '<span class="role-badge role-' + esc(role || 'book') + '">' +
    esc(roleLabels[role || 'book'] || role) + '</span>';
  const rootRows = (state.roots || []).filter((r) => {
    const path = typeof r === 'string' ? r : (r.path || '');
    return !roleEntry(path);
  }).map((r) => {
    const path = typeof r === 'string' ? r : (r.path || '');
    const count = typeof r === 'object' && r.books != null ? (r.books + ' 本') : '';
    return '<div class="form-row"><div class="label">飞牛授权目录</div>' +
      '<div class="hint mono" style="word-break:break-all">' + esc(path) + (count ? ' · ' + count : '') +
      ' ' + roleBadge('book') + '</div></div>';
  }).join('');
  const extraRows = (state.dirRoles || []).map((item, i) => {
    const role = item.role || 'book';
    return '<div class="user-row"><div class="grow"><div class="name mono" style="font-size:12px;word-break:break-all">' + esc(item.path || '') + '</div>' +
      '<div class="sub">' + roleBadge(role) + (role === 'bgm' ? ' 书库扫描会跳过此目录' : '') + '</div></div>' +
      '<select class="role-select" data-root-role data-index="' + i + '" aria-label="目录角色">' +
      ['book', 'bgm', 'media'].map((key) =>
        '<option value="' + key + '"' + (role === key ? ' selected' : '') + '>' + roleLabels[key] + '</option>').join('') +
      '</select>' +
      '<button class="icon-btn tiny danger-text" data-act="root-del" data-index="' + i + '" title="移除">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>';
  }).join('');
  const report = state.scanReport || { scanned: [], skipped: [], duplicates: 0 };
  const scanReportRow = '<div class="form-row"><div><div class="label">上次扫描范围</div>' +
    '<div class="hint">已扫 ' + (report.scanned || []).length + ' 个根目录 · 跳过 ' + (report.skipped || []).length +
    ' 个目录 · 判重 ' + (report.duplicates || 0) + ' 本</div></div>' +
    '<div class="scan-scope mono">' +
    ((report.scanned || []).length ? '<div>扫描：' + (report.scanned || []).map(esc).join('、') + '</div>' : '<div>扫描：—</div>') +
    ((report.skipped || []).length ? '<div class="skip">跳过：' + (report.skipped || []).map(esc).join('、') + '</div>' : '') +
    '</div></div>';

  const bgmBody = bgm.open
    ? '<div class="accordion-body">' +
      '<div class="row"><div class="txt">音乐目录' +
      (bgmEmpty ? '<div class="hint">还没有选择目录。选择后目录内的音频会自动出现在播放页的背景音乐里。</div>' : '') +
      '</div><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end">' +
      '<span class="dir-pill"><code>' + esc(bgm.dir || '未选择目录') + '</code>' +
      (bgm.dir ? '<span class="count">· ' + bgm.list.length + ' 首</span>' : '') + '</span>' +
      '<button class="btn btn-ghost btn-sm" data-act="pick-bgm-dir">选择目录</button></div></div>' +
      (bgmNoTracks ? '<div class="empty-note"><b>目录内没有可用的音频</b><span>支持 mp3 / m4a / ogg / flac / wav 等格式，放入文件后重新扫描即可。</span></div>' : '') +
      (!bgm.dir ? '<div class="empty-note"><b>背景音乐尚未启用</b><span>先选择一个音乐目录，再打开右侧开关。</span></div>' : '') +
      '<div class="row"><div class="txt">启用背景音乐<div class="hint">播放有声书时同步播放背景音乐，暂停时一起暂停。</div></div>' +
      '<button class="switch ' + (bgm.enabled ? 'on' : '') + '" data-act="bgm-toggle" aria-label="启用背景音乐"></button></div>' +
      '<div class="row"><div class="txt">音量<div class="hint">仅作用于背景音乐，不影响朗读音量。</div></div>' +
      '<input type="range" min="0" max="0.6" step="0.01" value="' + bgm.volume + '" data-bgm-volume style="max-width:180px"></div>' +
      '<div class="row"><div class="txt">随机播放<div class="hint">每首播完随机切换下一首。</div></div>' +
      '<button class="btn btn-ghost btn-sm" data-act="bgm-rand">换一首</button></div>' +
      '<div class="drawer-actions"><button class="btn btn-soft btn-sm" data-act="bgm-tracks">管理曲目（' + bgm.list.length + ' 首）</button></div>' +
      '</div>'
    : '';

  return '<div class="page-title">我的</div>' +
    '<div class="page-sub">账号、播放偏好与书库设置都在这里。</div>' +
    '<div class="settings-grid"><div class="settings-nav">' +
    '<a data-act="jump" data-target="sec-account" class="active">账号与安全</a>' +
    '<a data-act="jump" data-target="sec-play">播放偏好</a>' +
    '<a data-act="jump" data-target="sec-bgm">背景音乐</a>' +
    '<a data-act="jump" data-target="sec-library">书库与扫描</a>' +
    '<a data-act="jump" data-target="sec-connect">连接与扫码</a>' +
    '<a data-act="jump" data-target="sec-about">关于</a>' +
    '</div><div>' +

    '<section class="panel" id="sec-account"><div class="panel-head"><div><h2>账号与安全</h2>' +
    '<p>多账号共用同一个书库，播放进度、收藏与书单按账号独立保存。</p></div>' +
    '<button class="btn btn-ghost btn-sm" data-act="avatar-upload">更换头像</button></div>' +
    '<div class="user-row"><img src="' + esc(appUrl(u.avatar_url || ('/api/avatar/' + (u.id || '')))) + '" alt="" style="width:46px;height:46px">' +
    '<div class="grow"><div class="name">' + esc(u.username || '—') + '</div>' +
    '<div class="sub">' + (u.role === 'admin' ? '管理员' : '书友') + '</div></div>' +
    '<span class="badge gray">当前登录</span></div>' +
    (isAdmin ? userRows : '') +
    '<div class="inline-form" data-role="user-add"' + (isAdmin ? '' : ' style="display:none"') + '>' +
    '<input id="new-user" placeholder="新账号用户名" autocomplete="off">' +
    '<input id="new-pass" type="password" placeholder="密码" autocomplete="off">' +
    '<select id="new-role"><option value="user">书友</option><option value="admin">管理员</option></select>' +
    '<button class="btn btn-primary btn-sm" data-act="user-add">添加账号</button></div>' +
    '<div class="inline-form"><input id="self-pass" type="password" placeholder="设置新密码（留空不改）" autocomplete="off">' +
    '<button class="btn btn-ghost btn-sm" data-act="self-pass">修改我的密码</button></div>' +
    '</section>' +

    '<section class="panel" id="sec-play"><div class="panel-head"><div><h2>播放偏好</h2>' +
    '<p>倍速与定时关闭在当前设备生效；跳过片头片尾按书籍单独设置。</p></div></div>' +
    '<div class="form-row"><div><div class="label">默认倍速</div><div class="hint">打开新章节时自动应用</div></div>' +
    '<div class="segmented">' + [0.75, 1, 1.25, 1.5, 2].map((v) =>
      '<button class="' + (Math.abs(state.rate - v) < 0.01 ? 'active' : '') + '" data-act="rate" data-rate="' + v + '">' + v + 'x</button>').join('') + '</div></div>' +
    '<div class="form-row"><div><div class="label">定时关闭</div><div class="hint">' + (state.sleepMin ? state.sleepMin + ' 分钟后暂停播放' : '未开启') + '</div></div>' +
    '<button class="btn btn-ghost btn-sm" data-act="sleep" data-min="' + (state.sleepMin || 0) + '">' + (state.sleepMin ? '修改' : '设置') + '</button></div>' +
    '<div class="form-row"><div><div class="label">清空播放进度</div><div class="hint">只清空当前书籍的续播位置，不影响收藏</div></div>' +
    '<button class="btn btn-ghost btn-sm danger-text" data-act="clear-progress" data-id="' + esc((state.current && state.current.id) || '') + '">清空当前书</button></div>' +
    '</section>' +

    '<section class="panel" id="sec-bgm"><div class="accordion" style="border-top:none;padding-top:0">' +
    '<button class="accordion-head" data-act="bgm-open" aria-expanded="' + (bgm.open ? 'true' : 'false') + '">' +
    '<span class="acc-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18.5" r="2.5"/><circle cx="16.5" cy="16.5" r="2.5"/></svg></span>' +
    '<span class="grow"><span class="acc-title">背景音乐</span>' +
    '<span class="acc-summary">' + esc(bgmSummary()) + '</span></span>' +
    '<span class="state ' + (bgm.enabled ? '' : 'off') + '">' + (bgm.enabled ? '已启用' : '已关闭') + '</span>' +
    '<svg class="bgm-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transform:rotate(' + (bgm.open ? '90' : '0') + 'deg)"><path d="M9 6l6 6-6 6"/></svg>' +
    '</button>' + bgmBody + '</div></section>' +

    '<section class="panel" id="sec-library"><div class="panel-head"><div><h2>书库与扫描</h2>' +
    '<p>书库目录由飞牛授权文件夹决定，可追加其他授权目录。扫描在后台执行，完成后自动刷新。</p></div>' +
    '<button class="btn btn-primary btn-sm" data-act="rescan">重新扫描</button></div>' +
    rootRows + extraRows +
    '<div class="inline-form"><input id="new-root" placeholder="追加目录的绝对路径，如 /vol2/1000/有声书" autocomplete="off">' +
    '<select id="new-root-role"><option value="book">有声书库</option><option value="bgm">背景音乐</option><option value="media">通用媒体</option></select>' +
    '<button class="btn btn-ghost btn-sm" data-act="root-add">追加目录</button></div>' +
    '<div class="hint" style="margin-top:8px">背景音乐目录会被有声书扫描整棵跳过；通用媒体同时参与书库与背景音乐。</div>' +
    scanReportRow +
    '<div class="form-row" style="margin-top:16px"><div><div class="label">在线刮削接口</div>' +
    '<div class="hint">留空使用默认地址；需要服务端可访问该接口</div></div>' +
    '<div style="display:flex;gap:8px"><input id="scrape-base" class="mono" value="' + esc(state.scrapeBase || '') + '" style="width:260px;height:34px;border-radius:9px;border:1px solid var(--line-strong);background:var(--bg);color:var(--ink);padding:0 10px">' +
    '<button class="btn btn-ghost btn-sm" data-act="save-scrape">保存</button></div></div>' +
    '<div class="form-row"><div><div class="label">扫描状态</div><div class="hint">' +
    (state.scanning ? '正在扫描…' : (state.scannedAt ? '完成于 ' + esc(fmtDate(state.scannedAt)) : '尚未扫描')) +
    '</div></div></div></section>' +

    '<section class="panel" id="sec-connect"><div class="panel-head"><div><h2>连接与扫码</h2>' +
    '<p>手机 App 可通过扫码连接本机，自动带上一次性登录票据。</p></div>' +
    '<button class="btn btn-primary btn-sm" data-act="connect">显示二维码</button></div>' +
    '<div class="form-row"><div><div class="label">当前地址</div><div class="hint">页面按相对路径工作，以下地址均可直接访问</div></div>' +
    '<code class="mono" style="font-size:11.5px;word-break:break-all;max-width:420px;text-align:right">' + esc(window.location.origin + APP_BASE) + '</code></div>' +
    '<div class="form-row"><div><div class="label">外网地址</div><div class="hint">用于二维码与客户端；留空则自动检测</div></div>' +
    '<div style="display:flex;gap:8px"><input id="public-url" class="mono" value="' + esc(state.publicUrl || '') + '" placeholder="https://…" style="width:260px;height:34px;border-radius:9px;border:1px solid var(--line-strong);background:var(--bg);color:var(--ink);padding:0 10px">' +
    '<button class="btn btn-ghost btn-sm" data-act="save-public">保存</button></div></div>' +
    '</section>' +

    '<section class="panel" id="sec-about"><div class="panel-head"><div><h2>关于书中有音</h2>' +
    '<p>Python 标准库实现的本地有声书服务，零第三方依赖。</p></div></div>' +
    '<div class="form-row"><div class="label">版本</div><div class="hint" id="about-version">—</div></div>' +
    '<div class="form-row"><div class="label">数据与隐私</div><div class="hint" style="max-width:520px;text-align:right">书库、进度与收藏均保存在本机数据目录，不上传任何媒体文件。</div></div>' +
    '<div class="form-row"><div class="label">账户</div><div class="hint">' + esc(u.username || '') + ' · ' + (u.role === 'admin' ? '管理员' : '书友') + '</div></div>' +
    '<div class="drawer-actions"><button class="btn btn-ghost btn-sm" data-act="logout">退出登录</button></div>' +
    '</section>' +

    '</div></div>';
}

let mineFetchedAt = 0;
let mineFetching = false;

function ensureMineData() {
  if (mineFetching) return;
  if (Date.now() - mineFetchedAt < 6000) return;
  mineFetching = true;
  Promise.all([
    loadSettings().catch(() => {}),
    loadUsers().catch(() => {}),
    loadBgm().catch(() => {}),
    loadPlaylists().catch(() => {}),
  ]).then(() => {
    mineFetchedAt = Date.now();
    mineFetching = false;
    if (route().name === 'mine') { $('view').innerHTML = viewMine(); }
  }).catch(() => { mineFetching = false; });
}

/* ------------------------------------------------------------------ 背景音乐 */

function bgmApply(play) {
  if (!state.bgm.enabled) {
    bgmAudio.pause();
    return;
  }
  const url = state.bgm.current || (state.bgm.list[0] && state.bgm.list[0].url) || '';
  if (!url) return;
  const full = appUrl(url);
  if (!bgmAudio.src || bgmAudio.src.indexOf(full) < 0) {
    bgmAudio.src = full;
    bgmAudio.load();
  }
  bgmAudio.volume = Math.max(0, Math.min(1, state.bgm.volume));
  if (play) bgmAudio.play().catch(() => {});
  else bgmAudio.pause();
}

function syncBgmWithPlayback(playing) {
  bgmApply(playing && !audio.paused);
}

function bgmToggle() {
  if (!state.bgm.enabled) {
    if (!state.bgm.dir) {
      openBgmPicker('');
      toast('先选择一个音乐目录');
      return;
    }
    if (!state.bgm.list.length) {
      openBgmPicker(state.bgm.dir);
      toast('目录里还没有音频');
      return;
    }
    state.bgm.enabled = true;
  } else {
    state.bgm.enabled = false;
    bgmAudio.pause();
  }
  localStorage.setItem('sy_bgm_enabled', state.bgm.enabled ? '1' : '0');
  bgmApply(state.bgm.enabled && state.playing);
  if (route().name === 'mine') $('view').innerHTML = viewMine();
  if (route().name === 'player') { $('view').innerHTML = viewPlayer(); syncPlayerUI(); }
}

function bgmSetVolume(value) {
  state.bgm.volume = Math.max(0, Math.min(1, Number(value) || 0));
  localStorage.setItem('sy_bgm_volume', String(state.bgm.volume));
  bgmAudio.volume = state.bgm.volume;
  if (route().name === 'mine') {
    const summary = $('view') && $('view').querySelector('.acc-summary');
    if (summary) summary.textContent = bgmSummary();
  }
}

function bgmRandomTrack() {
  const list = state.bgm.list;
  if (!list.length) { toast('还没有可用的曲目'); return; }
  const others = list.filter((t) => t.url !== state.bgm.current);
  const pick = (others.length ? others : list)[Math.floor(Math.random() * (others.length || list.length))];
  state.bgm.current = pick.url;
  localStorage.setItem('sy_bgm_current', pick.url);
  bgmApply(state.playing);
  toast('已切换：' + pick.title);
  if (route().name === 'mine') $('view').innerHTML = viewMine();
}

function openBgmPicker(path) {
  api('/api/bgm/browse' + (path ? '?p=' + encodeURIComponent(path) : '')).then((data) => {
    const dirs = data.dirs || [];
    openModal('选择音乐目录',
      '<div class="sub" style="font-size:12px;color:var(--muted);margin-bottom:12px;word-break:break-all">当前：' +
      esc(data.path || '授权根目录') + '</div>' +
      '<div class="drawer-list">' +
      (data.parent ? '<div class="list-row" data-act="bgm-enter" data-path="' + esc(data.parent) + '" style="cursor:pointer">' +
        '<div class="grow"><div class="name">↑ 上一级</div></div></div>' : '') +
      dirs.map((d) => '<div class="list-row" data-act="bgm-enter" data-path="' + esc(d.path) + '" style="cursor:pointer">' +
        '<div class="list-thumb" style="--c1:#6a6f66;--c2:#3a3e39">夹</div>' +
        '<div class="grow"><div class="name">' + esc(d.name) + '</div><div class="sub mono" style="font-size:11px">' + esc(d.path) + '</div></div>' +
        '<span class="tail">进入 →</span></div>').join('') +
      '</div>' +
      (data.path ? '<div class="drawer-actions"><button class="btn btn-primary btn-sm" data-act="bgm-use" data-path="' + esc(data.path) + '">使用此目录</button></div>' : '') +
      '<div class="hint" style="margin-top:14px">只能选择飞牛已授权的文件夹。目录内音频会递归查找（最多 3 层）。</div>');
  }).catch((err) => toast('打开目录失败：' + err.message));
}

function bgmUseDir(path) {
  api('/api/bgm/dir', { method: 'POST', body: JSON.stringify({ path: path }) }).then((data) => {
    state.bgm.dir = data.dir || path;
    return loadBgm();
  }).then(() => {
    closeModal();
    toast('音乐目录已保存，共 ' + state.bgm.list.length + ' 首');
    if (route().name === 'mine') $('view').innerHTML = viewMine();
    if (state.bgm.enabled) bgmApply(state.playing);
  }).catch((err) => toast(err.message));
}

function openTrackDrawer() {
  const rows = state.bgm.list.map((t) =>
    '<div class="list-row" data-act="bgm-use-track" data-url="' + esc(t.url) + '" style="cursor:pointer' +
    (t.url === state.bgm.current ? ';background:var(--accent-soft)' : '') + '">' +
    '<div class="list-thumb" style="--c1:#b08a3e;--c2:#6d5323">音</div>' +
    '<div class="grow"><div class="name">' + esc(t.title) + '</div>' +
    '<div class="sub">' + (t.from === 'folder' ? '目录曲目' : '上传曲目') + ' · ' + fmtSize(t.size) + '</div></div>' +
    (t.from === 'folder' ? '' :
      '<button class="btn btn-ghost btn-sm" data-act="bgm-del" data-url="' + esc(t.url) + '">删除</button>') +
    (t.url === state.bgm.current ? '<span class="tail">当前</span>' : '<span class="tail">选用</span>') + '</div>'
  ).join('');
  openDrawer('<h3>背景音乐曲目</h3>' +
    '<div class="sub">' + (state.bgm.dir ? esc(state.bgm.dir) : '未选择目录') + ' · ' + state.bgm.list.length + ' 首</div>' +
    '<div class="drawer-list">' + (rows || '<div class="hint" style="padding:16px 0">目录内还没有音频文件</div>') + '</div>' +
    '<div class="drawer-actions"><button class="btn btn-ghost btn-sm" data-act="pick-bgm-dir">选择目录</button>' +
    '<button class="btn btn-ghost btn-sm" data-act="bgm-refresh">重新扫描</button></div>');
}

function pickBgmTrack(url) {
  state.bgm.current = url;
  localStorage.setItem('sy_bgm_current', url);
  if (state.bgm.enabled) bgmApply(state.playing);
  toast('已选用：' + bgmTrackTitle(url));
  openTrackDrawer();
  if (route().name === 'mine') $('view').innerHTML = viewMine();
}

/* 删除应用内上传/内置的曲目；目录曲目属于用户媒体库，不在这里删 */
function deleteBgmTrack(url) {
  const raw = String(url || '');
  if (!raw || raw.indexOf('/bgm/file?') === 0 || raw.indexOf('/bgm/') !== 0) return;
  let name = '';
  try { name = decodeURIComponent(raw.slice('/bgm/'.length)); } catch (e) { name = ''; }
  if (!name || name.indexOf('/') >= 0) return;
  if (!window.confirm('删除「' + name + '」？该文件会从应用数据目录移除，之后不再出现在列表里。')) return;
  api('/api/bgm/' + encodeURIComponent(name), { method: 'DELETE' }).then(() => {
    if (state.bgm.current === raw) {
      state.bgm.current = '';
      localStorage.removeItem('sy_bgm_current');
    }
    return loadBgm();
  }).then(() => {
    toast('已删除：' + name);
    if (state.bgm.enabled) bgmApply(state.playing);
    openTrackDrawer();
    if (route().name === 'mine') $('view').innerHTML = viewMine();
  }).catch((err) => toast(err.message));
}

/* ------------------------------------------------------------------ 书单操作 */

function createPlaylist() {
  const input = $('pl-new');
  const title = input ? input.value.trim() : '';
  if (!title) { toast('请填写书单名称'); return; }
  api('/api/playlists', { method: 'POST', body: JSON.stringify({ title: title }) }).then(() => {
    toast('书单已创建');
    return loadPlaylists();
  }).then(() => render()).catch((err) => toast(err.message));
}

function openPlaylistPicker(book) {
  const options = state.playlists.map((p) =>
    '<div class="picker-row" data-act="pl-add-to" data-id="' + esc(p.id) + '" data-book="' + esc(book.id) + '">' +
    '<div class="grow"><div class="name">' + esc(p.title) + '</div><div class="sub">' + (p.book_count || 0) + ' 本</div></div>' +
    '<span class="tail">加入 →</span></div>').join('');
  openModal('加入书单：' + book.title,
    '<div class="drawer-list">' + (options || '<div class="hint" style="padding:14px 0">还没有书单，先创建一个。</div>') + '</div>' +
    '<div class="drawer-actions"><button class="btn btn-primary btn-sm" data-act="pl-quick" data-book="' + esc(book.id) + '">新建书单并加入</button></div>');
}

function addToPlaylist(playlistId, bookId) {
  api('/api/playlists/' + encodeURIComponent(playlistId)).then((data) => {
    const pl = data.data || {};
    const ids = (pl.book_ids || []).slice();
    if (ids.indexOf(bookId) < 0) ids.push(bookId);
    return api('/api/playlists/' + encodeURIComponent(playlistId), {
      method: 'PUT', body: JSON.stringify({ book_ids: ids }),
    });
  }).then(() => {
    closeModal();
    toast('已加入书单');
    return loadPlaylists();
  }).then(() => { if (route().name === 'playlists') render(); })
    .catch((err) => toast('加入失败：' + err.message));
}

function quickPlaylist(bookId) {
  const name = '我的书单 ' + (state.playlists.length + 1);
  api('/api/playlists', { method: 'POST', body: JSON.stringify({ title: name, book_ids: [bookId] }) })
    .then(() => { closeModal(); toast('已创建「' + name + '」并加入'); return loadPlaylists(); })
    .then(() => { if (route().name === 'playlists') render(); })
    .catch((err) => toast(err.message));
}

function removeFromPlaylist(playlistId, bookId) {
  api('/api/playlists/' + encodeURIComponent(playlistId)).then((data) => {
    const ids = ((data.data && data.data.book_ids) || []).filter((id) => id !== bookId);
    return api('/api/playlists/' + encodeURIComponent(playlistId), {
      method: 'PUT', body: JSON.stringify({ book_ids: ids }),
    });
  }).then(() => { toast('已移出书单'); return loadPlaylists(); })
    .then(() => render()).catch((err) => toast(err.message));
}

function renamePlaylist(id) {
  const p = state.playlists.find((x) => x.id === id);
  const title = window.prompt('新的书单名称', p ? p.title : '');
  if (!title || !title.trim()) return;
  api('/api/playlists/' + encodeURIComponent(id), {
    method: 'PUT', body: JSON.stringify({ title: title.trim() }),
  }).then(() => { toast('已改名'); return loadPlaylists(); })
    .then(() => render()).catch((err) => toast(err.message));
}

function deletePlaylist(id) {
  if (!window.confirm('删除这个书单？书库里的书不会受影响。')) return;
  api('/api/playlists/' + encodeURIComponent(id), { method: 'DELETE' })
    .then(() => { toast('书单已删除'); return loadPlaylists(); })
    .then(() => go('#/playlists')).catch((err) => toast(err.message));
}

function playPlaylist(id) {
  api('/api/playlists/' + encodeURIComponent(id)).then((data) => {
    const books = ((data.data && data.data.books) || []).filter(Boolean);
    if (!books.length) { toast('书单里还没有书'); return; }
    return Promise.all(books.map((b) => detailOf(b.id))).then((details) => {
      const queue = [];
      details.forEach((book) => {
        if (!book.chapters || !book.chapters.length) return;
        const pr = book.progress || {};
        let start = Number(pr.chapter || 0);
        if (pr.finished) start = 0;
        queue.push({ bookId: book.id, index: start, title: chapterTitleOf(book, start), bookTitle: book.title });
      });
      if (!queue.length) { toast('书单里没有可播放的章节'); return; }
      playEntry(queue[0].bookId, queue[0].index, { queue: queue, queueIndex: 0, resume: true });
      go('#/player');
    });
  }).catch((err) => toast(err.message));
}

/* ------------------------------------------------------------------ 收藏 / 进度 */

function toggleFavorite(bookId) {
  const book = state.detailCache[bookId] || bookById(bookId);
  if (!book) return;
  const next = !book.favorite;
  api('/api/favorite', { method: 'POST', body: JSON.stringify({ book_id: bookId, favorite: next }) })
    .then(() => {
      book.favorite = next;
      const row = state.bookMap[bookId];
      if (row) row.favorite = next;
      toast(next ? '已收藏' : '已取消收藏');
      render();
    }).catch((err) => toast(err.message));
}

function clearProgress(bookId) {
  if (!bookId) { toast('先在播放页打开一本书'); return; }
  api('/api/progress/reset', { method: 'POST', body: JSON.stringify({ book_id: bookId }) })
    .then(() => {
      const cached = state.detailCache[bookId];
      if (cached && cached.progress) cached.progress = {};
      toast('播放进度已清空');
    }).catch((err) => toast(err.message));
}

/* ------------------------------------------------------------------ 刮削 */

function openScrape(bookId) {
  const book = state.detailCache[bookId] || bookById(bookId);
  if (!book) return;
  openModal('在线刮削：' + book.title,
    '<div class="inline-form"><input id="scrape-q" value="' + esc(book.title) + '" autocomplete="off">' +
    '<button class="btn btn-primary btn-sm" data-act="scrape-search" data-id="' + esc(bookId) + '">搜索</button></div>' +
    '<div class="hint" style="margin-top:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">' +
    '<label style="display:inline-flex;align-items:center;gap:6px"><input type="checkbox" id="scrape-update-chapters" checked> 同时更新章节标题</label>' +
    '<button class="btn btn-ghost btn-sm" data-act="chapters-reset" data-id="' + esc(bookId) + '">按书重置章节标题</button></div>' +
    '<div id="scrape-result" class="hint" style="margin-top:12px">搜索后选择一个匹配条目，再点下方「应用刮削结果」。</div>');
}

function scrapeCardHtml(it, index, selected) {
  return '<div class="item' + (selected ? ' selected' : '') + '" data-act="scrape-select" data-index="' + index + '">' +
    (it.cover_url ? '<img src="' + esc(appUrl('/api/image-proxy?url=' + encodeURIComponent(it.cover_url))) + '" alt="" loading="lazy">' : '<div style="aspect-ratio:3/4;background:var(--surface-2)"></div>') +
    '<p>' + esc(it.title || '未命名') + (it.author ? ' · ' + esc(it.author) : '') +
    '<span class="hint" style="margin-left:6px">' + esc(({ ximalaya: '喜马拉雅', lanren: '懒人听书', missevan: '猫耳 FM' })[it.source_id || it.source] || '') + '</span>' +
    '</p></div>';
}

function scrapeApplyBarHtml(bookId, reason, disabled) {
  return '<div class="scrape-actions">' +
    '<button id="scrape-apply-btn" class="btn btn-primary btn-sm" data-act="scrape-apply-selected" data-id="' + esc(bookId) + '"' +
    (disabled ? ' disabled' : '') + '>应用刮削结果</button>' +
    '<span id="scrape-apply-state" class="hint">' + esc(reason || '') + '</span>' +
    '</div><div id="scrape-apply-result" class="scrape-apply-result hint"></div>';
}

function scrapeSearch(bookId) {
  const q = $('scrape-q') ? $('scrape-q').value.trim() : '';
  if (!q) { toast('请输入关键词'); return; }
  const box = $('scrape-result');
  box.className = 'hint';
  box.innerHTML = '<div class="spinner" style="display:inline-block"></div> 搜索中…';
  state.scrapeItems = [];
  state.scrapeSelected = -1;
  api('/api/scrape/search?q=' + encodeURIComponent(q)).then((data) => {
    const items = data.items || data.results || [];
    state.scrapeItems = items;
    if (!items.length) {
      state.scrapeSelected = -1;
      box.className = 'hint';
      box.innerHTML = '<div class="empty-note">没有找到匹配条目' + (data.error ? '：' + esc(data.error) : '') + '</div>' +
        scrapeApplyBarHtml(bookId, '没有可应用的结果，请换个关键词重新搜索', true);
      return;
    }
    state.scrapeSelected = 0;
    box.className = '';
    box.innerHTML = '<div class="scrape-grid">' + items.slice(0, 12).map((it, i) =>
      scrapeCardHtml(it, i, i === 0)).join('') + '</div>' +
      scrapeApplyBarHtml(bookId, '已选中：' + (items[0].title || '未命名'), false);
  }).catch((err) => {
    state.scrapeItems = [];
    state.scrapeSelected = -1;
    box.className = 'hint';
    box.innerHTML = '<div class="empty-note">搜索失败：' + esc(err.message) + '</div>' +
      scrapeApplyBarHtml(bookId, '搜索失败，暂无可应用的结果', true);
  });
}

function selectScrapeItem(index) {
  const it = (state.scrapeItems || [])[index];
  if (!it) return;
  state.scrapeSelected = index;
  const grid = $('scrape-result') ? $('scrape-result').querySelector('.scrape-grid') : null;
  if (grid) {
    Array.prototype.forEach.call(grid.querySelectorAll('.item'), (node, i) => {
      node.classList.toggle('selected', i === index);
    });
  }
  const stateBox = $('scrape-apply-state');
  if (stateBox) stateBox.textContent = '已选中：' + (it.title || '未命名');
  const btn = $('scrape-apply-btn');
  if (btn && btn.dataset.busy !== '1') {
    btn.disabled = false;
    btn.textContent = '应用刮削结果';
  }
  const out = $('scrape-apply-result');
  if (out) out.innerHTML = '';
}

function applySelectedScrape(bookId) {
  const it = (state.scrapeItems || [])[state.scrapeSelected];
  const btn = $('scrape-apply-btn');
  const out = $('scrape-apply-result');
  const stateBox = $('scrape-apply-state');
  if (!it) {
    if (stateBox) stateBox.textContent = '没有可应用的结果，请先搜索并选择一条';
    return;
  }
  if (btn && btn.dataset.busy === '1') return;
  if (btn) {
    btn.dataset.busy = '1';
    btn.disabled = true;
    btn.textContent = '应用中…';
  }
  if (stateBox) stateBox.textContent = '正在提交，请稍候…';
  if (out) out.innerHTML = '';

  const book = state.detailCache[bookId] || bookById(bookId);
  const beforeTitle = book ? (book.title || '') : '';
  const source = it.source_id || it.source;
  const fields = {
    title: { value: it.title, source: source, external_id: it.id },
    author: { value: it.author || '', source: source, external_id: it.id },
    narrator: { value: it.narrator || '', source: source, external_id: it.id },
    description: { value: it.description || '', source: source, external_id: it.id },
    tags: { value: it.tags || '', source: source, external_id: it.id },
    cover: { value: it.cover_url || '', source: source, external_id: it.id },
  };
  const updateChapters = $('scrape-update-chapters') ? $('scrape-update-chapters').checked : false;
  api('/api/books/' + encodeURIComponent(bookId) + '/scrape-apply', {
    method: 'POST', body: JSON.stringify({ book: it, fields: fields, update_chapters: updateChapters }),
  }).then((data) => {
    delete state.detailCache[bookId];
    const ch = data && data.chapters;
    const cover = (data && data.cover) || {};
    const afterTitle = data && data.book ? (data.book.title || '') : '';
    let titleText;
    if (afterTitle && afterTitle !== beforeTitle) {
      titleText = '书名：已更新为「' + afterTitle + '」';
    } else if (afterTitle) {
      titleText = '书名：未变化（仍为「' + afterTitle + '」）';
    } else {
      titleText = '书名：未更新（服务端未返回书名）';
    }
    let coverText;
    if (cover.status === 'updated') {
      coverText = '封面：updated';
    } else if (cover.status === 'skipped_manual') {
      coverText = '封面：skipped_manual｜' + (cover.error || '已有手动/本地封面，已跳过');
    } else if (cover.status === 'failed') {
      coverText = '封面：failed｜' + (cover.error || '未知原因');
    } else {
      coverText = '封面：not_found' + (cover.error ? '｜' + cover.error : '');
    }
    let chapterText;
    if (!updateChapters) {
      chapterText = '章节：未勾选更新';
    } else if (ch && ch.ok) {
      chapterText = '章节：已对齐 ' + ch.applied + ' 条';
    } else {
      chapterText = '章节：failed｜' + ((ch && ch.error) || '未知原因');
    }
    if (out) {
      out.innerHTML = '<div style="line-height:1.9">' +
        '<p>' + esc(titleText) + '</p>' +
        '<p>' + esc(coverText) + '</p>' +
        '<p>' + esc(chapterText) + '</p></div>';
    }
    if (stateBox) stateBox.textContent = '已提交';
    if (btn) {
      btn.dataset.busy = '0';
      btn.textContent = '已应用';
      btn.disabled = true;
    }
    toast(coverText, 6000);
    return loadLibrary().then(() => { render(); });
  }).catch((err) => {
    if (out) {
      out.innerHTML = '<div style="line-height:1.9"><p>提交：failed｜' + esc(err.message) + '</p></div>';
    }
    if (stateBox) stateBox.textContent = '提交失败，可重试';
    if (btn) {
      btn.dataset.busy = '0';
      btn.textContent = '应用刮削结果';
      btn.disabled = false;
    }
  });
}

function resetChapterTitles(bookId) {
  api('/api/books/' + encodeURIComponent(bookId) + '/chapters/reset', { method: 'POST', body: '{}' })
    .then((data) => {
      delete state.detailCache[bookId];
      toast(data && data.had_override ? '已重置 ' + data.restored + ' 条章节标题' : '该书没有可重置的章节标题');
      return loadLibrary();
    }).then(() => { render(); }).catch((err) => toast(err.message));
}

/* ------------------------------------------------------------------ 账号 */

function addUser() {
  const username = ($('new-user') && $('new-user').value.trim()) || '';
  const password = ($('new-pass') && $('new-pass').value) || '';
  const role = ($('new-role') && $('new-role').value) || 'user';
  if (!username || !password) { toast('填写用户名和密码'); return; }
  api('/api/users', { method: 'POST', body: JSON.stringify({ username: username, password: password, role: role }) })
    .then(() => { toast('账号已添加'); mineFetchedAt = 0; ensureMineData(); })
    .catch((err) => toast(err.message));
}

function deleteUser(id) {
  if (!window.confirm('删除这个账号？其书单会一并移除。')) return;
  api('/api/users/' + encodeURIComponent(id), { method: 'DELETE' })
    .then(() => { toast('账号已删除'); mineFetchedAt = 0; ensureMineData(); })
    .catch((err) => toast(err.message));
}

function changeSelfPassword() {
  const input = $('self-pass');
  const password = input ? input.value : '';
  if (!password) { toast('请输入新密码'); return; }
  api('/api/users/' + encodeURIComponent(state.user.id), {
    method: 'PUT', body: JSON.stringify({ password: password }),
  }).then(() => { input.value = ''; toast('密码已更新'); })
    .catch((err) => toast(err.message));
}

function uploadAvatar(file) {
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) { toast('头像请小于 4MB'); return; }
  const reader = new FileReader();
  reader.onload = () => {
    api('/api/users/' + encodeURIComponent(state.user.id) + '/avatar', {
      method: 'POST', body: JSON.stringify({ data: reader.result }),
    }).then((data) => {
      state.user = data.user || state.user;
      paintUser();
      toast('头像已更新');
      mineFetchedAt = 0;
      ensureMineData();
    }).catch((err) => toast(err.message));
  };
  reader.readAsDataURL(file);
}

/* ------------------------------------------------------------------ 设置保存 */

function saveDirRoles(entries, message) {
  return api('/api/settings', { method: 'POST', body: JSON.stringify({ dir_roles: entries, rescan: true }) })
    .then((data) => {
      state.dirRoles = data.dir_roles || entries;
      (data.warnings || []).forEach((text) => toast(text));
      toast(message || '目录已更新，正在重新扫描');
      mineFetchedAt = 0;
      ensureMineData();
      loadLibrary().catch(() => {});
    })
    .catch((err) => toast(err.message));
}

function addRoot() {
  const input = $('new-root');
  const value = input ? input.value.trim() : '';
  if (!value) { toast('请输入目录路径'); return; }
  const role = ($('new-root-role') && $('new-root-role').value) || 'book';
  const next = (state.dirRoles || []).concat([{ path: value, role: role }]);
  saveDirRoles(next, '目录已追加，正在重新扫描');
}

function updateDirRole(index, role) {
  const next = (state.dirRoles || []).map((item, i) =>
    i === index ? { path: item.path, role: role } : item);
  saveDirRoles(next, '目录角色已更新，正在重新扫描');
}

function removeRoot(index) {
  const next = (state.dirRoles || []).filter((_, i) => i !== index);
  saveDirRoles(next, '已移除目录，索引同步清理中');
}

function saveScrapeBase() {
  const value = ($('scrape-base') && $('scrape-base').value.trim()) || '';
  api('/api/settings', { method: 'POST', body: JSON.stringify({ scrape: { api_base: value } }) })
    .then(() => { state.scrapeBase = value; toast('刮削接口已保存'); })
    .catch((err) => toast(err.message));
}

function savePublicUrl() {
  const value = ($('public-url') && $('public-url').value.trim()) || '';
  api('/api/settings', { method: 'POST', body: JSON.stringify({ public_url: value }) })
    .then((data) => { state.publicUrl = data.public_url || value; toast('外网地址已保存'); })
    .catch((err) => toast(err.message));
}

/* ------------------------------------------------------------------ 连接与扫码 */

function openConnect() {
  openModal('扫码连接', '<div id="connect-body"><div class="spinner" style="display:inline-block"></div> 获取本机地址…</div>');
  Promise.all([
    api('/api/network').catch(() => ({ urls: [] })),
    api('/api/auth/ticket', { method: 'POST', body: '{}' }).catch(() => null),
  ]).then(([net, ticket]) => {
    const urls = [];
    if (state.publicUrl) urls.push({ family: 'public', url: state.publicUrl });
    if (net && net.public) urls.push({ family: 'public', url: net.public });
    ((net && net.urls) || []).forEach((u) => urls.push(u));
    if (!urls.length) {
      $('connect-body').innerHTML = '<div class="empty-note">未检测到可用地址。可手动填写外网地址后再试。</div>';
      return;
    }
    const primary = urls[0];
    const payload = ticket && ticket.ticket && ticket.user
      ? JSON.stringify({ app: 'shuzhongyouyin', url: primary.url, ticket: ticket.ticket, user: ticket.user })
      : primary.url;
    let svg = '';
    try {
      const qr = qrcode(0, 'M');
      qr.addData(payload);
      qr.make();
      svg = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
    } catch (err) {
      svg = '<div class="hint">二维码生成失败</div>';
    }
    $('connect-body').innerHTML = '<div class="qr-wrap"><div class="qr-box">' + svg + '</div>' +
      '<div class="qr-meta">用「书中有音」手机端扫码，可携带一次性登录票据直接进入。<br>' +
      (ticket ? '票据有效期短，过期后重新打开本窗口即可。' : '当前未生成票据，扫码仅填入地址。') +
      '<br><br><b>可用地址</b><br>' +
      urls.map((u) => '<code>' + esc(u.url) + '</code>').join('<br>') +
      '</div></div>' +
      '<div class="hint" style="margin-top:14px">远程访问需在飞牛网关或反代放行 Cookie、WebSocket 与 X-Forwarded-* 头。</div>';
  });
}

/* ------------------------------------------------------------------ 抽屉 / 弹层 */

function openModal(title, html) {
  $('modal-title').textContent = title;
  $('modal-body').innerHTML = html;
  $('modal').classList.add('show');
}

function closeModal() {
  $('modal').classList.remove('show');
  $('modal-body').innerHTML = '';
}

function openDrawer(html) {
  $('drawer-body').innerHTML = html;
  $('drawer').classList.add('show');
}

function closeDrawer() {
  $('drawer').classList.remove('show');
  $('drawer-body').innerHTML = '';
}

/* ------------------------------------------------------------------ 事件 */

const ACTIONS = {
  'goto-library': () => go('#/library'),
  'goto-mine': () => go('#/mine'),
  'goto-playlists': () => go('#/playlists'),
  'open-book': (el) => go('#/book/' + el.dataset.id),
  'filter': (el) => { state.filter = el.dataset.key; render(); },
  'layout': (el) => { state.layout = el.dataset.key; localStorage.setItem('sy_layout', state.layout); render(); },
  'resume': (el) => { resumeBook(el.dataset.id); },
  'restart': (el) => { restartBook(el.dataset.id); },
  'play-chapter': (el) => { playEntry(el.dataset.id, Number(el.dataset.index), { resume: true }); },
  'toggle-fav': (el) => toggleFavorite(el.dataset.id),
  'book-tab': (el) => { state.bookTab = el.dataset.key; render(); },
  'rate': (el) => { applyRate(el.dataset.rate); render(); },
  'toggle': togglePlay,
  'prev': prevChapter,
  'next': nextChapter,
  'seek-back': () => seekBy(-15),
  'seek-fwd': () => seekBy(15),
  'sleep': cycleSleep,
  'queue-jump': (el) => {
    const i = Number(el.dataset.index);
    if (state.queue[i]) playEntry(state.queue[i].bookId, state.queue[i].index, { queue: state.queue, queueIndex: i });
  },
  'bgm-toggle': bgmToggle,
  'bgm-open': () => {
    state.bgm.open = !state.bgm.open;
    localStorage.setItem('sy_bgm_open', state.bgm.open ? '1' : '0');
    $('view').innerHTML = viewMine();
  },
  'pick-bgm-dir': () => openBgmPicker(state.bgm.dir || ''),
  'bgm-enter': (el) => openBgmPicker(el.dataset.path),
  'bgm-use': (el) => bgmUseDir(el.dataset.path),
  'bgm-tracks': openTrackDrawer,
  'bgm-use-track': (el) => pickBgmTrack(el.dataset.url),
  'bgm-del': (el) => deleteBgmTrack(el.dataset.url),
  'bgm-refresh': () => loadBgm().then(() => { toast('曲目已刷新'); openTrackDrawer(); }),
  'bgm-rand': bgmRandomTrack,
  'pl-create': createPlaylist,
  'open-playlist': (el) => go('#/playlist/' + el.dataset.id),
  'pl-play': (el) => playPlaylist(el.dataset.id),
  'pl-remove': (el) => removeFromPlaylist(el.dataset.id, el.dataset.book),
  'pl-rename': (el) => renamePlaylist(el.dataset.id),
  'pl-delete': (el) => deletePlaylist(el.dataset.id),
  'add-playlist': (el) => {
    const book = state.detailCache[el.dataset.id] || bookById(el.dataset.id);
    if (book) openPlaylistPicker(book);
  },
  'pl-add-to': (el) => addToPlaylist(el.dataset.id, el.dataset.book),
  'pl-quick': (el) => quickPlaylist(el.dataset.book),
  'scrape': (el) => openScrape(el.dataset.id),
  'scrape-search': (el) => scrapeSearch(el.dataset.id),
  'scrape-select': (el) => selectScrapeItem(Number(el.dataset.index)),
  'scrape-apply-selected': (el) => applySelectedScrape(el.dataset.id),
  'chapters-reset': (el) => resetChapterTitles(el.dataset.id),
  'chapter-group-pick': (el) => pickChapterGroup(el),
  'status-refresh-one': (el) => refreshBookStatus(el.dataset.id),
  'status-refresh-all': () => refreshAllStatus(),
  'skip-save': (el) => saveSkipCustom(el),
  'clamp-toggle': (el) => toggleClamp(el),
  'user-add': addUser,
  'user-del': (el) => deleteUser(el.dataset.id),
  'self-pass': changeSelfPassword,
  'avatar-upload': () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.onchange = () => uploadAvatar(input.files && input.files[0]);
    input.click();
  },
  'root-add': addRoot,
  'root-del': (el) => removeRoot(Number(el.dataset.index)),
  'player-text': () => {
    state.textOn = !state.textOn;
    localStorage.setItem('sy_text', state.textOn ? '1' : '0');
    paintPlayerText();
    if (state.textOn) syncPlayerText();
  },
  'player-seek': (el) => {
    const seg = (state.subtitle.segments || [])[Number(el.dataset.index)];
    if (!seg) return;
    audio.currentTime = Number(seg.start) || 0;
    syncPlayerUI();
    syncPlayerText();
    if (audio.paused) audio.play().catch(() => {});
  },
  'player-chapters': openPlayerChapters,
  'player-chapter': (el) => {
    const index = Number(el.dataset.index);
    const book = state.current;
    if (!book) return;
    closeModal();
    const queue = (book.chapters || []).map((c, i) => ({ bookId: book.id, index: i, title: c.title, bookTitle: book.title }));
    playEntry(book.id, index, { queue: queue, queueIndex: index });
  },
  'player-more': openPlayerMore,
  'player-rate': cyclePlayerRate,
  'save-scrape': saveScrapeBase,
  'save-public': savePublicUrl,
  'rescan': rescan,
  'connect': openConnect,
  'clear-progress': (el) => clearProgress(el.dataset.id),
  'logout': logout,
  'jump': (el) => {
    const target = $(el.dataset.target);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    $$('.settings-nav a').forEach((a) => a.classList.toggle('active', a === el));
  },
};

document.addEventListener('click', (event) => {
  const close = event.target.closest('[data-close]');
  if (close) {
    if (close.dataset.close === 'modal') closeModal();
    if (close.dataset.close === 'drawer') closeDrawer();
    return;
  }
  const bar = event.target.closest('#p-bar');
  if (bar && audio.duration) {
    const rect = bar.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    audio.currentTime = ratio * audio.duration;
    syncPlayerUI();
    syncPlayerText();
    return;
  }
  const el = event.target.closest('[data-act]');
  if (!el) return;
  const fn = ACTIONS[el.dataset.act];
  if (fn) {
    event.preventDefault();
    fn(el);
  }
});

document.addEventListener('input', (event) => {
  if (event.target.matches('[data-bgm-volume]')) bgmSetVolume(event.target.value);
  if (event.target.id === 'search') {
    state.query = event.target.value;
    if (route().name !== 'library') go('#/library');
    else { $('view').innerHTML = viewLibrary(); }
  }
  if (event.target.id === 'volume') audio.volume = Number(event.target.value);
  if (event.target.id === 'pl-ch-filter') paintPlayerChapterList(event.target.value);
});

document.addEventListener('change', (event) => {
  if (event.target.matches('[data-root-role]')) {
    updateDirRole(Number(event.target.dataset.index), event.target.value);
    return;
  }
  const skip = event.target.closest('[data-skip]');
  if (skip) {
    const book = state.detailCache[skip.dataset.id];
    if (!book) return;
    const key = skip.dataset.skip === 'intro' ? 'skip_intro' : 'skip_outro';
    if (skip.value === 'custom') {
      skip.value = String(Number(book[key] || 0));
      openSkipCustom(book, key);
      return;
    }
    book[key] = Number(skip.value);
    api('/api/books/' + encodeURIComponent(skip.dataset.id), {
      method: 'PUT', body: JSON.stringify({ [key]: Number(skip.value) }),
    }).then(() => toast('已保存跳过设置')).catch((err) => toast(err.message));
  }
  if (event.target.id === 'search') {
    state.query = event.target.value;
    if (route().name === 'library') $('view').innerHTML = viewLibrary();
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { closeModal(); closeDrawer(); }
});

$('mini-open').addEventListener('click', openPlayer);
$('mini-play').addEventListener('click', togglePlay);
$('mini-next').addEventListener('click', nextChapter);
$('mini').addEventListener('click', (e) => {
  if (e.target.closest('button')) return;
  openPlayer();
});
$('login-form').addEventListener('submit', doLogin);
$('conn-toggle').addEventListener('click', () => {
  const box = $('conn-box');
  box.classList.toggle('show');
  if (box.classList.contains('show') && !box.dataset.filled) {
    box.dataset.filled = '1';
    $('conn-current').textContent = window.location.origin + APP_BASE;
    api('/api/network').then((net) => {
      const lines = [];
      if (state.publicUrl) lines.push({ label: '外网', url: state.publicUrl });
      if (net.public) lines.push({ label: '外网', url: net.public });
      (net.urls || []).forEach((u) => lines.push({ label: u.family === 'ipv6' ? 'IPv6' : 'IPv4', url: u.url }));
      lines.push({ label: '当前', url: window.location.origin + APP_BASE });
      $('conn-lines').innerHTML = lines.map((l) =>
        '<div><b>' + esc(l.label) + '</b> ' + esc(l.url) + '</div>').join('');
    }).catch(() => { $('conn-lines').textContent = '无法获取本机地址'; });
  }
});
$('theme-btn').addEventListener('click', () => applyTheme(state.theme === 'dark' ? 'light' : 'dark'));
$('logout-btn').addEventListener('click', logout);
$('btn-rescan').addEventListener('click', rescan);
$('btn-connect').addEventListener('click', openConnect);
$('mini-cover').addEventListener('error', () => { $('mini-cover').style.visibility = 'hidden'; });

window.addEventListener('hashchange', render);

/* ------------------------------------------------------------------ 启动 */

function startApp() {
  paintUser();
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  $('boot').classList.add('hidden');
  loadLibrary().then(() => {
    render();
    if (state.scanning) startScanWatch();
    api('/api/health').then((d) => {
      const el = $('about-version');
      if (el) el.textContent = 'v' + (d.version || '—');
    }).catch(() => {});
  }).catch((err) => {
    const timedOut = err && (err.name === 'AbortError' ||
      /aborted|abort|超时/i.test(String(err.message || '')));
    toast(timedOut ? '书库加载超时，继续查询扫描状态…' : '书库加载失败：' + err.message, 4000);
    /* 不要停在转圈：继续用 /api/scan/status 轮询，扫描结束/可访问后再拉书库 */
    startScanWatch();
  });
  loadPlaylists();
  loadSettings().catch(() => {});
  loadBgm().then(() => bgmApply(false)).catch(() => {});
}

function initAuth() {
  applyTheme(state.theme);
  paintUser();
  if (!state.token) {
    showLogin('');
    return;
  }
  api('/api/auth/me').then((data) => {
    if (!data.user) throw new Error('未登录');
    state.user = data.user;
    startApp();
  }).catch(() => {
    state.token = '';
    localStorage.removeItem('sy_token');
    showLogin('请先登录');
  });
}

initAuth();
