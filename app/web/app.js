'use strict';

// Любая необработанная ошибка скрипта — на экран, крупно. Без этого скрипт
// падает молча, и страница выглядит как «вечная загрузка»: пустые списки
// и никакого объяснения. Именно так это и выглядело у пользователя.
window.addEventListener('error', (ev) => {
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999;' +
    'background:#5a1f1a;color:#ffd9d2;padding:10px 16px;font:13px/1.4 monospace;' +
    'white-space:pre-wrap;border-bottom:2px solid #f0563a';
  box.textContent = 'Ошибка скрипта: ' + (ev.message || ev.error) +
    (ev.lineno ? '  (строка ' + ev.lineno + ')' : '') +
    '\nОбновите страницу с очисткой кэша: Ctrl+F5. Если не помогло — пришлите этот текст.';
  document.body.appendChild(box);
});

// ---------------------------------------------------------------- состояние
// Герои, которых пользователь не играет: их не предлагаем никогда.
// Список живёт в localStorage; по умолчанию — Wraith King, по просьбе владельца.
const NEVER_DEFAULT = [42];
const NEVER_KEY = 'draft-helper.never';

function loadNever() {
  try {
    const raw = localStorage.getItem(NEVER_KEY);
    if (raw === null) return NEVER_DEFAULT.slice();
    const list = JSON.parse(raw);
    return Array.isArray(list) ? list.map(Number).filter(Number.isFinite) : [];
  } catch (e) {
    return NEVER_DEFAULT.slice();
  }
}

function saveNever() {
  try { localStorage.setItem(NEVER_KEY, JSON.stringify(state.draft.never)); }
  catch (e) { /* приватный режим — просто не сохранится */ }
}

const state = {
  heroes: [],
  byId: new Map(),
  brackets: [],
  stratz: null,   // статус снимка STRATZ: доступен ли, дата, свои ранги
  roles: [],
  draft: { enemy: [], ally: [], banned: [], never: loadNever() },
  mode: 'enemy',
  search: '',
};

// Роли приходят от OpenDota на английском, а интерфейс русский.
// Значение в списке остаётся английским: по нему фильтрует сервер.
const ROLE_LABELS = {
  Carry: 'Кэрри',
  Support: 'Поддержка',
  Nuker: 'Нюкер',
  Disabler: 'Контроль',
  Durable: 'Живучесть',
  Escape: 'Уход',
  Pusher: 'Пушер',
  Initiator: 'Инициатор',
};
const roleLabel = (r) => ROLE_LABELS[r] || r;

// Если элемента нет — это рассинхрон страницы и скрипта (обычно старый
// index.html из кэша браузера). Говорим об этом прямо, а не падаем на null.
const $ = (sel) => {
  const node = document.querySelector(sel);
  if (!node) {
    throw new Error('на странице нет элемента ' + sel +
      ' — страница и скрипт разных версий, обновите с очисткой кэша (Ctrl+F5)');
  }
  return node;
};
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

// Таймаут обязателен: без него неотвечающий запрос оставляет интерфейс
// пустым навсегда, и непонятно, грузится он или сломался.
const API_TIMEOUT_MS = 120000;

async function api(path, opts) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), API_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(path, Object.assign({ signal: ctrl.signal }, opts || {}));
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') {
      throw new Error(
        `сервер не ответил за ${API_TIMEOUT_MS / 1000} с (${path}). ` +
        'Обычно это значит, что нет доступа к api.opendota.com — ' +
        'проверьте интернет, VPN или брандмауэр.');
    }
    throw new Error(`не удалось связаться с сервером (${path}): ${e.message}`);
  }
  clearTimeout(timer);
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error(`сервер вернул не JSON (HTTP ${res.status})`);
  }
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function showError(container, err) {
  container.innerHTML = '';
  container.appendChild(el('div', 'error', 'Ошибка: ' + err.message));
}

const sign = (v) => (v > 0 ? '+' : '') + v.toFixed(2);
// «—» для отсутствующего значения; ноль — настоящее значение, его не трогаем
const orDash = (v) => (v === null || v === undefined ? '—' : v);
const cls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'dim');

// ---------------------------------------------------------------- вкладки
// Только настоящие вкладки — с data-view. Класс .tab используется и для
// оформления обычных кнопок; ловить их клики здесь нельзя.
document.querySelectorAll('.tabs .tab[data-view]').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tabs .tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
    tab.classList.add('active');
    $('#view-' + tab.dataset.view).classList.add('active');
    if (tab.dataset.view === 'meta') loadMeta();
    if (tab.dataset.view === 'tournaments') loadTournaments();
    if (tab.dataset.view === 'wards' && !ward.data) loadWards(false);
    if (tab.dataset.view === 'pro') loadProList();
  });
});

// ---------------------------------------------------------------- загрузка справочника
async function boot() {
  // Показываем, что идёт загрузка: первый запуск тянет справочник героев
  // из OpenDota, и без подсказки пустой интерфейс выглядит как поломка.
  $('#hero-grid').innerHTML =
    '<div class="loading">Загружаю справочник героев из OpenDota…</div>';
  $('#src-label').textContent = 'загрузка…';
  try {
    const data = await api('/api/heroes');
    state.heroes = data.heroes;
    state.byId = new Map(data.heroes.map((h) => [h.id, h]));
    state.brackets = data.brackets;
    state.stratz = data.stratz || null;
    $('#src-label').textContent = (data.snapshot
      ? 'источник: ' + data.source + ' (снимок)'
      : 'источник: ' + data.source) + ' · v' + (data.version || '?');
    if (data.snapshot_note) {
      const note = el('div', 'dim', data.snapshot_note);
      note.style.marginBottom = '8px';
      $('#rec-out').parentNode.insertBefore(note, $('#rec-out'));
    }

    const roles = new Set();
    data.heroes.forEach((h) => h.roles.forEach((r) => roles.add(r)));
    state.roles = [...roles].sort();

    fillMatchupSources();
    fillBrackets();
    fillSelect($('#meta-bracket'), state.brackets.map((b) => [b.key, b.label]));
    const posOptions = [['', 'Любая']]
      .concat((data.positions || []).map((p) => [p.key, p.label]));
    state.positions = Object.fromEntries((data.positions || []).map((p) => [p.key, p.label]));
    // «Авто» - только в подборе: свободные позиции считаются по союзникам,
    // которых видно на экране или отмечено рукой (игра свою роль не сообщает)
    fillSelect($('#position'), [['auto', 'Авто — по союзникам на экране'], ['', 'Любая']]
      .concat((data.positions || []).map((p) => [p.key, p.label])));
    // «Авто» по умолчанию: без союзников оно равно «Любая», а с ними
    // сужает список само - пользователю не нужно вспоминать про этот пункт
    $('#position').value = 'auto';
    fillSelect($('#meta-position'), posOptions);
    fillSelect($('#tour-position'), posOptions);

    renderHeroGrid();
    renderSlots();
    fillMyHeroSelect();
    fillWardHeroSelect();
    visionBoot();
    const savedAccount = loadAccount();
    if (savedAccount) {
      $('#account-input').value = savedAccount;
      applyAccount(savedAccount, 'сохранён');
    }
    pollGsi();
    me.gsiTimer = setInterval(pollGsi, 3000);
  } catch (e) {
    bootFailed(e);
  }
}

function bootFailed(err) {
  $('#src-label').textContent = 'источник недоступен';
  const grid = $('#hero-grid');
  grid.innerHTML = '';
  grid.style.display = 'block';
  grid.appendChild(el('div', 'error', 'Не удалось загрузить героев. ' + err.message));

  const retry = el('button', 'tab', 'Попробовать снова');
  retry.style.marginTop = '10px';
  retry.addEventListener('click', () => {
    grid.style.display = '';
    boot();
  });
  grid.appendChild(retry);

  const hint = el('div', 'dim');
  hint.style.marginTop = '10px';
  hint.textContent = 'Приложению нужен доступ к api.opendota.com. ' +
    'Если он закрыт, подбор работать не будет: все данные берутся оттуда. ' +
    'Подробности ошибки видны в окне, из которого запущен сервер.';
  grid.appendChild(hint);

  $('#vision-status').textContent =
    'Недоступно, пока не загрузится справочник героев.';
}

function fillSelect(sel, pairs) {
  sel.innerHTML = '';
  pairs.forEach(([value, label]) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    sel.appendChild(o);
  });
}

// Источник матчапов. STRATZ появляется в списке, только если его снимок
// есть на диске; выбор запоминается, потому что STRATZ, когда он есть,
// почти всегда лучше OpenDota - выборка на порядок больше.
const MATCHUP_SOURCE_KEY = 'matchup_source';

function fillMatchupSources() {
  const sel = $('#matchup-source');
  const pairs = [['opendota', 'OpenDota']];
  const stz = state.stratz;
  if (stz && stz.available) {
    pairs.push(['stratz', 'STRATZ' + (stz.date ? ' (' + stz.date + ')' : '')]);
  }
  fillSelect(sel, pairs);
  let saved = null;
  try { saved = localStorage.getItem(MATCHUP_SOURCE_KEY); } catch (e) { /* приватный режим */ }
  const want = saved || (stz && stz.available ? 'stratz' : 'opendota');
  sel.value = pairs.some(([v]) => v === want) ? want : 'opendota';
}

// Ранги у источников разные: у OpenDota - по одному, у STRATZ - парами.
// Список подменяется при смене источника; ключ ранга сохраняется, если
// он есть и там, и там (это только «all»).
function fillBrackets() {
  const sel = $('#bracket');
  const keep = sel.value;
  const useStratz = $('#matchup-source').value === 'stratz' && state.stratz;
  const list = useStratz ? state.stratz.brackets : state.brackets;
  fillSelect(sel, list.map((b) => [b.key, b.label]));
  if (list.some((b) => b.key === keep)) sel.value = keep;
}

$('#matchup-source').addEventListener('change', () => {
  try { localStorage.setItem(MATCHUP_SOURCE_KEY, $('#matchup-source').value); }
  catch (e) { /* приватный режим */ }
  fillBrackets();
});

// ---------------------------------------------------------------- драфт
$('#mode').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  state.mode = btn.dataset.mode;
  document.querySelectorAll('#mode button').forEach((b) => b.classList.remove('active'));
  btn.classList.add('active');
});

$('#hero-search').addEventListener('input', (e) => {
  state.search = e.target.value.trim().toLowerCase();
  renderHeroGrid();
});

// Занятые в текущем драфте. Список «не предлагать» сюда не входит:
// врага Wraith King нужно засчитать во враги, даже если сами мы его не играем.
function usedIds() {
  const d = state.draft;
  return new Set([...d.enemy, ...d.ally, ...d.banned]);
}

function renderHeroGrid() {
  const grid = $('#hero-grid');
  const used = usedIds();
  grid.innerHTML = '';
  state.heroes
    .filter((h) => !state.search || (h.name || '').toLowerCase().includes(state.search))
    .forEach((h) => {
      const card = el('div', 'hero' + (used.has(h.id) ? ' used' : ''));
      const img = el('img');
      img.src = h.img;
      img.alt = h.name;
      img.loading = 'lazy';
      card.appendChild(img);
      card.appendChild(el('div', 'n', h.name));
      card.title = h.name + ' — ' + h.roles.join(', ');
      card.addEventListener('click', () => addHero(h.id));
      grid.appendChild(card);
    });
}

function addHero(id) {
  const list = state.draft[state.mode];
  const caps = { enemy: 5, ally: 5, banned: 14, never: 127 };
  if (list.length >= caps[state.mode]) return;
  // в драфте герой может быть только в одном списке; «не предлагать» —
  // отдельный список, туда можно добавить кого угодно, лишь бы не дважды
  const taken = state.mode === 'never' ? new Set(list) : usedIds();
  if (taken.has(id)) return;
  list.push(id);
  if (state.mode === 'never') saveNever();
  renderSlots();
  renderHeroGrid();
  refreshRecommendations();
}

function removeHero(kind, id) {
  state.draft[kind] = state.draft[kind].filter((x) => x !== id);
  if (kind === 'never') saveNever();
  renderSlots();
  renderHeroGrid();
  refreshRecommendations();
}

function renderSlots() {
  [['enemy', '#slots-enemy'], ['ally', '#slots-ally'],
   ['banned', '#slots-banned'], ['never', '#slots-never']]
    .forEach(([kind, sel]) => {
      const box = $(sel);
      box.innerHTML = '';
      if (!state.draft[kind].length) {
        box.appendChild(el('div', 'empty-hint', 'пусто'));
        return;
      }
      state.draft[kind].forEach((id) => {
        const h = state.byId.get(id);
        const chip = el('div', 'chip');
        const img = el('img');
        img.src = h.img;
        img.alt = h.name;
        chip.appendChild(img);
        chip.appendChild(el('span', '', h.name));
        chip.appendChild(el('span', 'x', '✕'));
        chip.title = 'Убрать';
        chip.addEventListener('click', () => removeHero(kind, id));
        box.appendChild(chip);
      });
    });
}

// #matchup-source стоит после своего обработчика выше: сначала подменяется
// список рангов, потом пересчёт читает уже новый ранг
['#matchup-source', '#bracket', '#position', '#limit', '#tour-weight', '#tour-period'].forEach((sel) =>
  $(sel).addEventListener('change', refreshRecommendations));

let recToken = 0;
async function refreshRecommendations() {
  // сборка зависит от врагов: поменялся драфт - пересчитать и её
  const enemyKey = state.draft.enemy.slice().sort().join(',');
  if (me.heroId && enemyKey !== me.lastEnemyKey) {
    me.lastEnemyKey = enemyKey;
    loadBuild();
  }
  const out = $('#rec-out');
  if (!state.draft.enemy.length) {
    out.innerHTML = '<div class="empty-hint">Выберите хотя бы одного героя противника.</div>';
    return;
  }
  const token = ++recToken;
  out.innerHTML = '<div class="loading">Считаю…</div>';
  try {
    const data = await api('/api/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enemy: state.draft.enemy,
        ally: state.draft.ally,
        // «не предлагать» для сервера — те же баны: их просто нет в выдаче
        banned: state.draft.banned.concat(state.draft.never),
        matchup_source: $('#matchup-source').value,
        bracket: $('#bracket').value,
        position: $('#position').value,
        limit: Number($('#limit').value),
        tour_weight: Number($('#tour-weight').value),
        tour_months: Number($('#tour-period').value),
        account: account.id,
        personal_weight: Number($('#personal-weight').value),
      }),
    });
    if (token !== recToken) return;
    renderRecommendations(out, data);
    // сервер отдал подбор сразу, а турнирную или личную часть считает
    // в фоне - дозапросить, пока не досчитает (но не бесконечно)
    if (data.pending && pendingRetries < 8) {
      pendingRetries += 1;
      setTimeout(() => { if (token === recToken) refreshRecommendations(); }, 4000);
    } else if (!data.pending) {
      pendingRetries = 0;
    }
  } catch (e) {
    if (token === recToken) showError(out, e);
  }
}
let pendingRetries = 0;

function renderRecommendations(out, data) {
  const rows = data.rows;
  out.innerHTML = '';
  if (data.note) out.appendChild(el('div', 'error', 'Внимание: ' + data.note));
  // позиция определена автоматически: сказать, какая и почему, и дать
  // поправить рукой - это вывод, а не знание
  if (data.position_auto) {
    const pa = data.position_auto;
    const line = el('div', pa.positions ? 'note' : 'dim');
    line.style.marginBottom = '6px';
    const label = (p) => (state.positions || {})[String(p)] || String(p);
    if (pa.positions && pa.positions.length === 1) {
      line.textContent = `${pa.screen ? 'Позиция с экрана' : 'Позиция по союзникам'}: ` +
        `${label(pa.positions[0])} — ${pa.reason}. Не так? Выберите позицию в списке.`;
    } else if (pa.positions) {
      line.textContent = `Свободные позиции: ${pa.positions.map(label).join(', ')} — ${pa.reason}. ` +
        'Показаны герои всех свободных; уточните позицию в списке.';
    } else {
      line.textContent = 'Союзники ещё не взяты — показаны все позиции. ' +
        'Как только появятся союзники на экране, позиции сузятся.';
    }
    out.appendChild(line);
  }
  // «считается в фоне» - не ошибка, показываем приглушённо
  const noteCls = data.pending ? 'dim' : 'error';
  if (data.tour_note) out.appendChild(el('div', noteCls, data.tour_note));
  if (data.personal_note) out.appendChild(el('div', noteCls, data.personal_note));
  if (!rows.length) {
    out.appendChild(el('div', 'empty-hint', 'Ничего не подошло под фильтры.'));
    return;
  }
  const withTour = data.tour_weight > 0;
  const withMe = data.personal_weight > 0;
  // синергия есть только у STRATZ и только когда выбраны союзники
  const withSyn = rows.some((r) => r.synergy_pp !== null && r.synergy_pp !== undefined);
  const max = Math.max(...rows.map((r) => Math.abs(r.score_pp)), 1);
  const table = el('table');
  table.innerHTML = `<thead><tr>
      <th>#</th><th>Герой</th><th class="num">Балл</th>
      <th class="num">Матчапы</th>
      ${withSyn ? '<th class="num">Синергия</th>' : ''}
      <th class="num">База</th>
      ${withTour ? '<th class="num">Турниры</th>' : ''}
      ${withMe ? '<th class="num">Мои</th><th class="num">Мои игры</th>' : ''}
      <th class="num">Винрейт</th><th class="num">Выборка</th>
      <th>Против кого</th></tr></thead>`;
  const tb = el('tbody');

  rows.forEach((r, i) => {
    const tr = el('tr');

    tr.appendChild(el('td', 'dim', String(i + 1)));

    const tdHero = el('td');
    const cell = el('div', 'hero-cell');
    const img = el('img');
    img.src = 'https://cdn.cloudflare.steamstatic.com' + (r.img || '');
    img.alt = r.name;
    img.loading = 'lazy';
    cell.appendChild(img);
    const nameBox = el('div');
    nameBox.appendChild(el('div', '', r.name));
    nameBox.appendChild(el('div', 'dim', r.roles.slice(0, 3).join(', ')));
    cell.appendChild(nameBox);
    tdHero.appendChild(cell);
    tr.appendChild(tdHero);

    const tdScore = el('td', 'num');
    tdScore.appendChild(el('div', cls(r.score_pp), sign(r.score_pp)));
    const bar = el('div', 'bar');
    bar.style.width = Math.round((Math.abs(r.score_pp) / max) * 60) + 'px';
    bar.style.marginLeft = 'auto';
    bar.style.opacity = r.score_pp >= 0 ? '1' : '.4';
    tdScore.appendChild(bar);
    tr.appendChild(tdScore);

    tr.appendChild(el('td', 'num ' + cls(r.matchup_pp), sign(r.matchup_pp)));
    if (withSyn) {
      const sp = r.synergy_pp || 0;
      const td = el('td', 'num ' + cls(sp), sign(sp));
      td.title = (r.per_ally || []).map((pa) => {
        const ally = state.byId.get(pa.ally_id);
        return (ally ? ally.name : pa.ally_id) + ' ' + sign(pa.syn_pp) + ' (' + pa.games + ' игр)';
      }).join('\n');
      tr.appendChild(td);
    }
    tr.appendChild(el('td', 'num ' + cls(r.base_pp), sign(r.base_pp)));
    if (withTour) {
      const tp = r.tour_pp || 0;
      tr.appendChild(el('td', 'num ' + cls(tp), sign(tp)));
    }
    if (withMe) {
      const pp = r.personal_pp || 0;
      tr.appendChild(el('td', 'num ' + cls(pp), sign(pp)));
      const mine = r.my_games
        ? `${r.my_games} · ${r.my_winrate}%` : '—';
      tr.appendChild(el('td', 'num dim', mine));
    }
    tr.appendChild(el('td', 'num', r.base_winrate === null ? '—' : r.base_winrate.toFixed(1) + '%'));
    tr.appendChild(el('td', 'num dim', String(r.games_total)));

    const tdBreak = el('td');
    const bd = el('div', 'breakdown');
    r.per_enemy.forEach((pe) => {
      const enemy = state.byId.get(pe.enemy_id);
      const s = el('span');
      s.textContent = (enemy ? enemy.name : pe.enemy_id) + ' ';
      const v = el('b', cls(pe.adv_pp), sign(pe.adv_pp));
      s.appendChild(v);
      s.title = `${pe.games} игр в выборке`;
      if (pe.games < 15) s.style.opacity = '.45';
      bd.appendChild(s);
    });
    tdBreak.appendChild(bd);
    tr.appendChild(tdBreak);

    tb.appendChild(tr);
  });
  table.appendChild(tb);
  out.appendChild(table);

  const legend = el('div', 'dim');
  legend.style.marginTop = '10px';
  const viaStratz = data.matchup_source === 'stratz';
  legend.textContent =
    'Балл в процентных пунктах. Матчапы — вклад контрпика, База — сила героя в выбранном ранге' +
    (viaStratz ? ' и на выбранной позиции' : '') + '. ' +
    (withSyn
      ? 'Синергия — насколько герой вместе с вашими союзниками выигрывает чаще, чем они в среднем ' +
        '(наведите на число — вклад каждого союзника). '
      : '') +
    (withTour
      ? `Турниры — востребованность и винрейт у про за выбранный период (${data.tour_matches} матчей), ` +
        `вес ${data.tour_weight}: чем выше, тем сильнее турнирная мета перевешивает остальное. `
      : '') +
    (withMe
      ? 'Мои — ваш личный винрейт на герое относительно общего и опыт на нём: ' +
        'герой, которого вы не брали, получает минус, основной — плюс. '
      : '') +
    'Полупрозрачные пары — менее 15 игр в выборке. ' +
    'Сглаживание K = ' + orDash(data.k_shrink) +
    (withSyn ? ', для синергии K = ' + orDash(data.k_synergy) : '') + '.' +
    (viaStratz
      ? ' Матчапы, синергия и позиции — по снимку STRATZ' +
        (state.stratz && state.stratz.weeks && state.stratz.weeks.length
          ? ' за недели с ' + state.stratz.weeks[state.stratz.weeks.length - 1] : '') +
        ' · Powered by STRATZ.'
      : '');
  out.appendChild(legend);
}

// ---------------------------------------------------------------- мой герой и сборка
const me = { heroId: null, gsiTimer: null, gsiHero: null, gsiTeam: null, buildToken: 0 };

function fillMyHeroSelect() {
  const sel = $('#my-hero');
  const keep = sel.value;
  sel.innerHTML = '<option value="">— не выбран —</option>';
  state.heroes.forEach((h) => {
    const o = document.createElement('option');
    o.value = h.id;
    o.textContent = h.name;
    sel.appendChild(o);
  });
  if (keep) sel.value = keep;
}

$('#my-hero').addEventListener('change', () => {
  setMyHero(Number($('#my-hero').value) || null, 'вручную');
});
$('#build-tier').addEventListener('change', () => loadBuild());

function setMyHero(id, how) {
  if (me.heroId === id) return;
  me.heroId = id;
  me.lastEnemyKey = state.draft.enemy.slice().sort().join(',');
  $('#my-hero').value = id ? String(id) : '';
  // свой герой - это и союзник в драфте
  if (id && !usedIds().has(id) && state.draft.ally.length < 5) {
    state.draft.ally.push(id);
    renderSlots();
    renderHeroGrid();
    refreshRecommendations();
  }
  loadBuild(how);
}

async function loadBuild(how, quiet) {
  const out = $('#build-out');
  if (!me.heroId) {
    out.className = 'empty-hint';
    out.textContent = 'Выберите своего героя — покажу стартовый закуп и порядок сборки из турнирных матчей.';
    return;
  }
  const hero = state.byId.get(me.heroId);
  const enemies = state.draft.enemy.filter((id) => id !== me.heroId);
  // quiet - дозапрос, пока сервер досчитывает: показ не сбрасывать
  if (!quiet) {
    out.className = 'loading';
    out.textContent = `Собираю сборку ${hero ? hero.name : ''} по турнирным матчам` +
      (enemies.length ? ' и отдельно против этого драфта' : '') + '…';
  }
  const token = ++me.buildToken;
  try {
    const q = new URLSearchParams({
      hero: me.heroId,
      months: Math.min(Number($('#tour-period').value), 3),
      enemy: enemies.join(','),
      tier: $('#build-tier').value,
    });
    const b = await api('/api/build?' + q);
    if (token !== me.buildToken) return;
    out.className = '';
    renderBuild(out, b, hero, how);
    loadSkills(out, token);
    // сервер отдал снимок или прежнюю копию, а живую сборку считает в фоне -
    // дозапросить, пока не досчитает (но не бесконечно: на битом канале
    // сервер сам скажет, что OpenDota не отвечает, и pending снимется)
    if (b.pending) pollLater(() => token === me.buildToken && loadBuild(how, true), 'build');
    else pollDone('build');
  } catch (e) { if (token === me.buildToken) { out.className = ''; showError(out, e); } }
}

// повторные запросы, пока сервер досчитывает в фоне: 5 с, потом реже,
// всего не дольше ~3 минут на ключ
const pollState = {};
function pollLater(fn, key) {
  const st = pollState[key] || (pollState[key] = { n: 0 });
  if (st.n >= 20) return;
  st.n += 1;
  clearTimeout(st.timer);
  st.timer = setTimeout(fn, st.n < 6 ? 5000 : 12000);
}
function pollDone(key) { if (pollState[key]) { pollState[key].n = 0; clearTimeout(pollState[key].timer); } }

// прокачка и таланты грузятся отдельно: это ещё один запрос к базе,
// и сборка не должна его ждать
async function loadSkills(out, token, again) {
  // again - блок от прошлого показа: при дозапросе не плодить новые
  const box = again || el('div');
  if (!again) {
    box.style.marginTop = '10px';
    box.appendChild(el('div', 'loading', 'Считаю прокачку и таланты…'));
    out.appendChild(box);
  }
  try {
    const q = new URLSearchParams({
      hero: me.heroId,
      months: Math.min(Number($('#tour-period').value), 3),
      tier: $('#build-tier').value,
    });
    const s = await api('/api/skills?' + q);
    if (token !== me.buildToken) return;
    box.innerHTML = '';
    renderSkills(box, s);
    if (s.pending) pollLater(() => token === me.buildToken && loadSkills(out, token, box), 'skills');
    else pollDone('skills');
  } catch (e) {
    if (token === me.buildToken) { box.innerHTML = ''; showError(box, e); }
  }
}

// раздел «Способности» как во внутриигровом гайде: порядок прокачки
// иконками по уровням и таланты слева-справа от уровня, выбранный подсвечен
function renderSkills(box, s) {
  box.appendChild(el('div', 'section-title', 'Способности'));
  // откуда данные: снимок, считается в фоне, OpenDota не отвечает
  if (s.note) box.appendChild(el('div', s.pending || !/не отвечает/.test(s.note) ? 'dim' : 'error', s.note));
  if (!s.games) {
    if (!s.pending) box.appendChild(el('div', 'dim', 'Данных по прокачке за период нет.'));
    return;
  }
  const sub = el('div', 'skills-sub');
  sub.appendChild(el('span', '', 'Порядок способностей'));
  sub.appendChild(el('span', 'dim', ` — по ${s.games} про-играм, доля игр с этой способностью на уровне`));
  box.appendChild(sub);

  const row = el('div', 'skill-row');
  s.order.forEach((a) => {
    const cell = el('div', 'skill');
    cell.appendChild(el('div', 'skill-lvl', String(a.level)));
    if (a.img) {
      const img = el('img'); img.src = a.img; img.alt = a.dname; img.title = a.dname;
      cell.appendChild(img);
    } else {
      cell.appendChild(el('div', 'skill-noimg', a.dname));
    }
    cell.appendChild(el('div', 'skill-share' + (a.share < 60 ? ' dim' : ''), a.share + '%'));
    row.appendChild(cell);
  });
  box.appendChild(row);

  if (!s.talents.length) return;
  const sub2 = el('div', 'skills-sub');
  sub2.appendChild(el('span', '', 'Таланты'));
  sub2.appendChild(el('span', 'dim', ' — доля выбора среди игр, дошедших до уровня, и винрейт этих игр'));
  box.appendChild(sub2);

  const grid = el('div', 'talents');
  [25, 20, 15, 10].forEach((tier) => {
    const pair = s.talents.filter((x) => x.tier === tier);
    if (!pair.length) return;
    // в игре левый талант - второй в списке, правый - первый
    const left = pair[1] || pair[0];
    const right = pair[0];
    const line = el('div', 'talent-row');
    const cellFor = (t) => {
      const cell = el('div', 'talent' + (t.picked && t.share >= 50 ? ' talent-top' : ''));
      cell.appendChild(el('div', 'talent-name',
        t.dname + (t.approx ? ' (число см. в игре)' : '')));
      if (t.picked) {
        const st = el('div', 'talent-stat');
        st.appendChild(el('span', '', `${t.share}% выбор`));
        st.appendChild(el('span', 'dim', ' · '));
        st.appendChild(el('span', 'pos', `${t.winrate}% побед`));
        cell.appendChild(st);
      } else {
        cell.appendChild(el('div', 'talent-stat dim', 'Не выбирался в этой выборке'));
      }
      return cell;
    };
    line.appendChild(cellFor(left));
    const tierCell = el('div', 'talent-tier');
    tierCell.appendChild(el('div', '', String(tier)));
    // сколько игр дошло до уровня: 100% выбора на 8 играх - не то же, что на 120
    tierCell.appendChild(el('div', 'talent-tier-n', `${right.tier_games} игр`));
    line.appendChild(tierCell);
    line.appendChild(cellFor(right));
    grid.appendChild(line);
  });
  box.appendChild(grid);
}

function renderBuild(out, b, hero, how) {
  out.innerHTML = '';
  const head = el('div');
  head.style.marginBottom = '8px';
  const title = el('span', '', (hero ? hero.name : '') + ' ');
  title.style.fontWeight = '600';
  head.appendChild(title);
  const tierName = { top: 'Premium + Professional', premium: 'Premium', all: 'все турниры' }[b.tier] || b.tier;
  head.appendChild(el('span', 'dim',
    b.games
      ? `— ${b.games} турнирных игр (${tierName}) за ${b.months} мес, винрейт ${b.winrate}%` +
        (how ? ` · герой определён: ${how}` : '')
      : (b.pending ? '— считаю по базе турнирных матчей…'
        : `— в турнирах (${tierName}) за этот период герой не встречался`)));
  out.appendChild(head);
  if (b.note) {
    const n = el('div', b.pending || !/не отвечает/.test(b.note) ? 'dim' : 'error', b.note);
    n.style.marginBottom = '8px';
    out.appendChild(n);
  }
  if (!b.games) return;

  // против конкретного драфта: сколько игр и по чему считается показ
  if (b.vs) {
    const names = b.vs.enemy_ids.map((id) => (state.byId.get(id) || {}).name || id);
    const vsLine = el('div', b.vs.used ? 'note' : 'dim');
    vsLine.style.marginBottom = '8px';
    if (b.vs.games) {
      vsLine.textContent = `Против этого драфта (${names.join(', ')}): ${b.vs.games} про-игр ` +
        `с хотя бы одним из них, винрейт ${b.vs.winrate}%. ` +
        (b.vs.used
          ? 'Сборка ниже — именно по этим играм.'
          : `Мало для отдельной сборки (нужно ${15}) — ниже общая, а сдвиги показаны как подсказка.`);
    } else {
      vsLine.textContent = `Против этого драфта (${names.join(', ')}) про-игр за период нет — ниже общая сборка.`;
    }
    out.appendChild(vsLine);
  }

  // четыре ряда одного размера: подпись слева, иконки с минутой на бейдже,
  // под каждой доля игр и винрейт с этим предметом
  const block = el('div', 'items');
  const line = (title, list, badge) => {
    if (!list.length) return;
    const row = el('div', 'items-row build-line');
    row.appendChild(el('span', 'dim items-title', title));
    list.forEach((it) => row.appendChild(buildItem(it, badge(it))));
    block.appendChild(row);
  };
  const minuteBadge = (it) => it.minute_f === null || it.minute_f === undefined
    ? '' : (it.minute_f < 10 ? it.minute_f.toFixed(1) : Math.round(it.minute_f)) + '′';
  line('старт', b.start, (it) => it.count > 1 ? `×${it.count}` : '');
  line('сборка', b.order, minuteBadge);
  line('ситуативно', b.situational, minuteBadge);
  line('итог', b.final, () => '');
  out.appendChild(block);

  // сдвиги: что против этого драфта берут иначе, чем обычно
  if (b.vs && b.vs.shifts && b.vs.shifts.length) {
    const sh = el('div');
    sh.style.marginTop = '8px';
    sh.appendChild(el('div', 'dim', 'Что против этого драфта берут иначе, чем обычно:'));
    const table = el('table');
    table.innerHTML = '<thead><tr><th>Предмет</th><th class="num">Обычно</th>' +
      '<th class="num">Против них</th><th class="num">Сдвиг</th></tr></thead>';
    const tb = el('tbody');
    b.vs.shifts.forEach((s) => {
      const tr = el('tr');
      const td = el('td');
      const cell = el('div', 'hero-cell');
      cell.appendChild(itemIcon(s));
      cell.appendChild(el('span', '', s.dname));
      td.appendChild(cell);
      tr.appendChild(td);
      tr.appendChild(el('td', 'num dim',
        `${s.general_share}%` + (s.general_minute !== null ? ` · ${s.general_minute} мин` : '')));
      tr.appendChild(el('td', 'num',
        `${s.share}%` + (s.minute !== null ? ` · ${s.minute} мин` : '')));
      const parts = [];
      if (Math.abs(s.d_share) >= 8) parts.push((s.d_share > 0 ? 'чаще на ' : 'реже на ') + Math.abs(s.d_share) + ' п.п.');
      if (Math.abs(s.d_minute) >= 3) parts.push((s.d_minute < 0 ? 'раньше на ' : 'позже на ') + Math.abs(s.d_minute) + ' мин');
      const good = s.d_share > 0 || s.d_minute < 0;
      tr.appendChild(el('td', 'num ' + (good ? 'pos' : 'neg'), parts.join(', ')));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    sh.appendChild(table);
    out.appendChild(sh);
  }

  const legend = el('div', 'dim');
  legend.style.marginTop = '6px';
  legend.textContent = 'Под иконкой — доля игр с предметом и винрейт этих игр. ' +
    'Старт — куплено до рога хотя бы в 40% игр, ×N — сколько штук. ' +
    'Сборка — собранные предметы из ≥25% игр по медианной минуте первой покупки, части (Ogre Axe, Reaver) не показываются. ' +
    'Ситуативно — дорогие предметы из 10–25% игр. Итог — что чаще всего в инвентаре к концу. ' +
    'Игры против драфта взвешены по числу совпавших врагов: игра против трёх из них весит втрое больше, чем против одного.';
  out.appendChild(legend);
}

// --- мой аккаунт: личная статистика в подборе -----------------------------
const ACCOUNT_KEY = 'draft-helper.account';
const account = { id: null, name: null };

function loadAccount() {
  try { return localStorage.getItem(ACCOUNT_KEY) || ''; } catch (e) { return ''; }
}
function saveAccount(v) {
  try { localStorage.setItem(ACCOUNT_KEY, v || ''); } catch (e) { /* ничего */ }
}

async function applyAccount(text, how) {
  const out = $('#account-out');
  if (!text) {
    account.id = null;
    saveAccount('');
    out.className = 'dim';
    out.textContent = 'Аккаунт не задан — подбор без личной статистики.';
    refreshRecommendations();
    return;
  }
  out.className = 'loading';
  out.textContent = 'Проверяю аккаунт…';
  try {
    const p = await api('/api/player?id=' + encodeURIComponent(text));
    account.id = p.account_id;
    account.name = p.name;
    saveAccount(String(p.account_id));
    out.className = '';
    out.innerHTML = '';
    if (!p.public) {
      out.appendChild(el('div', 'error',
        `Аккаунт ${p.account_id} найден, но игр по нему нет: скорее всего в Steam закрыта ` +
        '«Открытая история матчей» (Steam → профиль → настройки приватности → Dota 2). ' +
        'Включите — и через несколько игр данные появятся.'));
      return;
    }
    const head = el('div');
    head.innerHTML = `<b>${p.name || p.account_id}</b> <span class="dim">· ${p.games} игр, ` +
      `винрейт ${p.winrate}%` + (p.rank_tier ? ` · ранг ${rankName(p.rank_tier)}` : '') +
      (how ? ` · ${how}` : '') + '</span>';
    out.appendChild(head);
    const row = el('div', 'draft-row');
    row.style.marginTop = '6px';
    p.top_heroes.forEach((h) => {
      const chip = el('div', 'chip');
      chip.style.cursor = 'default';
      if (h.img) { const img = el('img'); img.src = h.img; img.alt = h.name; chip.appendChild(img); }
      chip.appendChild(el('span', '', h.name));
      chip.appendChild(el('span', 'dim', `${h.games} · ${h.winrate}%`));
      row.appendChild(chip);
    });
    out.appendChild(row);
    refreshRecommendations();
  } catch (e) {
    out.className = '';
    showError(out, e);
  }
}

function rankName(tier) {
  const names = ['', 'Herald', 'Guardian', 'Crusader', 'Archon', 'Legend', 'Ancient', 'Divine', 'Immortal'];
  const major = Math.floor(tier / 10);
  const star = tier % 10;
  return (names[major] || '?') + (star && major < 8 ? ` ${star}` : '');
}

$('#account-apply').addEventListener('click', () => {
  applyAccount($('#account-input').value.trim(), 'введён вручную');
});
$('#account-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') applyAccount($('#account-input').value.trim(), 'введён вручную');
});
$('#personal-weight').addEventListener('change', refreshRecommendations);

// --- Game State Integration: игра сама сообщает героя и сторону -----------
async function pollGsi() {
  try {
    const s = await api('/api/gsi/state');
    const box = $('#gsi-status');
    if (!s.received) {
      box.textContent = 'не подключена';
      box.className = 'dim';
    } else if (!s.alive) {
      box.textContent = `молчит ${Math.round((Date.now() / 1000 - s.last_at))} с`;
      box.className = 'dim';
    } else {
      const parts = [];
      if (s.hero_localized) parts.push(s.hero_localized);
      if (s.team) parts.push(s.team === 'radiant' ? 'Radiant' : 'Dire');
      if (s.game_state) parts.push(s.game_state.replace('DOTA_GAMERULES_STATE_', '').toLowerCase());
      box.textContent = parts.join(' · ') || 'подключена';
      box.className = 'pos';
    }
    if (s.hero_id && s.hero_id !== me.gsiHero) {
      me.gsiHero = s.hero_id;
      setMyHero(s.hero_id, 'из игры');
    }
    // Steam ID из игры -> аккаунт для личной статистики, если ещё не задан
    if (s.steamid && !account.id && !me.gsiSteam) {
      me.gsiSteam = s.steamid;
      $('#account-input').value = s.steamid;
      applyAccount(s.steamid, 'из игры');
    }
    // сторона из игры: в верхней полоске Radiant слева, Dire справа
    if (s.team && s.team !== me.gsiTeam) {
      me.gsiTeam = s.team;
      const want = s.team === 'radiant' ? 'left' : 'right';
      if ($('#vis-side').value !== want) {
        $('#vis-side').value = want;
        $('#vis-side').dispatchEvent(new Event('change'));
      }
    }
  } catch (e) { /* сервер занят - подождём */ }
}

$('#gsi-check').addEventListener('click', async () => {
  const out = $('#gsi-check-out');
  out.innerHTML = '<div class="loading">Проверяю…</div>';
  try {
    const c = await api('/api/gsi/check');
    out.innerHTML = '';
    const steps = [];
    steps.push([!!c.dota_cfg_dir,
      c.dota_cfg_dir ? `папка Dota найдена: ${c.dota_cfg_dir}`
        : 'папка Dota не найдена в обычных местах Steam — скачайте конфиг и положите вручную']);
    if (c.dota_cfg_dir) {
      steps.push([c.config_present,
        c.config_present ? `конфиг лежит: ${c.config_path}`
          : 'конфига нет — нажмите «Подключить игру»']);
      if (c.config_present) {
        steps.push([c.config_port_ok,
          c.config_port_ok ? 'в конфиге правильный адрес и порт'
            : 'в конфиге другой порт — нажмите «Подключить игру» ещё раз']);
      }
    }
    steps.push([c.received > 0,
      c.received > 0
        ? `игра прислала сообщений: ${c.received}` + (c.alive ? ' (сейчас на связи)' : ' (сейчас молчит)')
        : 'от игры не пришло ни одного сообщения']);
    steps.forEach(([ok, text]) => {
      const row = el('div', ok ? 'pos' : 'neg', (ok ? '✓ ' : '✗ ') + text);
      row.style.fontSize = '12px';
      out.appendChild(row);
    });
    if (c.config_present && !c.received) {
      out.appendChild(el('div', 'error',
        'Конфиг на месте, но игра молчит. Почти наверняка не задан параметр запуска: ' +
        'Steam → Dota 2 → Свойства → Параметры запуска → добавить -gamestateintegration, ' +
        'затем перезапустить Dota. Сообщения идут даже из главного меню — ждать матча не нужно.'));
    }
  } catch (e) { showError(out, e); }
});

$('#gsi-install').addEventListener('click', async () => {
  const box = $('#gsi-status');
  box.textContent = 'ищу папку Dota…';
  try {
    const r = await api('/api/gsi/install', { method: 'POST' });
    box.textContent = r.message + (r.path ? ` (${r.path})` : '');
    box.className = r.path ? 'pos' : 'neg';
  } catch (e) { box.textContent = 'ошибка: ' + e.message; box.className = 'neg'; }
});

// ---------------------------------------------------------------- чтение экрана
const vision = { timer: null, running: false, lastKey: '', lastState: null, lastScans: 0 };

async function visionBoot() {
  const box = $('#vision-status');
  try {
    const st = await api('/api/vision/status');
    if (!st.available) {
      box.innerHTML = '';
      box.appendChild(el('div', 'error', st.hint || 'Чтение экрана недоступно.'));
      return;
    }
    $('#vision-controls').hidden = false;
    fillSelect($('#vis-monitor'), st.monitors.filter((m) => m.index > 0)
      .map((m) => [String(m.index), m.label]));
    if (st.config) {
      $('#vis-monitor').value = String(st.config.monitor);
      $('#vis-interval').value = String(st.config.interval);
    }
    const ic = st.icons;
    const loaded = st.templates_loaded;
    if (ic.ready && loaded !== undefined && loaded < ic.have) {
      // файлы есть, а распознаватель их не прочитал — раньше это было
      // невидимо и выглядело как «не распознаёт»
      box.innerHTML = '';
      box.appendChild(el('div', 'error',
        `Файлов портретов ${ic.have}, но в распознаватель загружено только ${loaded}. ` +
        `Папка: ${st.assets_dir}. Пришлите этот текст.`));
    } else {
      box.textContent = ic.ready
        ? `Готово. Эталонов героев: ${ic.have} из ${ic.total}, загружено ${loaded}.`
        : `Портретов героев: ${ic.have} из ${ic.total}. Они скачаются сами при ` +
          `первом запуске слежения — это займёт около минуты. Можно и заранее, ` +
          `кнопкой «Скачать портреты».`;
    }
  } catch (e) {
    showError(box, e);
  }
}

async function visionConfig() {
  const region = $('#vis-region').value.split(',').map(Number);
  await api('/api/vision/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      monitor: Number($('#vis-monitor').value),
      interval: Number($('#vis-interval').value),
      region,
    }),
  });
}

$('#vis-start').addEventListener('click', async () => {
  try {
    await visionConfig();
    const r = await api('/api/vision/start', { method: 'POST' });
    if (!r.started && r.state && r.state.last_error) throw new Error(r.state.last_error);
    vision.running = true;
    renderVision(r.state);
    if (vision.timer) clearInterval(vision.timer);
    vision.timer = setInterval(pollVision, 2000);
  } catch (e) { showError($('#vision-out'), e); }
});

$('#vis-stop').addEventListener('click', async () => {
  vision.running = false;
  if (vision.timer) { clearInterval(vision.timer); vision.timer = null; }
  try { renderVision((await api('/api/vision/stop', { method: 'POST' })).state); }
  catch (e) { showError($('#vision-out'), e); }
});

$('#vis-scan').addEventListener('click', async () => {
  const out = $('#vision-out');
  const delay = Number($('#vis-delay').value);
  try {
    await visionConfig();
    // обратный отсчёт: чтобы человек успел переключиться в игру,
    // иначе в кадр попадает браузер с этой самой кнопкой
    for (let left = delay; left > 0; left--) {
      out.innerHTML = `<div class="loading">Переключитесь в игру. Снимок через ${left} с…</div>`;
      await new Promise((r) => setTimeout(r, 1000));
    }
    out.innerHTML = '<div class="loading">Ищу героев на экране…</div>';
    renderVision(await api('/api/vision/scan', { method: 'POST' }));
  } catch (e) { showError(out, e); }
});

// Смена стороны — пересобрать драфт из последнего распознанного заново:
// иначе союзники, уже попавшие во враги при неверной стороне, там и останутся.
$('#vis-side').addEventListener('change', () => {
  if (!vision.lastState) return;
  state.draft.enemy = [];
  state.draft.ally = [];
  vision.lastKey = '';
  renderSlots();
  renderHeroGrid();
  renderVision(vision.lastState);
});

// Проверка на файле: отделяет «не захватывает экран» от «не распознаёт».
$('#vis-file').addEventListener('change', async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const out = $('#vision-out');
  out.innerHTML = `<div class="loading">Распознаю ${file.name}…</div>`;
  try {
    const res = await fetch('/api/vision/recognize', {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    renderVision(data);
  } catch (err) { showError(out, err); }
  e.target.value = '';
});

// Самопроверка: распознаватель ищет свои же эталоны на синтетическом кадре.
// Провал здесь — поломка самого распознавания, а не захвата или игры.
$('#vis-selftest').addEventListener('click', async () => {
  const out = $('#vision-out');
  out.innerHTML = '<div class="loading">Самопроверка распознавания…</div>';
  try {
    const r = await api('/api/vision/selftest');
    out.innerHTML = '';
    if (r.ok) {
      out.appendChild(el('div', 'note',
        `Самопроверка пройдена: ${r.found} из ${r.placed} эталонов найдено за ${r.seconds} с. ` +
        'Распознавание исправно — если вживую не находит, дело в захвате экрана: ' +
        'смотрите превью «Что видит приложение».'));
    } else {
      out.appendChild(el('div', 'error',
        `Самопроверка НЕ пройдена: найдено ${r.found} из ${r.placed}` +
        (r.missing && r.missing.length ? `, потеряны: ${r.missing.join(', ')}` : '') +
        (r.reason ? `. ${r.reason}` : '') + '. Пришлите этот текст.'));
    }
  } catch (e) { showError(out, e); }
});

function showFrame() {
  const box = $('#vision-frame-box');
  const img = $('#vis-frame');
  box.hidden = false;
  img.src = '/api/vision/frame.jpg?t=' + Date.now();
}

$('#vis-icons').addEventListener('click', async (e) => {
  const btn = e.target;
  btn.disabled = true;
  $('#vision-status').textContent =
    'Скачиваю портреты героев (127 штук), это займёт около минуты…';
  try {
    const r = await api('/api/vision/icons', { method: 'POST' });
    $('#vision-status').textContent =
      `Портретов на диске: ${r.have} из ${r.total}` +
      (r.failed ? `, не удалось скачать: ${r.failed}. Причина: ${r.reason}` : '') +
      (r.ready ? '. Можно включать слежение.' : '');
  } catch (err) { showError($('#vision-status'), err); }
  btn.disabled = false;
});

async function pollVision() {
  if (!vision.running) return;
  try { renderVision(await api('/api/vision/state')); }
  catch (e) { /* сеть моргнула — ждём следующего опроса */ }
}

// Стороны делятся по центру экрана. Делить по середине между найденными
// портретами нельзя: пока враги не выбрали героев, найдена одна команда,
// и такой делитель режет её пополам.
function splitBySide(heroes, frameWidth) {
  const side = $('#vis-side').value;
  if (side === 'none') return { enemy: heroes, ally: [] };
  const mid = frameWidth ? frameWidth / 2 : null;
  if (!mid) return { enemy: heroes, ally: [] };
  const left = heroes.filter((h) => h.box[0] + h.box[2] / 2 < mid);
  const right = heroes.filter((h) => h.box[0] + h.box[2] / 2 >= mid);
  return side === 'left' ? { ally: left, enemy: right } : { ally: right, enemy: left };
}

// параметр называется vs, а не state: глобальный state — это драфт,
// перекрыть его здесь значит сломать подстановку героев
function renderVision(vs) {
  const out = $('#vision-out');
  out.innerHTML = '';
  if (!vs) { out.appendChild(el('div', 'empty-hint', 'Нет данных.')); return; }

  const head = el('div', 'dim');
  head.textContent = `Режим: ${vs.mode}` +
    (vs.template_width ? ` · размер портрета ${vs.template_width} px` : '') +
    ` · распознано: ${(vs.heroes || []).length}` +
    (vs.frame_brightness !== null && vs.frame_brightness !== undefined
      ? ` · яркость кадра ${vs.frame_brightness}` : '');
  out.appendChild(head);

  if (vs.last_error) out.appendChild(el('div', 'error', vs.last_error));
  if (vs.frame_hint) out.appendChild(el('div', 'error', vs.frame_hint));
  // превью перекачиваем только когда был новый снимок, а не каждый опрос
  if (vs.scans && vs.scans !== vision.lastScans) {
    vision.lastScans = vs.scans;
    showFrame();
  }

  const heroes = vs.heroes || [];
  if (!heroes.length) {
    out.appendChild(el('div', 'empty-hint',
      'Героев на экране не видно. Откройте экран драфта и нажмите «Сканировать сейчас».'));
    return;
  }

  const { enemy, ally } = splitBySide(heroes, vs.frame_width);
  const row = el('div', 'draft-row');
  heroes.forEach((h) => {
    const chip = el('div', 'chip');
    const known = state.byId.get(h.hero_id);
    if (known && known.img) {
      const img = el('img'); img.src = known.img; img.alt = h.name; chip.appendChild(img);
    }
    chip.appendChild(el('span', '', h.name));
    chip.appendChild(el('span', 'dim', String(h.score)));
    const isAlly = ally.indexOf(h) !== -1;
    chip.title = isAlly ? 'определён как свой' : 'определён как враг';
    if (isAlly) {
      chip.style.borderColor = 'var(--radiant)';
      // на своём герое - кнопка «это я»: сразу считается сборка
      const meBtn = el('button', 'tab', me.heroId === h.hero_id ? 'это я ✓' : 'это я');
      meBtn.style.padding = '2px 8px';
      meBtn.style.fontSize = '11px';
      meBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        setMyHero(h.hero_id, 'кнопка «это я»');
        renderVision(vs);
      });
      chip.appendChild(meBtn);
    }
    row.appendChild(chip);
  });
  out.appendChild(row);

  // подставляем распознанное в драфт, не трогая то, что выбрано руками
  vision.lastState = vs;
  const key = heroes.map((h) => h.hero_id).sort().join(',') + '|' + $('#vis-side').value;
  if (key !== vision.lastKey) {
    vision.lastKey = key;
    applyVision(enemy, ally);
  }

  const note = el('div', 'dim');
  note.style.marginTop = '6px';
  note.textContent = 'Распознанное подставляется в драфт автоматически. ' +
    'Выбранного вручную героя это не перезаписывает.';
  out.appendChild(note);
}

function applyVision(enemy, ally) {
  let changed = false;
  const put = (kind, list) => {
    list.slice(0, 5).forEach((h) => {
      if (usedIds().has(h.hero_id)) return;
      if (state.draft[kind].length >= 5) return;
      state.draft[kind].push(h.hero_id);
      changed = true;
    });
  };
  put('ally', ally);
  put('enemy', enemy);
  if (changed) {
    renderSlots();
    renderHeroGrid();
    refreshRecommendations();
  }
  return changed;
}

// ---------------------------------------------------------------- мета
['#meta-bracket', '#meta-position'].forEach((sel) =>
  $(sel).addEventListener('change', loadMeta));

let metaLoaded = false;
async function loadMeta(force) {
  const out = $('#meta-out');
  if (metaLoaded && force === undefined && out.dataset.ready === '1' &&
      out.dataset.key === metaKey()) return;
  out.className = 'loading';
  out.textContent = 'Загрузка…';
  try {
    const q = new URLSearchParams({
      bracket: $('#meta-bracket').value,
      position: $('#meta-position').value,
    });
    const data = await api('/api/meta?' + q);
    out.className = 'scroll';
    out.dataset.ready = '1';
    out.dataset.key = metaKey();
    metaLoaded = true;
    renderMeta(out, data.rows);
  } catch (e) {
    out.className = '';
    showError(out, e);
  }
}

const metaKey = () => $('#meta-bracket').value + '|' + $('#meta-position').value;

function renderMeta(out, rows) {
  out.innerHTML = '';
  const table = el('table');
  table.innerHTML = `<thead><tr>
    <th>#</th><th>Герой</th><th class="num">Винрейт</th><th class="num">Пики</th>
    <th class="num">Доля пиков</th><th class="num">Про-пики</th>
    <th class="num">Про-баны</th><th class="num">Про-винрейт</th></tr></thead>`;
  const tb = el('tbody');
  rows.forEach((r, i) => {
    const tr = el('tr');
    tr.appendChild(el('td', 'dim', String(i + 1)));
    const td = el('td');
    const cell = el('div', 'hero-cell');
    const img = el('img');
    img.src = r.img;
    img.alt = r.name;
    img.loading = 'lazy';
    cell.appendChild(img);
    cell.appendChild(el('span', '', r.name));
    td.appendChild(cell);
    tr.appendChild(td);
    tr.appendChild(el('td', 'num', r.winrate === null ? '—' : r.winrate.toFixed(2) + '%'));
    tr.appendChild(el('td', 'num dim', r.picks.toLocaleString('ru-RU')));
    tr.appendChild(el('td', 'num dim', r.pick_share.toFixed(2) + '%'));
    tr.appendChild(el('td', 'num dim', String(r.pro_pick)));
    tr.appendChild(el('td', 'num dim', String(r.pro_ban)));
    tr.appendChild(el('td', 'num dim',
      r.pro_winrate === null ? '—' : r.pro_winrate.toFixed(1) + '%'));
    attachGuide(tr, r.id, 8, () => ({ months: 3, tier: 'top' }));
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  out.appendChild(table);
}

// ---------------------------------------------------------------- гайд по клику
// Строка таблицы («Турниры», «Мета») раскрывается по клику: сборка и
// прокачка героя по турнирным матчам - тот же показ, что у своего героя
// в «Драфте», только без сборки против драфта. Открыт один гайд за раз.
let guideToken = 0;

function attachGuide(tr, heroId, colSpan, opts) {
  tr.classList.add('clickable');
  tr.title = 'Показать сборку и прокачку по турнирным матчам';
  tr.addEventListener('click', () => {
    const next = tr.nextElementSibling;
    const wasOpen = next && next.classList.contains('guide-row');
    tr.parentElement.querySelectorAll('.guide-row').forEach((g) => g.remove());
    tr.parentElement.querySelectorAll('tr.open').forEach((g) => g.classList.remove('open'));
    guideToken += 1;
    if (wasOpen) return;
    const row = el('tr', 'guide-row');
    const td = el('td');
    td.colSpan = colSpan;
    const box = el('div', 'guide');
    td.appendChild(box);
    row.appendChild(td);
    tr.after(row);
    tr.classList.add('open');
    loadGuide(box, heroId, opts(), guideToken);
  });
}

async function loadGuide(box, heroId, opts, token, quiet) {
  const hero = state.byId.get(heroId);
  const months = Math.min(Number(opts.months) || 3, 3);
  const tier = opts.tier || 'top';
  if (!quiet) {
    box.className = 'guide loading';
    box.textContent = `Собираю сборку ${hero ? hero.name : ''} по турнирным матчам…`;
  }
  try {
    const b = await api('/api/build?' + new URLSearchParams({ hero: heroId, months, tier }));
    if (token !== guideToken) return;
    box.className = 'guide';
    renderBuild(box, b, hero, '');
    const sk = el('div');
    sk.style.marginTop = '10px';
    sk.appendChild(el('div', 'loading', 'Считаю прокачку и таланты…'));
    box.appendChild(sk);
    loadGuideSkills(sk, heroId, months, tier, token);
    if (b.pending) pollLater(() => token === guideToken && loadGuide(box, heroId, opts, token, true), 'guide');
    else pollDone('guide');
  } catch (e) { if (token === guideToken) { box.className = 'guide'; showError(box, e); } }
}

async function loadGuideSkills(sk, heroId, months, tier, token) {
  try {
    const s = await api('/api/skills?' + new URLSearchParams({ hero: heroId, months, tier }));
    if (token !== guideToken) return;
    sk.innerHTML = '';
    renderSkills(sk, s);
    if (s.pending) pollLater(() => token === guideToken && loadGuideSkills(sk, heroId, months, tier, token), 'guide-skills');
    else pollDone('guide-skills');
  } catch (e) { if (token === guideToken) { sk.innerHTML = ''; showError(sk, e); } }
}

// ---------------------------------------------------------------- турниры
const tour = { rows: [], total: 0, leaguesFor: null };

['#tour-months', '#tour-tier', '#tour-league'].forEach((sel) =>
  $(sel).addEventListener('change', () => loadTournaments(true)));
// сортировка и позиция - без похода на сервер: данные те же, меняется показ
$('#tour-sort').addEventListener('change', () => renderTournaments());
$('#tour-position').addEventListener('change', () => renderTournaments());

async function loadTournamentLeagues() {
  const months = $('#tour-months').value;
  if (tour.leaguesFor === months) return;
  let data;
  try {
    data = await api('/api/tournaments/leagues?months=' + months);
  } catch (e) {
    return; // без списка турниров вкладка работает: остаётся «Все»
  }
  if (data.pending) pollLater(loadTournamentLeagues, 'leagues');
  else pollDone('leagues');
  if (!data.leagues.length) return;
  tour.leaguesFor = months;
  const sel = $('#tour-league');
  const keep = sel.value;
  sel.innerHTML = '';
  const all = document.createElement('option');
  all.value = ''; all.textContent = 'Все';
  sel.appendChild(all);
  data.leagues.forEach((l) => {
    const o = document.createElement('option');
    o.value = l.leagueid;
    o.textContent = `${l.name} — ${l.matches} матчей (${l.tier})`;
    sel.appendChild(o);
  });
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

let tourLoaded = false;
let tourToken = 0;
async function loadTournaments(force, recompute) {
  if (tourLoaded && !force) return;
  const out = $('#tour-out');
  if (!tourLoaded) {
    out.className = 'loading';
    out.textContent = 'Загрузка…';
  }
  const token = ++tourToken;
  // список турниров - параллельно, таблицу он не задерживает
  loadTournamentLeagues();
  try {
    const q = new URLSearchParams({
      months: $('#tour-months').value,
      tier: $('#tour-tier').value,
      league: $('#tour-league').value,
    });
    if (recompute) q.set('force', '1');
    const data = await api('/api/tournaments?' + q);
    if (token !== tourToken) return;
    tour.rows = data.rows;
    tour.total = data.total_matches;
    tour.note = data.note;
    tour.pending = data.pending;
    tourLoaded = true;
    out.className = 'scroll';
    renderTournaments();
    // сервер показал снимок или прежнюю копию, свежее считает в фоне
    if (data.pending) pollLater(() => token === tourToken && loadTournaments(true), 'tour');
    else pollDone('tour');
  } catch (e) {
    out.className = '';
    showError(out, e);
  }
}
$('#tour-refresh').addEventListener('click', () => loadTournaments(true, true));

function renderTournaments() {
  const out = $('#tour-out');
  const key = $('#tour-sort').value;
  // фильтр по позиции - по справочнику позиций из про-матчей
  // (positions.json), он приходит вместе со списком героев
  const position = Number($('#tour-position').value) || 0;
  const rows = tour.rows
    .filter((r) => !position || ((state.byId.get(r.id) || {}).positions || []).includes(position))
    .sort((a, b) => {
      const av = a[key] === null ? -1 : a[key];
      const bv = b[key] === null ? -1 : b[key];
      return bv - av;
    });
  $('#tour-summary').textContent =
    `Матчей в выборке: ${tour.total}. Пик-рейт и бан-рейт — доля матчей, в которых ` +
    'героя взяли или забанили; спорность — их сумма. Винрейт считается только по пикам.' +
    (position ? ' Позиции героев — по справочнику из про-матчей; один герой может стоять на нескольких.' : '');
  out.innerHTML = '';
  // откуда данные: снимок, прежняя копия, считается, OpenDota не отвечает
  if (tour.note) {
    out.appendChild(el('div', tour.pending || !/не отвечает/.test(tour.note) ? 'dim' : 'error', tour.note));
  }
  if (!rows.length) {
    out.appendChild(el('div', 'empty-hint', position
      ? 'На этой позиции за период никого не брали.'
      : (tour.pending ? 'Считаю по базе турнирных матчей…' : 'За этот период матчей нет.')));
    return;
  }
  const table = el('table');
  table.innerHTML = `<thead><tr>
    <th>#</th><th>Герой</th><th class="num">Спорность</th><th class="num">Пики</th>
    <th class="num">Пик-рейт</th><th class="num">Баны</th><th class="num">Бан-рейт</th>
    <th class="num">Винрейт</th></tr></thead>`;
  const tb = el('tbody');
  rows.forEach((r, i) => {
    const tr = el('tr');
    tr.appendChild(el('td', 'dim', String(i + 1)));
    const td = el('td');
    const cell = el('div', 'hero-cell');
    if (r.img) {
      const img = el('img'); img.src = r.img; img.alt = r.name; img.loading = 'lazy';
      cell.appendChild(img);
    }
    const nameBox = el('div');
    nameBox.appendChild(el('div', '', r.name || String(r.id)));
    // позиции героя мелко под именем: видно, кто где играет, даже без фильтра
    const pos = (state.byId.get(r.id) || {}).positions || [];
    if (pos.length) nameBox.appendChild(el('div', 'dim', 'поз. ' + pos.join(', ')));
    cell.appendChild(nameBox);
    td.appendChild(cell);
    tr.appendChild(td);
    tr.appendChild(el('td', 'num', r.contest_rate.toFixed(1) + '%'));
    tr.appendChild(el('td', 'num dim', String(r.picks)));
    tr.appendChild(el('td', 'num dim', r.pick_rate.toFixed(1) + '%'));
    tr.appendChild(el('td', 'num dim', String(r.bans)));
    tr.appendChild(el('td', 'num dim', r.ban_rate.toFixed(1) + '%'));
    const wr = el('td', 'num', r.winrate === null ? '—' : r.winrate.toFixed(1) + '%');
    if (r.winrate !== null && r.picks < 10) wr.classList.add('dim');
    wr.title = r.picks < 10 ? 'меньше 10 пиков — винрейт ненадёжен' : '';
    tr.appendChild(wr);
    // по клику - сборка и прокачка героя за тот же период и уровень турниров
    attachGuide(tr, r.id, 8, () => ({ months: $('#tour-months').value, tier: $('#tour-tier').value }));
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  out.appendChild(table);
}


// ---------------------------------------------------------------- варды
// Тепловая карта вардов по турнирным матчам: клетки карты (64..192 по
// обеим осям, как в логах OpenDota) рисуются поверх картинки карты.
// Картинка идёт через сервер (/api/map, кэш); если не скачалась -
// схема: реки, линии, базы. Данные - как у турниров: памятка или
// «считается», страница дозапрашивает.
const ward = { data: null, token: 0, active: -1, img: null, imgFailed: false };
const MAP_MIN = 64;
const MAP_SIZE = 128;

['#ward-kind', '#ward-side', '#ward-window', '#ward-hero', '#ward-months', '#ward-tier'].forEach((sel) =>
  $(sel).addEventListener('change', () => loadWards(false)));
$('#ward-refresh').addEventListener('click', () => loadWards(true));

function fillWardHeroSelect() {
  const sel = $('#ward-hero');
  const keep = sel.value;
  sel.innerHTML = '<option value="">все</option>';
  state.heroes.forEach((h) => {
    const o = document.createElement('option');
    o.value = h.id;
    o.textContent = h.name;
    sel.appendChild(o);
  });
  if (keep) sel.value = keep;
}

function loadMapImage() {
  if (ward.img || ward.imgFailed) return;
  const img = new Image();
  img.onload = () => { ward.img = img; drawWards(); };
  img.onerror = () => { ward.imgFailed = true; drawWards(); };
  img.src = '/api/map';
}

async function loadWards(recompute, quiet) {
  const out = $('#ward-out');
  if (!quiet) { out.className = 'loading'; out.textContent = 'Загрузка…'; }
  loadMapImage();
  const token = ++ward.token;
  try {
    const q = new URLSearchParams({
      kind: $('#ward-kind').value, side: $('#ward-side').value, window: $('#ward-window').value,
      hero: $('#ward-hero').value, months: $('#ward-months').value, tier: $('#ward-tier').value,
    });
    if (recompute) q.set('force', '1');
    const data = await api('/api/wards?' + q);
    if (token !== ward.token) return;
    ward.data = data;
    ward.active = -1;
    out.className = '';
    renderWards(out, data);
    drawWards();
    if (data.pending) pollLater(() => token === ward.token && loadWards(false, true), 'wards');
    else pollDone('wards');
  } catch (e) {
    if (token === ward.token) { out.className = ''; showError(out, e); }
  }
}

function renderWards(out, data) {
  const kindName = data.kind === 'sen' ? 'стражей' : 'обзорных вардов';
  const sideName = { radiant: 'Radiant', dire: 'Dire', both: 'обеих сторон' }[data.side] || data.side;
  const hero = data.hero_id ? (state.byId.get(data.hero_id) || {}).name : null;
  const per = data.total_matches ? (data.wards / data.total_matches).toFixed(1) : '0';
  $('#ward-summary').textContent = data.total_matches
    ? `${data.wards} ${kindName} ${sideName}${hero ? ` (${hero})` : ''} в ${data.total_matches} матчах, ` +
      `минуты ${data.from < 0 ? 'до рога' : data.from}–${data.to >= 180 ? 'конец' : data.to}: ` +
      `в среднем ${per} за матч.`
    : '';
  out.innerHTML = '';
  if (data.note) out.appendChild(el('div', data.pending || !/не отвечает/.test(data.note) ? 'dim' : 'error', data.note));
  if (!data.spots.length) {
    out.appendChild(el('div', 'empty-hint', data.pending ? 'Считаю по базе турнирных матчей…' : 'Вардов за период нет.'));
    return;
  }
  const max = data.spots[0].n || 1;
  data.spots.forEach((s, i) => {
    const row = el('div', 'ward-spot');
    row.appendChild(el('span', 'num', String(i + 1)));
    const txt = el('div');
    txt.style.minWidth = '120px';
    txt.appendChild(el('div', '', `${s.n} шт. · ${data.total_matches ? Math.round(s.matches / data.total_matches * 100) : 0}% матчей`));
    txt.appendChild(el('div', 'dim', `клетка ${s.x}, ${s.y}`));
    row.appendChild(txt);
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = Math.round(s.n / max * 100) + '%';
    bar.appendChild(fill);
    row.appendChild(bar);
    row.addEventListener('click', () => {
      ward.active = ward.active === i ? -1 : i;
      out.querySelectorAll('.ward-spot').forEach((r, j) => r.classList.toggle('active', j === ward.active));
      drawWards();
    });
    out.appendChild(row);
  });
}

// координаты клеток -> пиксели холста; ось y у игры вверх, у холста вниз
function wardXY(x, y, size) {
  return [(x - MAP_MIN) / MAP_SIZE * size, (1 - (y - MAP_MIN) / MAP_SIZE) * size];
}

function drawMapSchematic(ctx, size) {
  ctx.fillStyle = '#12351a';
  ctx.fillRect(0, 0, size, size);
  // река - диагональ из левого верхнего угла в правый нижний
  ctx.strokeStyle = '#1f4f6b';
  ctx.lineWidth = size * 0.06;
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(size, size); ctx.stroke();
  // линии: верх, низ и мид
  ctx.strokeStyle = 'rgba(200,180,120,.35)';
  ctx.lineWidth = size * 0.02;
  const m = size * 0.12;
  ctx.beginPath(); ctx.moveTo(m, size - m); ctx.lineTo(m, m); ctx.lineTo(size - m, m); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(m, size - m); ctx.lineTo(size - m, size - m); ctx.lineTo(size - m, m); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(m, size - m); ctx.lineTo(size - m, m); ctx.stroke();
  // базы
  ctx.fillStyle = 'rgba(63,185,80,.35)';
  ctx.beginPath(); ctx.arc(m * 0.7, size - m * 0.7, size * 0.09, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = 'rgba(226,84,74,.35)';
  ctx.beginPath(); ctx.arc(size - m * 0.7, m * 0.7, size * 0.09, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = 'rgba(255,255,255,.5)';
  ctx.font = `${Math.round(size * 0.022)}px sans-serif`;
  ctx.fillText('схема: картинка карты не скачалась', size * 0.02, size * 0.98);
}

function drawWards() {
  const canvas = $('#ward-map');
  const ctx = canvas.getContext('2d');
  const size = canvas.width;
  ctx.clearRect(0, 0, size, size);
  if (ward.img) ctx.drawImage(ward.img, 0, 0, size, size);
  else drawMapSchematic(ctx, size);
  const data = ward.data;
  if (!data || !data.cells.length) return;
  const max = data.cells[0].n || 1;
  const color = data.kind === 'sen' ? '90,160,255' : '255,200,40';
  // тепло: круг с прозрачностью по частоте; радиус - две клетки
  const r = size / MAP_SIZE * 2.2;
  data.cells.forEach((c) => {
    const [px, py] = wardXY(c.x, c.y, size);
    const a = 0.15 + 0.7 * Math.sqrt(c.n / max);
    const g = ctx.createRadialGradient(px, py, 0, px, py, r);
    g.addColorStop(0, `rgba(${color},${a})`);
    g.addColorStop(1, `rgba(${color},0)`);
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.fill();
  });
  // номера точек
  data.spots.forEach((s, i) => {
    const [px, py] = wardXY(s.x, s.y, size);
    const active = i === ward.active;
    const rr = active ? size * 0.024 : size * 0.016;
    ctx.beginPath(); ctx.arc(px, py, rr, 0, Math.PI * 2);
    ctx.fillStyle = active ? '#fff' : 'rgba(232,122,33,.95)';
    ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = active ? '#e87a21' : 'rgba(0,0,0,.6)'; ctx.stroke();
    ctx.fillStyle = active ? '#e87a21' : '#fff';
    ctx.font = `bold ${Math.round(rr * 1.2)}px sans-serif`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(String(i + 1), px, py + 1);
  });
}

// ---------------------------------------------------------------- про-матчи
let proLoaded = false;
// список про-матчей перечитывается не чаще раза в 10 минут (столько живёт
// кэш на сервере), но и не реже: раньше он грузился один раз за сессию, и
// открытое окно сутками показывало один и тот же список
const PRO_LIST_TTL_MS = 10 * 60 * 1000;
let proLoadedAt = 0;

function freshnessLine(f) {
  // честно про возраст данных: пользователь должен видеть, что смотрит
  // на вчерашнюю копию, если OpenDota не отвечает
  const line = el('div', f && f.error ? 'error' : 'dim');
  line.style.margin = '0 0 6px';
  if (!f || f.updated_at === null || f.updated_at === undefined) {
    line.textContent = 'данные ещё не загружались';
    return line;
  }
  const min = Math.round((f.age || 0) / 60);
  const age = min < 1 ? 'только что' : min < 60 ? `${min} мин назад`
    : min < 48 * 60 ? `${Math.round(min / 60)} ч назад` : `${Math.round(min / 1440)} дн назад`;
  line.textContent = f.error
    ? `Свежий список получить не удалось (${f.error}). Показана копия: обновлена ${age}.`
    : `Обновлено ${age}.`;
  line.title = 'OpenDota публикует про-матч только после разбора реплея - обычно через ' +
    'несколько часов после игры, и только по лигам из своего справочника. ' +
    'Поэтому список отстаёт от сайтов с результатами.';
  return line;
}

// «сегодня 16:41», «вчера 23:10», иначе «12.09 18:00» - по местному времени
function whenPlayed(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((day(now) - day(d)) / 86400000);
  if (diffDays === 0) return 'сегодня ' + hm;
  if (diffDays === 1) return 'вчера ' + hm;
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' + hm;
}

async function loadProList(force) {
  if (!force && proLoadedAt && Date.now() - proLoadedAt < PRO_LIST_TTL_MS) return;
  const out = $('#pro-list');
  try {
    const data = await api('/api/pro/matches');
    proLoadedAt = Date.now();
    proLoaded = true;
    out.className = 'scroll';
    out.innerHTML = '';
    const head = el('div');
    head.style.display = 'flex';
    head.style.gap = '8px';
    head.style.alignItems = 'baseline';
    head.appendChild(freshnessLine(data.freshness));
    const refresh = el('button', 'tab', 'Обновить');
    refresh.style.marginLeft = 'auto';
    refresh.addEventListener('click', () => loadProList(true));
    head.appendChild(refresh);
    out.appendChild(head);
    data.matches.forEach((m) => {
      const card = el('div', 'match');
      const teams = el('div', 'teams');
      teams.appendChild(el('span', m.radiant_win ? 'win' : 'lose', m.radiant));
      teams.appendChild(el('span', 'dim', (m.score || []).join(':')));
      teams.appendChild(el('span', m.radiant_win === false ? 'win' : 'lose', m.dire));
      card.appendChild(teams);
      const meta = el('div', 'dim');
      const mins = m.duration ? Math.round(m.duration / 60) + ' мин' : '';
      // когда сыгран: без этого не видно, что OpenDota отдаёт матчи с
      // отставанием на часы - реплей сначала надо скачать и разобрать
      meta.textContent = [whenPlayed(m.start_time), m.league, mins].filter(Boolean).join(' · ');
      card.appendChild(meta);
      card.addEventListener('click', () => loadProMatch(m.match_id));
      out.appendChild(card);
    });
  } catch (e) {
    out.className = '';
    showError(out, e);
  }
}

async function loadProMatch(id) {
  const out = $('#pro-detail');
  out.innerHTML = '<div class="loading">Загружаю матч: драфт, игроки, закупы — три коротких ' +
    'запроса к базе OpenDota…</div>';
  try {
    const m = await api('/api/pro/match?id=' + id);
    renderProMatch(out, m);
  } catch (e) {
    showError(out, e);
  }
}

function renderProMatch(out, m) {
  out.innerHTML = '';

  const head = el('div');
  head.style.marginBottom = '12px';
  const title = el('div');
  title.innerHTML =
    `<b class="${m.radiant_win ? 'win' : 'lose'}">${m.radiant_name}</b>` +
    ` <span class="dim">${(m.score || []).join(' : ')}</span> ` +
    `<b class="${m.radiant_win === false ? 'win' : 'lose'}">${m.dire_name}</b>`;
  head.appendChild(title);
  head.appendChild(el('div', 'dim',
    [m.league, m.duration ? Math.round(m.duration / 60) + ' мин' : '', 'ID ' + m.match_id]
      .filter(Boolean).join(' · ')));
  out.appendChild(head);

  if (m.edge && m.edge.radiant_edge_pp !== undefined) {
    const e = m.edge.radiant_edge_pp;
    const box = el('div', 'note');
    const who = e > 0 ? m.radiant_name : m.dire_name;
    box.textContent =
      `По матчапам драфт был удобнее для ${who}: ${sign(Math.abs(e))} п.п. ` +
      `в сторону ${e > 0 ? 'Radiant' : 'Dire'}. ` +
      `Это оценка только по историческим матчапам — она не учитывает ни игроков, ни исполнение.`;
    out.appendChild(box);
  }

  if (m.has_draft) {
    ['radiant', 'dire'].forEach((side) => {
      const picks = m.draft_order.filter((d) => d.team === side && d.is_pick);
      const bans = m.draft_order.filter((d) => d.team === side && !d.is_pick);
      const wrap = el('div');
      wrap.style.marginBottom = '10px';
      wrap.appendChild(el('div', 'dim',
        (side === 'radiant' ? m.radiant_name : m.dire_name) + ' — пики'));
      wrap.appendChild(heroRow(picks));
      if (bans.length) {
        wrap.appendChild(el('div', 'dim', 'баны'));
        wrap.appendChild(heroRow(bans, true));
      }
      out.appendChild(wrap);
    });
  } else {
    out.appendChild(el('div', 'dim', 'Порядок драфта для этого матча недоступен.'));
  }

  [['radiant', m.radiant_name, m.radiant], ['dire', m.dire_name, m.dire]]
    .forEach(([, name, players]) => {
      if (!players || !players.length) return;
      const card = el('div');
      card.style.marginTop = '12px';
      card.appendChild(el('div', 'dim', name));
      const table = el('table');
      table.innerHTML =
        '<thead><tr><th>Герой</th><th>Игрок</th><th class="num">K/D/A</th>' +
        '<th class="num">GPM</th><th class="num">XPM</th><th class="num">Нетворс</th></tr></thead>';
      const tb = el('tbody');
      players.forEach((p) => {
        const tr = el('tr');
        const td = el('td');
        const cell = el('div', 'hero-cell');
        if (p.img) {
          const img = el('img');
          img.src = p.img;
          img.alt = p.name;
          cell.appendChild(img);
        }
        cell.appendChild(el('span', '', p.name || '—'));
        td.appendChild(cell);
        tr.appendChild(td);
        tr.appendChild(el('td', 'dim', p.player || '—'));
        tr.appendChild(el('td', 'num', (p.kda || []).join(' / ')));
        tr.appendChild(el('td', 'num dim', String(orDash(p.gpm))));
        tr.appendChild(el('td', 'num dim', String(orDash(p.xpm))));
        tr.appendChild(el('td', 'num dim',
          p.net_worth ? p.net_worth.toLocaleString('ru-RU') : '—'));
        tb.appendChild(tr);

        // строка с предметами: старт, сборка по минутам, итог
        if (p.items) {
          const tri = el('tr');
          const tdi = el('td');
          tdi.colSpan = 6;
          tdi.appendChild(itemsBlock(p.items));
          tri.appendChild(tdi);
          tb.appendChild(tri);
        }
      });
      table.appendChild(tb);
      card.appendChild(table);
      out.appendChild(card);
    });
}

// предмет в сборке: иконка с бейджем (минута или число штук), под ней
// доля игр и винрейт с этим предметом
function buildItem(it, badge) {
  const card = el('div', 'build-item');
  card.appendChild(itemIcon(it, badge || undefined));
  const stat = el('div', 'build-stat');
  stat.appendChild(el('span', '', `${it.share}%`));
  if (it.winrate !== null && it.winrate !== undefined) {
    stat.appendChild(el('span', it.winrate >= 50 ? 'pos' : 'neg', `${it.winrate}%`));
  }
  card.appendChild(stat);
  card.title = `${it.dname}${it.cost ? ` — ${it.cost} зол.` : ''}: куплен в ${it.share}% игр` +
    (it.winrate !== null && it.winrate !== undefined ? `, винрейт с ним ${it.winrate}%` : '') +
    (badge ? `, ${badge.startsWith('×') ? badge.slice(1) + ' шт.' : 'медианная минута ' + badge}` : '');
  // взаимоисключающие предметы (ботинки, апгрейды блинка): в сборке один,
  // остальные - «или …» под ним, чтобы два вида ботинок не читались как оба
  if (it.alternatives && it.alternatives.length) {
    const alt = el('div', 'build-alt');
    alt.appendChild(el('span', 'dim', 'или'));
    it.alternatives.forEach((a) => {
      const chip = el('span', 'build-alt-item');
      chip.appendChild(itemIcon(a));
      chip.appendChild(el('span', 'dim', `${a.share}%`));
      chip.title = `${a.dname}: вместо этого в ${a.share}% игр` +
        (a.winrate !== null && a.winrate !== undefined ? `, винрейт ${a.winrate}%` : '');
      alt.appendChild(chip);
    });
    card.appendChild(alt);
    card.title += '; или ' + it.alternatives.map((a) => `${a.dname} (${a.share}%)`).join(', ');
  }
  return card;
}

function itemIcon(it, label) {
  const wrap = el('span', 'item');
  if (it.img) {
    const img = el('img');
    img.src = it.img;
    img.alt = it.dname;
    img.loading = 'lazy';
    wrap.appendChild(img);
  } else {
    wrap.appendChild(el('span', 'dim', it.dname));
  }
  if (label !== undefined) wrap.appendChild(el('span', 'item-min', label));
  wrap.title = it.dname + (it.cost ? ` — ${it.cost} зол.` : '') +
    (label !== undefined ? ` — ${label} мин` : '');
  return wrap;
}

function itemsBlock(items) {
  const box = el('div', 'items');
  const line = (title, nodes) => {
    if (!nodes.length) return;
    const row = el('div', 'items-row');
    row.appendChild(el('span', 'dim items-title', title));
    nodes.forEach((n) => row.appendChild(n));
    box.appendChild(row);
  };
  if (!items.has_log && !items.final.length) {
    box.appendChild(el('span', 'dim', 'предметы недоступны: матч не разобран'));
    return box;
  }
  line('старт', items.start.map((it) => itemIcon(it)));
  line('сборка', items.build.map((it) => itemIcon(it, it.minute)));
  const fin = items.final.map((it) => itemIcon(it));
  if (items.neutral) fin.push(itemIcon(items.neutral, 'нейтр.'));
  line('итог', fin);
  return box;
}

function heroRow(items, isBan) {
  const row = el('div', 'draft-row');
  items.forEach((d) => {
    if (!d.img) return;
    const img = el('img', isBan ? 'ban' : '');
    img.src = d.img;
    img.alt = d.name;
    img.title = (isBan ? 'бан: ' : '') + (d.name || '');
    row.appendChild(img);
  });
  return row;
}

// ---------------------------------------------------------------- обновления
// сервер проверяет version.txt в релизе в фоне при старте; спрашиваем
// через несколько секунд и ещё раз позже, если сеть не успела
async function checkUpdate(attempt) {
  try {
    const u = await api('/api/update');
    if (u.available) {
      const link = $('#update-link');
      link.textContent = `Доступна версия ${u.latest} — скачать`;
      link.href = u.url;
      link.title = `У вас ${u.current}`;
      link.hidden = false;
      return;
    }
    if (!u.checked && attempt < 3) setTimeout(() => checkUpdate(attempt + 1), 15000);
  } catch (e) { /* без сети - без обновлений, это не ошибка */ }
}
setTimeout(() => checkUpdate(0), 6000);

boot();
