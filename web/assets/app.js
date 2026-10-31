// WE Weekends settings page. No dependencies, no inline scripts, no HTML
// strings: every text, including anything that came from the server, is set
// with textContent, so nothing can be injected into the page.
import { LANGS, T } from './i18n.js';
import { API } from './config.js';

const TOKEN_KEY = 'wew.token';
const LOGIN_KEY = 'wew.login';
const LANG_KEY = 'wew.lang';
const BOT = 'WEWeekendsBot';

const store = {
  get(area, key) { try { return window[area].getItem(key); } catch { return null; } },
  set(area, key, value) { try { window[area].setItem(key, value); } catch { /* private mode */ } },
  del(area, key) { try { window[area].removeItem(key); } catch { /* private mode */ } },
};

let lang = pickLang();
let geo = null;
let me = null;
let draft = null;
let saved = '';
const root = document.getElementById('app');

function pickLang() {
  const fromUrl = new URLSearchParams(location.search).get('lang');
  if (LANGS.includes(fromUrl)) { store.set('localStorage', LANG_KEY, fromUrl); return fromUrl; }
  const stored = store.get('localStorage', LANG_KEY);
  if (LANGS.includes(stored)) return stored;
  const nav = (navigator.language || '').toLowerCase();
  if (/^(ru|uk|be|kk)/.test(nav)) return 'ru';
  if (/^(sk|cs)/.test(nav)) return 'sk';
  return 'en';
}

const t = (key) => T[lang][key] ?? T.en[key] ?? key;
const fmt = (key, values) => t(key).replace(/\{(\w+)\}/g, (_, k) => String(values[k] ?? ''));

// DOM helper: h('div', {class: 'x', onclick: fn}, child, 'text')
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k in el && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const regionNames = () => { try { return new Intl.DisplayNames([lang], { type: 'region' }); } catch { return null; } };
function countryName(code) {
  const names = regionNames();
  if (code === 'XK') return { ru: 'Косово', sk: 'Kosovo', en: 'Kosovo' }[lang];
  return (names && names.of(code)) || code;
}

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  const token = store.get('localStorage', TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(API + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store',
    });
  } catch {
    return { ok: false, status: 0, data: { error: 'network' } };
  }
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && token) { store.del('localStorage', TOKEN_KEY); }
  return { ok: res.ok, status: res.status, data };
}

function errorText(result) {
  const code = result.data && result.data.error;
  if (result.status === 0) return t('err_network');
  if (result.status === 429) return t('err_rate');
  if (code === 'invalid') return t('err_invalid');
  if (code === 'expired') return t('err_expired');
  if (code === 'pending') return t('err_pending');
  return t('err_generic');
}

// ---------- page frame ----------

function header() {
  const langs = h('nav', { class: 'langs', 'aria-label': 'Language' },
    h('ul', {}, LANGS.map((code) => h('li', {}, h('a', {
      href: `?lang=${code}`, 'aria-current': code === lang ? 'page' : null, lang: code,
      onclick: (e) => { e.preventDefault(); setLang(code); },
    }, code.toUpperCase())))));
  const right = [langs];
  if (me) right.unshift(h('button', { class: 'btn small', type: 'button', onclick: logout }, t('logout')));
  return h('header', { class: 'top' }, h('div', { class: 'wrap' },
    h('a', { class: 'brand', href: './' }, h('img', { src: 'assets/icon.svg', alt: '', width: 32, height: 32 }), 'WE Weekends'),
    h('div', { class: 'top-right' }, right)));
}

function footer() {
  return h('footer', {}, h('div', { class: 'wrap' },
    h('p', { class: 'disclaimer-short' }, t('disclaimer')),
    h('p', { class: 'foot-links' },
      h('a', { href: `privacy.html?lang=${lang}` }, t('privacy')), ' · ',
      h('a', { href: 'https://github.com/JaffarSk24/WE-Weekends', rel: 'noopener' }, t('source')), ' · ',
      h('a', { href: `https://whiteeagles.sk/${lang}/apps/`, rel: 'noopener' }, t('apps'))),
    h('p', { class: 'copy' }, '© 2026 White Eagles & Co. s. r. o.')));
}

function setLang(code) {
  lang = code;
  store.set('localStorage', LANG_KEY, code);
  const url = new URL(location.href);
  url.searchParams.set('lang', code);
  history.replaceState(null, '', url);
  if (document.body.dataset.page === 'privacy') { renderPrivacy(); return; }
  if (draft) markDirty();
  render();
}

function render() {
  document.documentElement.lang = lang;
  document.title = t('page_title');
  const main = me ? settingsView() : landingView();
  root.replaceChildren(header(), h('main', { id: 'main' }, main), footer());
}

// ---------- landing and login ----------

function landingView() {
  const features = [1, 2, 3, 4].map((n) => h('div', { class: 'card feature' }, h('h3', {}, t(`f${n}_t`)), h('p', {}, t(`f${n}_d`))));
  return h('div', { class: 'wrap' },
    h('section', { class: 'hero' },
      h('p', { class: 'eyebrow' }, t('tagline')),
      h('h1', {}, t('hero_title')),
      h('p', { class: 'lead' }, t('hero_text')),
      loginBox()),
    h('section', { class: 'grid4' }, features),
    h('section', { class: 'card how' }, h('h2', {}, t('how_t')),
      h('ol', {}, h('li', {}, t('how1')), h('li', {}, t('how2')), h('li', {}, t('how3')))));
}

function pendingLogin() {
  try {
    const raw = store.get('sessionStorage', LOGIN_KEY);
    if (!raw) return null;
    const login = JSON.parse(raw);
    if (!login || typeof login.nonce !== 'string' || typeof login.secret !== 'string' || login.until < Date.now()) return null;
    return login;
  } catch { return null; }
}

// The Telegram app is opened from this very tab (tg:// link), so no empty
// browser tab is left behind and the code field stays in front of the user.
const appLink = (nonce) => `tg://resolve?domain=${BOT}&start=l_${nonce}`;
const webLink = (nonce) => `https://t.me/${BOT}?start=l_${nonce}`;

function loginBox() {
  const box = h('div', { class: 'login' });
  const login = pendingLogin();
  if (!login) {
    const msg = h('p', { class: 'error', role: 'alert' });
    const button = h('button', { class: 'btn primary big', type: 'button' }, t('login_btn'));
    button.addEventListener('click', async () => {
      button.disabled = true;
      const res = await api('POST', '/v1/auth/start');
      button.disabled = false;
      if (!res.ok || !/^[A-Za-z0-9_-]{32}$/.test(res.data.nonce || '')) { msg.textContent = errorText(res); return; }
      store.set('sessionStorage', LOGIN_KEY, JSON.stringify({ nonce: res.data.nonce, secret: res.data.secret, until: Date.now() + res.data.expires_in * 1000 }));
      render();
      location.href = appLink(res.data.nonce);
    });
    box.append(button, msg);
    return box;
  }
  const input = h('input', { id: 'code', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: 6, pattern: '[0-9]{6}', required: true, placeholder: '000000' });
  const msg = h('p', { class: 'error', role: 'alert' });
  const submit = h('button', { class: 'btn primary', type: 'submit' }, t('login_verify'));
  const form = h('form', { class: 'code-form' },
    h('label', { for: 'code' }, t('login_code')), h('div', { class: 'row' }, input, submit));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = input.value.replace(/\D/g, '');
    if (code.length !== 6) { msg.textContent = t('err_invalid'); return; }
    submit.disabled = true;
    const res = await api('POST', '/v1/auth/verify', { nonce: login.nonce, secret: login.secret, code });
    submit.disabled = false;
    if (!res.ok || typeof res.data.token !== 'string') {
      msg.textContent = errorText(res);
      if (res.data.error === 'expired') { store.del('sessionStorage', LOGIN_KEY); setTimeout(render, 1500); }
      return;
    }
    store.del('sessionStorage', LOGIN_KEY);
    store.set('localStorage', TOKEN_KEY, res.data.token);
    await loadMe();
  });
  input.addEventListener('input', () => { if (input.value.replace(/\D/g, '').length === 6) form.requestSubmit(); });
  box.classList.add('card', 'login-steps');
  box.append(
    h('ol', { class: 'steps' },
      h('li', {}, h('p', {}, t('login_step1')),
        h('a', { class: 'btn', href: appLink(login.nonce) }, t('login_open')),
        h('p', { class: 'hint' }, t('login_no_app'), ' ', h('a', { href: webLink(login.nonce), target: '_blank', rel: 'noopener' }, t('login_web')))),
      h('li', {}, h('p', {}, t('login_step2')), form, msg)),
    h('button', { class: 'linkish', type: 'button', onclick: () => { store.del('sessionStorage', LOGIN_KEY); render(); } }, t('cancel')));
  setTimeout(() => input.focus(), 0);
  return box;
}

async function loadMe() {
  if (!store.get('localStorage', TOKEN_KEY)) { me = null; render(); return; }
  const res = await api('GET', '/v1/me');
  if (!res.ok) { me = null; render(); return; }
  me = res.data;
  draft = structuredClone(me.settings);
  saved = JSON.stringify(draft);
  window.scrollTo(0, 0);
  if (LANGS.includes(me.lang) && !new URLSearchParams(location.search).get('lang') && !store.get('localStorage', LANG_KEY)) lang = me.lang;
  render();
}

async function logout() {
  await api('POST', '/v1/logout');
  store.del('localStorage', TOKEN_KEY);
  me = null; draft = null;
  render();
}

// ---------- form controls ----------

let uid = 0;
const nextId = () => `f${++uid}`;

function field(label, control, hint) {
  const id = control.id || (control.id = nextId());
  return h('div', { class: 'field' }, h('label', { for: id }, label), control, hint ? h('p', { class: 'hint' }, hint) : null);
}

function group(label, control, hint) {
  return h('fieldset', { class: 'field' }, h('legend', {}, label), control, hint ? h('p', { class: 'hint' }, hint) : null);
}

function numberInput(obj, key, min, max, step = 1, suffix = '') {
  const input = h('input', {
    type: 'number', min, max, step, value: obj[key], inputmode: step < 1 ? 'decimal' : 'numeric',
    oninput: (e) => {
      const v = Number(e.target.value);
      if (Number.isFinite(v) && v >= min && v <= max) { obj[key] = step < 1 ? Math.round(v * 10) / 10 : Math.round(v); markDirty(); }
    },
  });
  return suffix ? h('span', { class: 'with-suffix' }, input, h('span', { class: 'suffix' }, suffix)) : input;
}

// − 2 + : for small counts, easier than typing on a phone
function stepper(obj, key, min, max, label) {
  const out = h('output', { 'aria-live': 'polite' }, String(obj[key]));
  const set = (v) => { obj[key] = Math.min(max, Math.max(min, v)); out.textContent = String(obj[key]); markDirty(); };
  return h('div', { class: 'stepper', role: 'group', 'aria-label': label },
    h('button', { type: 'button', 'aria-label': '−', onclick: () => set(obj[key] - 1) }, '−'), out,
    h('button', { type: 'button', 'aria-label': '+', onclick: () => set(obj[key] + 1) }, '+'));
}

function select(obj, key, options, onchange) {
  return h('select', {
    onchange: (e) => { const raw = e.target.value; obj[key] = raw === '' ? null : (typeof options[0][0] === 'number' ? Number(raw) : raw); markDirty(); if (onchange) onchange(); },
  }, options.map(([value, label]) => h('option', { value: value ?? '', selected: obj[key] === value }, label)));
}

function toggle(obj, key, label, onchange) {
  const id = nextId();
  return h('label', { class: 'switch', for: id },
    h('input', { type: 'checkbox', role: 'switch', id, checked: Boolean(obj[key]), onchange: (e) => { obj[key] = e.target.checked; markDirty(); if (onchange) onchange(); } }),
    h('span', { class: 'track', 'aria-hidden': 'true' }),
    h('span', { class: 'switch-label' }, label));
}

function chips(list, options, { min = 0, max = Infinity } = {}) {
  return h('div', { class: 'chips' }, options.map(([value, label]) => {
    const btn = h('button', { type: 'button', class: 'chip', 'aria-pressed': String(list.includes(value)) }, label);
    btn.addEventListener('click', () => {
      const i = list.indexOf(value);
      if (i >= 0 && list.length > min) list.splice(i, 1);
      else if (i < 0 && list.length < max) list.push(value);
      btn.setAttribute('aria-pressed', String(list.includes(value)));
      markDirty();
    });
    return btn;
  }));
}

function segmented(obj, key, options, onchange) {
  const wrap = h('div', { class: 'segmented', role: 'radiogroup' });
  const paint = () => wrap.querySelectorAll('button').forEach((b, i) => b.setAttribute('aria-checked', String(obj[key] === options[i][0])));
  options.forEach(([value, label]) => wrap.append(h('button', {
    type: 'button', role: 'radio', onclick: () => { obj[key] = value; paint(); markDirty(); if (onchange) onchange(); },
  }, label)));
  paint();
  return wrap;
}

function slider(obj, key, label) {
  const out = h('output', {}, String(obj[key]));
  const id = nextId();
  return h('div', { class: 'slider' },
    h('label', { for: id }, label),
    h('input', { id, type: 'range', min: 0, max: 10, step: 1, value: obj[key], oninput: (e) => { obj[key] = Number(e.target.value); out.textContent = e.target.value; draft.rating.preset = 'custom'; markDirty(); paintPresets(); } }),
    out);
}

// Country search: every country is known by its name in Russian, Slovak and
// English and by its code; only a country picked from the suggestions can be
// added, so a typo never silently turns into an empty search.
const fold = (s) => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/ё/g, 'е').trim();
let countryIndex = null;
function countries() {
  if (countryIndex && countryIndex.lang === lang) return countryIndex.list;
  const names = Object.fromEntries(['ru', 'sk', 'en'].map((l) => {
    try { return [l, new Intl.DisplayNames([l], { type: 'region' })]; } catch { return [l, null]; }
  }));
  const list = geo.countries.map((code) => {
    const all = ['ru', 'sk', 'en'].map((l) => (code === 'XK' ? { ru: 'Косово', sk: 'Kosovo', en: 'Kosovo' }[l] : names[l] && names[l].of(code)) || code);
    return { code, name: countryName(code), keys: [...new Set([...all.map(fold), code.toLowerCase()])] };
  }).sort((a, b) => a.name.localeCompare(b.name, lang));
  countryIndex = { lang, list };
  return list;
}

function suggest(query, exclude, source = countries) {
  const q = fold(query);
  if (!q) return [];
  const starts = [], inside = [];
  for (const c of source()) {
    if (exclude.includes(c.code)) continue;
    if (c.keys.some((k) => k.startsWith(q) || k.split(/[\s-]+/).some((w) => w.startsWith(q)))) starts.push(c);
    else if (q.length >= 3 && c.keys.some((k) => k.includes(q))) inside.push(c);
  }
  return [...starts, ...inside].slice(0, 8);
}

function combobox({ placeholder, exclude, onPick, source = countries }) {
  const listId = nextId();
  const input = h('input', { type: 'text', role: 'combobox', 'aria-expanded': 'false', 'aria-controls': listId, 'aria-autocomplete': 'list', autocomplete: 'off', placeholder });
  const list = h('ul', { id: listId, role: 'listbox', class: 'options', hidden: true });
  const msg = h('p', { class: 'error small', role: 'alert' });
  let items = [], active = -1;
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
  const pick = (c) => { onPick(c.code); input.value = ''; msg.textContent = ''; close(); input.focus(); };
  const paint = () => {
    list.replaceChildren(...items.map((c, i) => {
      const li = h('li', { role: 'option', id: `${listId}-${i}`, 'aria-selected': String(i === active) }, c.name, h('span', { class: 'code' }, c.badge || c.code));
      li.addEventListener('mousedown', (e) => { e.preventDefault(); pick(c); });
      return li;
    }));
    list.hidden = items.length === 0;
    input.setAttribute('aria-expanded', String(!list.hidden));
    if (active >= 0) input.setAttribute('aria-activedescendant', `${listId}-${active}`); else input.removeAttribute('aria-activedescendant');
  };
  input.addEventListener('input', () => {
    items = suggest(input.value, exclude(), source);
    active = items.length ? 0 : -1;
    msg.textContent = input.value.trim() && !items.length ? t(source === countries ? 'no_country' : 'no_place') : '';
    paint();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && items.length) { e.preventDefault(); active = (active + 1) % items.length; paint(); }
    else if (e.key === 'ArrowUp' && items.length) { e.preventDefault(); active = (active - 1 + items.length) % items.length; paint(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (active >= 0) pick(items[active]); else if (input.value.trim()) msg.textContent = t(source === countries ? 'pick_country' : 'pick_place'); }
    else if (e.key === 'Escape') close();
  });
  input.addEventListener('blur', () => { setTimeout(close, 100); if (input.value.trim()) msg.textContent = t(source === countries ? 'pick_country' : 'pick_place'); });
  input.addEventListener('focus', () => { if (input.value.trim()) { items = suggest(input.value, exclude(), source); paint(); } });
  return h('div', { class: 'combo' }, input, list, msg);
}

function countryPicker(list, max) {
  const tags = h('div', { class: 'tags' });
  const paint = () => tags.replaceChildren(...list.map((code) => h('span', { class: 'tag' }, countryName(code),
    h('button', { type: 'button', 'aria-label': `${t('remove')} ${countryName(code)}`, onclick: () => { list.splice(list.indexOf(code), 1); paint(); markDirty(); } }, '×'))));
  paint();
  return h('div', { class: 'picker' },
    combobox({ placeholder: t('add_country'), exclude: () => list, onPick: (code) => { if (list.length < max) { list.push(code); paint(); markDirty(); } } }),
    tags);
}

// Cities and resorts a user never wants: picked from the known list only.
let placeIndex = null;
function places() {
  if (placeIndex && placeIndex.lang === lang) return placeIndex.list;
  const list = Object.entries(geo.places || {}).map(([id, p]) => ({
    code: id, badge: p.country, name: `${p[lang] || p.en}, ${countryName(p.country)}`,
    keys: [...new Set([p.en, p.ru, p.sk, ...p.match.map((m) => m.replace(/_/g, ' '))].map(fold))],
  })).sort((a, b) => a.name.localeCompare(b.name, lang));
  placeIndex = { lang, list };
  return list;
}

function placePicker(list, max) {
  const label = (id) => { const p = (geo.places || {})[id]; return p ? (p[lang] || p.en) : id; };
  const tags = h('div', { class: 'tags' });
  const paint = () => tags.replaceChildren(...list.map((id) => h('span', { class: 'tag' }, label(id),
    h('button', { type: 'button', 'aria-label': `${t('remove')} ${label(id)}`, onclick: () => { list.splice(list.indexOf(id), 1); paint(); markDirty(); } }, '×'))));
  paint();
  return h('div', { class: 'picker' },
    combobox({ placeholder: t('add_place'), exclude: () => list, source: places, onPick: (id) => { if (list.length < max) { list.push(id); paint(); markDirty(); } } }),
    tags);
}

// One country or none, for citizenship and residence.
function countrySingle(obj, key, allowed, emptyLabel) {
  const box = h('div', { class: 'single' });
  const paint = () => {
    if (obj[key]) {
      box.replaceChildren(h('span', { class: 'tag big' }, countryName(obj[key]),
        h('button', { type: 'button', 'aria-label': t('remove'), onclick: () => { obj[key] = null; paint(); markDirty(); } }, '×')));
    } else {
      box.replaceChildren(combobox({
        placeholder: emptyLabel,
        exclude: () => (allowed ? geo.countries.filter((c) => !allowed.includes(c)) : []),
        onPick: (code) => { obj[key] = code; paint(); markDirty(); },
      }));
    }
  };
  paint();
  return box;
}

function rangeList(list, max, withScope) {
  const box = h('div', { class: 'ranges' });
  const today = new Date().toISOString().slice(0, 10);
  const paint = () => {
    box.replaceChildren(...list.map((item, i) => h('div', { class: 'range' },
      h('label', {}, t('from'), h('input', { type: 'date', value: item.start, min: today, onchange: (e) => { item.start = e.target.value; if (item.end < item.start) { item.end = item.start; paint(); } markDirty(); } })),
      h('label', {}, t('to'), h('input', { type: 'date', value: item.end, min: item.start, onchange: (e) => { item.end = e.target.value; markDirty(); } })),
      withScope ? segmented(item, 'scope', [['all', t('scope_all')], ['family', t('scope_family')]]) : null,
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': t('remove'), onclick: () => { list.splice(i, 1); paint(); markDirty(); } }, '×'))),
    list.length < max ? h('button', { type: 'button', class: 'btn small', onclick: () => { list.push(withScope ? { start: today, end: today, scope: 'all' } : { start: today, end: today }); paint(); markDirty(); } }, `+ ${t('add_range')}`) : '');
  };
  paint();
  return box;
}

function panel(title, intro, ...body) {
  return h('div', { class: 'panel' }, h('h2', {}, title), intro ? h('p', { class: 'intro' }, intro) : null, body);
}

// ---------- settings ----------

const TABS = ['from', 'where', 'budget', 'calendar', 'feeds', 'score', 'digest'];
let tab = TABS.includes(store.get('sessionStorage', 'wew.tab')) ? store.get('sessionStorage', 'wew.tab') : 'from';
let paintPresets = () => {};

function settingsView() {
  const s = draft;
  const status = [];
  if (me.status === 'waitlist') status.push(h('p', { class: 'banner warn' }, fmt('st_waitlist', { n: me.waitlist_position })));
  if (me.paused) status.push(h('p', { class: 'banner' }, t('st_paused')));
  if (!me.configured) status.push(h('p', { class: 'banner' }, t('st_new')));

  // 1. From where and who
  const from = panel(t('tab_from'), t('from_intro'),
    group(t('origins'), chips(s.profile.origins, Object.entries(geo.airports).map(([code, city]) => [code, `${city} ${code}`]), { min: 1, max: 6 }), t('origins_hint')),
    h('div', { class: 'two' },
      group(t('citizenship'), countrySingle(s.profile, 'citizenship', null, t('add_country'))),
      group(t('residence'), countrySingle(s.profile, 'eu_residence', geo.eu_residence, t('residence_none')))),
    h('p', { class: 'hint' }, t('residence_hint')));

  // 2. Where
  const prio = (key) => h('div', { class: 'prio-row' }, h('span', { class: 'prio-label' }, t(key)), segmented(s.priorities, key, t('prio').map((label, i) => [i, label])));
  const where = panel(t('tab_where'), t('where_intro'),
    group(t('priorities'), h('div', { class: 'prio' }, prio('sea'), prio('mountains'), prio('sights'))),
    group(t('groups'), chips(s.geography.groups, Object.keys(geo.groups).map((g) => [g, t(`g_${g}`)]), { max: 20 })),
    h('div', { class: 'two' },
      group(t('include'), countryPicker(s.geography.include, 250)),
      group(t('exclude'), countryPicker(s.geography.exclude, 250))),
    group(t('exclude_places'), placePicker(s.geography.exclude_places, 50), t('exclude_places_hint')),
    group(t('s_trip'), h('div', { class: 'inline' },
      h('span', {}, t('nights_min')), stepper(s.trip, 'nights_min', 1, 7, t('nights_min')),
      h('span', {}, t('nights_max')), stepper(s.trip, 'nights_max', 1, 14, t('nights_max')))),
    group(t('horizon'), segmented(s.trip, 'horizon_days', [[30, '30'], [60, '60'], [90, '90'], [120, '120'], [180, '180']].map(([v, l]) => [v, fmt('days_n', { n: l })]))));

  // 3. Budget and stay
  const fam = s.budgets.family;
  const familyBox = h('div', { class: 'sub', hidden: !fam.enabled });
  const kids = h('div', { class: 'tags' });
  const paintKids = () => kids.replaceChildren(
    ...fam.children.map((age, i) => h('span', { class: 'tag' },
      h('input', { type: 'number', min: 0, max: 17, value: age, 'aria-label': t('child_age'), class: 'age', oninput: (e) => { const v = Number(e.target.value); if (v >= 0 && v <= 17) { fam.children[i] = Math.round(v); markDirty(); } } }),
      h('span', { class: 'muted' }, t('years')),
      h('button', { type: 'button', 'aria-label': t('remove'), onclick: () => { fam.children.splice(i, 1); paintKids(); markDirty(); } }, '×'))),
    fam.children.length < 6 ? h('button', { type: 'button', class: 'btn small', onclick: () => { fam.children.push(8); paintKids(); markDirty(); } }, `+ ${t('add_child')}`) : '');
  paintKids();
  familyBox.append(
    h('div', { class: 'two' }, group(t('adults'), stepper(fam, 'adults', 1, 4, t('adults'))), field(t('family_budget'), numberInput(fam, 'budget_eur', 50, 30000, 1, '€'))),
    group(t('children'), kids));
  const budget = panel(t('tab_budget'), t('budget_hint'),
    h('div', { class: 'two' }, field(t('solo_budget'), numberInput(s.budgets.solo, 'budget_eur', 30, 10000, 1, '€'))),
    toggle(fam, 'enabled', t('family_enable'), () => { familyBox.hidden = !fam.enabled; }),
    familyBox,
    toggle(s.budgets, 'include_transfer', t('include_transfer')),
    h('h3', {}, t('s_stay')),
    h('div', { class: 'two' },
      group(t('min_rating'), segmented(s.stay, 'min_rating', [[7, '7+'], [7.5, '7.5+'], [8, '8+'], [8.5, '8.5+'], [9, '9+']])),
      group(t('min_stars'), segmented(s.stay, 'min_stars', [[0, t('stars_any')], [3, '3+'], [4, '4+'], [5, '5']]))),
    h('div', { class: 'two' },
      field(t('min_reviews'), numberInput(s.stay, 'min_reviews', 0, 5000)),
      group(t('breakfast'), segmented(s.stay, 'breakfast', [['any', t('bf_any')], ['prefer', t('bf_prefer')], ['require', t('bf_require')]]))));

  // 4. Calendar
  const school = s.calendar.school;
  const schoolRegion = h('div', { hidden: school.mode !== 'region' }, segmented(school, 'region', geo.school_regions.SK.map((r) => [r, t(`r_${r.replace('-', '_')}`)])));
  const schoolManual = h('div', { hidden: school.mode !== 'manual' }, rangeList(school.manual, 12, false));
  const calendar = panel(t('tab_calendar'), t('calendar_intro'),
    group(t('holiday_country'), segmented(s.calendar, 'holiday_country', geo.holiday_countries.map((c) => [c, countryName(c)]))),
    group(t('workdays'), chips(s.calendar.workdays, t('days').map((d, i) => [i + 1, d]), { max: 7 })),
    group(t('max_lost'), segmented(s.calendar, 'max_lost_workdays', [[0, t('lost_0')], [1, t('lost_1')], [2, t('lost_2')]])),
    group(t('school'), h('div', {},
      segmented(school, 'mode', [['none', t('school_none')], ['region', t('school_region')], ['manual', t('school_manual')]], () => {
        if (school.mode === 'region' && !school.region) { school.region = 'SK-BL'; schoolRegion.replaceChildren(segmented(school, 'region', geo.school_regions.SK.map((r) => [r, t(`r_${r.replace('-', '_')}`)]))); }
        schoolRegion.hidden = school.mode !== 'region';
        schoolManual.hidden = school.mode !== 'manual';
      }), schoolRegion, schoolManual)),
    group(t('busy'), rangeList(s.calendar.busy, 30, true), t('busy_hint')));

  // 5. Feeds: the digests a user picks, as cards
  const freqOptions = Object.entries(t('freq'));
  const feed = (key, icon, extra) => {
    const b = s.blocks[key];
    const body = h('div', { class: 'feed-body', hidden: !b.enabled }, group(t('how_often'), segmented(b, 'freq', freqOptions)), extra || null);
    const card = h('article', { class: `feed${b.enabled ? ' on' : ''}` });
    const sw = toggle(b, 'enabled', t('enabled'), () => { body.hidden = !b.enabled; card.classList.toggle('on', b.enabled); });
    card.append(h('div', { class: 'feed-head' },
      h('span', { class: 'feed-icon', 'aria-hidden': 'true' }, icon),
      h('div', { class: 'feed-title' }, h('h3', {}, t(`b_${key}`)), h('p', {}, t(`b_${key}_d`))),
      sw), body);
    return card;
  };
  const vac = s.blocks.vacation;
  const hotels = h('div', { class: 'ranges' });
  const paintHotels = () => {
    const input = h('input', { type: 'url', placeholder: 'https://www.booking.com/hotel/…', 'aria-label': t('hotel_url') });
    const msg = h('p', { class: 'error small', role: 'alert' });
    const add = () => {
      const url = normalizeBooking(input.value);
      if (!url) { msg.textContent = t('hotel_bad'); return; }
      if (!vac.hotels.includes(url) && vac.hotels.length < 10) { vac.hotels.push(url); markDirty(); }
      paintHotels();
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
    hotels.replaceChildren(
      ...vac.hotels.map((url, i) => h('div', { class: 'hotel' },
        h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, hotelLabel(url)),
        h('button', { type: 'button', class: 'icon-btn', 'aria-label': t('remove'), onclick: () => { vac.hotels.splice(i, 1); paintHotels(); markDirty(); } }, '×'))),
      vac.hotels.length < 10 ? h('div', { class: 'row' }, input, h('button', { type: 'button', class: 'btn small', onclick: add }, t('add'))) : '', msg);
  };
  paintHotels();
  const sug = vac.suggest;
  const sugBox = h('div', { class: 'sub', hidden: !sug.enabled },
    h('div', { class: 'two' },
      field(t('suggest_max'), numberInput(sug, 'max_hotel_eur', 100, 50000, 1, '€')),
      group(t('min_stars'), segmented(sug, 'min_stars', [[0, t('stars_any')], [3, '3+'], [4, '4+'], [5, '5']]))),
    group(t('min_rating'), segmented(sug, 'min_rating', [[7.5, '7.5+'], [8, '8+'], [8.5, '8.5+'], [9, '9+']])),
    toggle(sug, 'all_inclusive', t('all_inclusive')), toggle(sug, 'aquapark', t('aquapark')));
  const feeds = panel(t('tab_feeds'), t('feeds_intro'),
    h('div', { class: 'feeds' },
      feed('weekends', '🌊'),
      feed('diamonds', '💎', field(t('max_price'), numberInput(s.blocks.diamonds, 'max_price_eur', 10, 300, 1, '€'))),
      feed('wow', '🔥', field(t('max_ppn'), numberInput(s.blocks.wow, 'max_per_person_night', 10, 500, 1, '€'))),
      feed('vacation', '🌴', h('div', {},
        group(t('party'), segmented(vac, 'party', [['solo', t('solo')], ['family', t('family')]])),
        group(t('dates'), rangeList(vac.date_options, 4, false)),
        group(t('vac_countries'), countryPicker(vac.countries, 30)),
        group(t('hotels'), hotels),
        toggle(sug, 'enabled', t('suggest'), () => { sugBox.hidden = !sug.enabled; }), sugBox,
        h('p', { class: 'hint' }, t('hotel_price_note'))))));

  // 6. Scoring: presets as cards, sliders folded away
  const KEYS = ['price', 'times', 'workdays', 'weather', 'stay', 'discount'];
  const PRESETS = { cheap: [10, 5, 6, 3, 3, 6], comfort: [5, 8, 7, 6, 8, 4], kids: [6, 7, 5, 7, 9, 5] };
  const weightsBox = h('div', {});
  const paintWeights = () => weightsBox.replaceChildren(...KEYS.map((k) => slider(s.rating.weights, k, t(`w_${k}`))));
  const presetCards = h('div', { class: 'presets', role: 'radiogroup' });
  paintPresets = () => presetCards.querySelectorAll('button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.preset === s.rating.preset)));
  for (const p of ['cheap', 'comfort', 'kids', 'custom']) {
    presetCards.append(h('button', {
      type: 'button', role: 'radio', class: 'preset', 'data-preset': p,
      onclick: () => {
        s.rating.preset = p;
        if (PRESETS[p]) KEYS.forEach((k, i) => { s.rating.weights[k] = PRESETS[p][i]; });
        paintWeights(); paintPresets(); markDirty();
        if (p === 'custom') fine.open = true;
      },
    }, h('strong', {}, t(`preset_${p}`)), h('span', {}, t(`preset_${p}_d`))));
  }
  paintWeights(); paintPresets();
  const fine = h('details', { class: 'fine' }, h('summary', {}, t('fine_tune')), weightsBox);
  const score = panel(t('tab_score'), t('rating_hint'), presetCards, fine);

  // 7. Messages and account
  const pausedBox = { paused: me.paused };
  const digest = panel(t('tab_digest'), t('digest_hint'),
    h('div', { class: 'two' },
      field(t('hour'), select(s.digest, 'hour', Array.from({ length: 18 }, (_, i) => [i + 5, `${String(i + 5).padStart(2, '0')}:00`]))),
      group(t('weekly_day'), segmented(s.digest, 'weekly_day', t('days').map((d, i) => [i + 1, d])))),
    h('h3', {}, t('s_account')),
    toggle(pausedBox, 'paused', t('pause'), async () => {
      const res = await api('POST', '/v1/pause', { paused: pausedBox.paused });
      if (res.ok) me.paused = pausedBox.paused;
    }),
    h('div', { class: 'buttons left' },
      h('button', { type: 'button', class: 'btn small', onclick: async () => { await api('POST', '/v1/logout-all'); store.del('localStorage', TOKEN_KEY); me = null; render(); } }, t('logout_all')),
      h('button', { type: 'button', class: 'btn small danger', onclick: async () => {
        if (!window.confirm(t('delete_confirm'))) return;
        const res = await api('DELETE', '/v1/me', { confirm: 'DELETE' });
        if (res.ok) { store.del('localStorage', TOKEN_KEY); me = null; render(); }
      } }, t('delete'))));

  const panels = { from, where, budget, calendar, feeds, score, digest };
  const nav = h('nav', { class: 'tabs', 'aria-label': t('settings_nav') });
  const content = h('div', { class: 'tab-content' });
  const stepLine = h('p', { class: 'step-line' });
  const prev = h('button', { type: 'button', class: 'btn' }, `← ${t('back')}`);
  const next = h('button', { type: 'button', class: 'btn primary' });
  const show = (name, scroll) => {
    tab = name;
    store.set('sessionStorage', 'wew.tab', name);
    const i = TABS.indexOf(name);
    nav.querySelectorAll('button').forEach((b) => b.setAttribute('aria-current', b.dataset.tab === name ? 'step' : 'false'));
    for (const [k, p] of Object.entries(panels)) p.hidden = k !== name;
    stepLine.textContent = fmt('step_of', { n: i + 1, m: TABS.length });
    prev.hidden = i === 0;
    next.textContent = i === TABS.length - 1 ? (me.configured ? t('save') : t('finish')) : `${t('next')}: ${t(`tab_${TABS[i + 1]}`)} →`;
    const active = nav.querySelector(`[data-tab="${name}"]`);
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest', inline: 'center' });
    if (scroll) window.scrollTo({ top: 0 });
  };
  TABS.forEach((name, i) => nav.append(h('button', { type: 'button', 'data-tab': name, onclick: () => show(name, true) },
    h('span', { class: 'num' }, String(i + 1)), t(`tab_${name}`))));
  prev.addEventListener('click', () => show(TABS[Math.max(0, TABS.indexOf(tab) - 1)], true));
  next.addEventListener('click', () => {
    const i = TABS.indexOf(tab);
    if (i === TABS.length - 1) save(); else show(TABS[i + 1], true);
  });
  content.append(...Object.values(panels), h('div', { class: 'pager' }, prev, stepLine, next));
  const view = h('div', { class: 'wrap settings' }, status, h('div', { class: 'layout' }, nav, content), saveBar());
  show(tab, false);
  return view;
}

function normalizeBooking(value) {
  try {
    const url = new URL(value.trim());
    if (url.protocol !== 'https:' || !['www.booking.com', 'booking.com'].includes(url.hostname)) return null;
    const path = url.pathname.toLowerCase();
    if (!/^\/hotel\/[a-z]{2}\/[a-z0-9-]{1,120}(\.[a-z]{2}(-[a-z]{2})?)?\.html$/.test(path)) return null;
    return `https://www.booking.com${path.replace(/\.[a-z]{2}(-[a-z]{2})?\.html$/, '.html')}`;
  } catch { return null; }
}

function hotelLabel(url) {
  const slug = url.split('/').pop().replace('.html', '');
  return slug.replace(/-/g, ' ');
}

// ---------- saving ----------

let bar = null;
function saveBar() {
  const status = h('span', { class: 'status', role: 'status' });
  const button = h('button', { class: 'btn primary', type: 'button', onclick: save }, t('save'));
  bar = { el: h('div', { class: 'savebar' }, h('div', { class: 'wrap narrow' }, status, button)), status, button };
  paintBar();
  return bar.el;
}

function isDirty() { return draft && JSON.stringify(draft) !== saved; }
function markDirty() { paintBar(); }
function paintBar(text) {
  if (!bar) return;
  const dirty = isDirty() || !me.configured || me.lang !== lang;
  bar.el.classList.toggle('dirty', dirty);
  bar.status.textContent = text || (dirty ? t('unsaved') : t('saved'));
}

async function save() {
  bar.button.disabled = true;
  paintBar(t('saving'));
  const res = await api('PUT', '/v1/settings', { version: me.version, lang, settings: draft });
  bar.button.disabled = false;
  if (res.ok) {
    me.version = res.data.version;
    me.configured = true;
    me.lang = lang;
    saved = JSON.stringify(draft);
    paintBar();
    return;
  }
  if (res.status === 409) { window.alert(t('err_conflict')); await loadMe(); return; }
  if (res.status === 422 && Array.isArray(res.data.fields)) { paintBar(fmt('err_fields', { f: res.data.fields.slice(0, 5).join(', ') })); return; }
  if (res.status === 401) { me = null; render(); return; }
  paintBar(errorText(res));
}

window.addEventListener('beforeunload', (e) => { if (isDirty()) { e.preventDefault(); } });

// ---------- start ----------

async function start() {
  try {
    const res = await fetch('assets/geo.json', { cache: 'force-cache' });
    geo = await res.json();
  } catch {
    root.textContent = t('err_network');
    return;
  }
  if (document.body.dataset.page === 'privacy') { renderPrivacy(); return; }
  await telegramSignIn();
  render();
  await loadMe();
}

// Opened from the bot's menu inside Telegram: the page gets init data signed
// with the bot token in the URL fragment, and the server turns it into a
// session, so no code is needed. No Telegram script is loaded for this.
async function telegramSignIn() {
  const data = new URLSearchParams(location.hash.slice(1)).get('tgWebAppData');
  if (!data) return;
  history.replaceState(null, '', location.pathname + location.search);
  telegramEvent('web_app_ready');
  telegramEvent('web_app_expand');
  const res = await api('POST', '/v1/auth/telegram', { init_data: data });
  if (res.ok && typeof res.data.token === 'string') store.set('localStorage', TOKEN_KEY, res.data.token);
}

function telegramEvent(name) {
  try {
    if (window.TelegramWebviewProxy) window.TelegramWebviewProxy.postEvent(name, '');
    else if (window.parent !== window) window.parent.postMessage(JSON.stringify({ eventType: name, eventData: '' }), 'https://web.telegram.org');
  } catch { /* not inside Telegram */ }
}

function renderPrivacy() {
  document.documentElement.lang = lang;
  document.title = `${t('privacy_title')} · WE Weekends`;
  root.replaceChildren(header(), h('main', { id: 'main' }, h('article', { class: 'wrap narrow doc' },
    h('p', {}, h('a', { href: `./?lang=${lang}` }, `← ${t('back')}`)),
    h('h1', {}, t('privacy_title')),
    t('privacy_body').map((p) => h('p', {}, p)),
    h('h2', {}, t('disclaimer_t')), h('p', {}, t('disclaimer')))), footer());
}

start();
