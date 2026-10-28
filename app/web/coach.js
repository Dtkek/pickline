// Голосовой тренер Pickline: Gemini Live прямо из браузера.
//
// Браузер сам держит WebSocket к Gemini Live: микрофон -> PCM 16 кГц,
// ответ - PCM (обычно 24 кГц) через WebAudio, перебивание (когда модель
// присылает interrupted - сразу глушим очередь). Данные матча берёт с
// сервера Pickline (GSI), скриншот - только когда модель или игрок просит.
//
// Документация Live API (ai.google.dev, проверено 2026-10-28): модель
// gemini-3.8-live; соединение живёт ~10 минут, сессия без сжатия контекста -
// 15 мин (аудио) / 2 мин (аудио+видео). Поэтому включены сжатие контекста
// (slidingWindow) и продление сессии (sessionResumption + GoAway):
// переподключаемся с последним handle, разговор продолжается.

const MODEL = 'models/gemini-3.8-live';
const WS_URL = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=';
const $ = (s) => document.querySelector(s);

const st = {
  ws: null, key: '', handle: null, running: false, setupDone: false,
  micCtx: null, micStream: null, micNode: null,
  outCtx: null, outTime: 0, sources: [],
  lastEvent: 0, lastSpoke: 0, eventsTimer: null,
  inLine: null, outLine: null,
};

// ------------------------------------------------------------------ интерфейс
function log(cls, text) {
  const box = $('#log');
  const d = document.createElement('div');
  d.className = cls;
  d.textContent = text;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}
function status(text) { $('#status').textContent = text; }

['aggr', 'support', 'empathy', 'humor', 'talk'].forEach((id) => {
  const r = $('#' + id);
  const save = () => { $('#v-' + id).textContent = r.value; try { localStorage.setItem('coach-' + id, r.value); } catch (e) { /* */ } };
  try { const v = localStorage.getItem('coach-' + id); if (v !== null) r.value = v; } catch (e) { /* */ }
  r.addEventListener('input', save);
  save();
});
['name', 'voice', 'swear'].forEach((id) => {
  const elx = $('#' + id);
  try {
    const v = localStorage.getItem('coach-' + id);
    if (v !== null) { if (elx.type === 'checkbox') elx.checked = v === '1'; else elx.value = v; }
  } catch (e) { /* */ }
  elx.addEventListener('change', () => {
    try { localStorage.setItem('coach-' + id, elx.type === 'checkbox' ? (elx.checked ? '1' : '0') : elx.value); } catch (e) { /* */ }
  });
});

async function loadKey() {
  const r = await fetch('/api/coach/key').then((x) => x.json());
  st.key = r.key || '';
  $('#key-note').textContent = st.key ? 'Ключ сохранён.' : 'Ключа нет: вставьте ключ и нажмите «Сохранить ключ».';
}
$('#key-save').addEventListener('click', async () => {
  const key = $('#key').value.trim();
  await fetch('/api/coach/key', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  $('#key').value = '';
  loadKey();
});
loadKey();

// ------------------------------------------------------------ характер -> промпт
function systemPrompt() {
  const v = (id) => Number($('#' + id).value);
  const name = $('#name').value.trim() || 'Тренер';
  return [
    `Ты — ${name}, голосовой тренер по Dota 2. Говоришь по-русски с игроком во время его матча.`,
    'Говори коротко: 1–2 предложения, по делу. В драке — одно слово-команда или молчи. Не пересказывай очевидное.',
    'Факты о матче бери только из инструмента get_game_state и из сообщений «[Событие игры]». Не выдумывай, чего не знаешь.',
    'Если нужно увидеть игру (карта, драка, магазин) — вызови look_at_screen. Снимок экрана — только по необходимости.',
    'Для сборки и предметов против врагов есть get_hero_build и counter_items. Для свежих новостей и патчей — поиск Google.',
    `Агрессия советов ${v('aggr')}/10 (0 — осторожно, фарм; 10 — постоянно зовёт драться).`,
    `Поддержка ${v('support')}/10 (насколько подбадриваешь).`,
    `Сопереживание ${v('empathy')}/10 (насколько мягко говоришь об ошибках).`,
    `Юмор ${v('humor')}/10.`,
    `Болтливость ${v('talk')}/10 (0 — только по делу и когда спросили; 10 — комментируешь всё).`,
    $('#swear').checked ? 'Мат разрешён, но без оскорблений игрока.' : 'Без мата.',
    'На смерть игрока реагируй коротко и подскажи, что сделать иначе, если понятно из данных.',
  ].join('\n');
}

const TOOLS = [
  { googleSearch: {} },
  {
    functionDeclarations: [
      { name: 'get_game_state', description: 'Текущее состояние матча игрока: время, герой, уровень, здоровье, мана, KDA, золото, предметы, способности.' },
      { name: 'look_at_screen', description: 'Сделать один снимок экрана игрока, чтобы посмотреть на игру.' },
      {
        name: 'get_hero_build', description: 'Сборка героя по турнирным матчам: стартовый закуп, порядок предметов.',
        parameters: { type: 'object', properties: { hero: { type: 'string', description: 'Имя героя на английском, например Juggernaut' } }, required: ['hero'] },
      },
      {
        name: 'counter_items', description: 'Какие предметы брать против героев врага.',
        parameters: {
          type: 'object',
          properties: {
            enemies: { type: 'array', items: { type: 'string' }, description: 'Имена героев врага на английском' },
            role: { type: 'string', enum: ['core', 'support'] },
          },
          required: ['enemies'],
        },
      },
    ],
  },
];

// ------------------------------------------------------------------ соединение
function send(obj) { if (st.ws && st.ws.readyState === 1) st.ws.send(JSON.stringify(obj)); }

function connect() {
  st.setupDone = false;
  const ws = new WebSocket(WS_URL + encodeURIComponent(st.key));
  ws.binaryType = 'arraybuffer';
  st.ws = ws;
  ws.onopen = () => {
    status('соединяюсь…');
    send({
      setup: {
        model: MODEL,
        generationConfig: {
          responseModalities: ['AUDIO'],
          speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: $('#voice').value } } },
        },
        systemInstruction: { parts: [{ text: systemPrompt() }] },
        tools: TOOLS,
        inputAudioTranscription: {},
        outputAudioTranscription: {},
        contextWindowCompression: { slidingWindow: {} },
        sessionResumption: st.handle ? { handle: st.handle } : {},
      },
    });
  };
  ws.onmessage = (ev) => {
    const text = typeof ev.data === 'string' ? ev.data : new TextDecoder().decode(ev.data);
    let msg;
    try { msg = JSON.parse(text); } catch (e) { return; }
    onMessage(msg);
  };
  ws.onerror = () => log('sys', 'Ошибка соединения с Gemini.');
  ws.onclose = (ev) => {
    if (ws !== st.ws) return;
    if (st.running) {
      // соединение живёт ~10 минут: продолжаем ту же сессию по handle
      log('sys', `Соединение закрыто (${ev.code}${ev.reason ? ': ' + ev.reason : ''}), переподключаюсь…`);
      if (!st.setupDone && !st.handle) { stop('Не удалось начать сессию: ' + (ev.reason || 'код ' + ev.code)); return; }
      setTimeout(() => st.running && connect(), 800);
    }
  };
}

function onMessage(msg) {
  if (msg.setupComplete !== undefined) {
    st.setupDone = true;
    status('слушаю');
    if (!st.micNode) startMic();
    return;
  }
  if (msg.sessionResumptionUpdate) {
    const u = msg.sessionResumptionUpdate;
    if (u.resumable && u.newHandle) st.handle = u.newHandle;
  }
  if (msg.goAway) {
    log('sys', 'Сервер скоро закроет соединение — переподключаюсь заранее.');
    const old = st.ws;
    connect();
    try { old.close(); } catch (e) { /* */ }
    return;
  }
  if (msg.toolCall) { handleTools(msg.toolCall.functionCalls || []); return; }
  const sc = msg.serverContent;
  if (!sc) return;
  if (sc.interrupted) stopPlayback();                 // игрок заговорил - замолкаем
  if (sc.inputTranscription && sc.inputTranscription.text) {
    if (!st.inLine) st.inLine = log('me', 'Вы: ');
    st.inLine.textContent += sc.inputTranscription.text;
  }
  if (sc.outputTranscription && sc.outputTranscription.text) {
    st.inLine = null;
    if (!st.outLine) st.outLine = log('coach-line', ($('#name').value || 'Тренер') + ': ');
    st.outLine.textContent += sc.outputTranscription.text;
  }
  ((sc.modelTurn && sc.modelTurn.parts) || []).forEach((p) => {
    if (p.inlineData && p.inlineData.data) {
      const m = /rate=(\d+)/.exec(p.inlineData.mimeType || '');
      play(p.inlineData.data, m ? Number(m[1]) : 24000);
      st.lastSpoke = Date.now();
    }
  });
  if (sc.turnComplete) { st.outLine = null; st.inLine = null; }
}

// ------------------------------------------------------------------ инструменты
async function heroIdByName(name) {
  const r = await fetch('/api/heroes').then((x) => x.json());
  const n = (name || '').toLowerCase().trim();
  const h = r.heroes.find((x) => (x.name || '').toLowerCase() === n)
    || r.heroes.find((x) => (x.name || '').toLowerCase().includes(n));
  return h ? h.id : null;
}

async function callTool(fc) {
  const a = fc.args || {};
  if (fc.name === 'get_game_state') {
    const s = await fetch('/api/coach/state').then((x) => x.json());
    return { summary: s.summary, hero: s.hero, team: s.team };
  }
  if (fc.name === 'look_at_screen') return lookAtScreen();
  if (fc.name === 'get_hero_build') {
    const id = await heroIdByName(a.hero);
    if (!id) return { error: 'герой не найден: ' + a.hero };
    const b = await fetch('/api/build?' + new URLSearchParams({ hero: id, months: 3, tier: 'top' })).then((x) => x.json());
    const names = (list) => (list || []).map((it) => it.dname).join(', ');
    return { games: b.games, start: names(b.start), order: names(b.order), situational: names(b.situational), note: b.note };
  }
  if (fc.name === 'counter_items') {
    const ids = [];
    for (const n of a.enemies || []) { const id = await heroIdByName(n); if (id) ids.push(id); }
    const c = await fetch('/api/counters?' + new URLSearchParams({ enemy: ids.join(','), role: a.role || 'core' })).then((x) => x.json());
    return { items: c.items.map((it) => `${it.dname}: ${it.why.join('; ')}`) };
  }
  return { error: 'неизвестный инструмент' };
}

async function handleTools(calls) {
  const responses = [];
  for (const fc of calls) {
    let response;
    try { response = await callTool(fc); } catch (e) { response = { error: String(e) }; }
    responses.push({ id: fc.id, name: fc.name, response });
  }
  send({ toolResponse: { functionResponses: responses } });
}

async function lookAtScreen() {
  const s = await fetch('/api/coach/screen').then((x) => x.json());
  if (s.error) { log('sys', s.error); return { error: s.error }; }
  send({ realtimeInput: { video: { data: s.data, mimeType: s.mime } } });
  log('sys', 'Тренер посмотрел на экран.' + (s.note ? ' ' + s.note : ''));
  return { ok: true, note: s.note || 'кадр отправлен' };
}
$('#look').addEventListener('click', async () => {
  if (!st.setupDone) return;
  await lookAtScreen();
  send({ realtimeInput: { text: 'Посмотри на мой экран и коротко подскажи.' } });
});

// ------------------------------------------------------------- события матча
// важные (смерть, убийство, 6/12/18 уровень, мало здоровья) - с ответом, но
// не чаще раза в 20 с и не поверх его речи; остальные - просто в контекст
async function pollEvents() {
  if (!st.setupDone) return;
  try {
    const r = await fetch('/api/coach/events?since=' + st.lastEvent).then((x) => x.json());
    if (!st.lastEvent) { st.lastEvent = r.last; return; }   // старые события не озвучиваем
    for (const e of r.events) {
      st.lastEvent = e.id;
      const quiet = Date.now() - st.lastSpoke > 2500;
      const answer = e.important && quiet && Date.now() - (st.lastPrompt || 0) > 20000;
      if (answer) st.lastPrompt = Date.now();
      send({ clientContent: { turns: [{ role: 'user', parts: [{ text: '[Событие игры] ' + e.text }] }], turnComplete: answer } });
      log('sys', '• ' + e.text);
    }
  } catch (e) { /* сервер занят - в следующий раз */ }
}

// ------------------------------------------------------------------ микрофон
const WORKLET = `
class Mic extends AudioWorkletProcessor {
  constructor() { super(); this.buf = []; this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) { this.buf.push(new Float32Array(ch)); this.n += ch.length; }
    if (this.n >= 1600) {                       // ~100 мс при 16 кГц
      const out = new Float32Array(this.n); let o = 0;
      for (const b of this.buf) { out.set(b, o); o += b.length; }
      this.port.postMessage(out, [out.buffer]); this.buf = []; this.n = 0;
    }
    return true;
  }
}
registerProcessor('mic', Mic);`;

function b64FromInt16(f32) {
  const i16 = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) { const s = Math.max(-1, Math.min(1, f32[i])); i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff; }
  const bytes = new Uint8Array(i16.buffer);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

async function startMic() {
  try {
    st.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (e) { stop('Нет доступа к микрофону: ' + e.message); return; }
  st.micCtx = new AudioContext({ sampleRate: 16000 });          // браузер сам пересэмплирует
  const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
  await st.micCtx.audioWorklet.addModule(url);
  const src = st.micCtx.createMediaStreamSource(st.micStream);
  st.micNode = new AudioWorkletNode(st.micCtx, 'mic');
  st.micNode.port.onmessage = (ev) => {
    if (st.setupDone) send({ realtimeInput: { audio: { data: b64FromInt16(ev.data), mimeType: 'audio/pcm;rate=16000' } } });
  };
  src.connect(st.micNode);
}

// ------------------------------------------------------------- воспроизведение
function play(b64, rate) {
  if (!st.outCtx) st.outCtx = new AudioContext();
  const bin = atob(b64);
  const n = bin.length >> 1;
  const f32 = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
    if (v >= 0x8000) v -= 0x10000;
    f32[i] = v / 0x8000;
  }
  const buf = st.outCtx.createBuffer(1, n, rate);
  buf.copyToChannel(f32, 0);
  const s = st.outCtx.createBufferSource();
  s.buffer = buf;
  s.connect(st.outCtx.destination);
  const t = Math.max(st.outCtx.currentTime + 0.02, st.outTime);
  s.start(t);
  st.outTime = t + buf.duration;
  st.sources.push(s);
  s.onended = () => { st.sources = st.sources.filter((x) => x !== s); };
}
function stopPlayback() {
  st.sources.forEach((s) => { try { s.stop(); } catch (e) { /* */ } });
  st.sources = [];
  st.outTime = 0;
}

// ------------------------------------------------------------------ старт/стоп
async function start() {
  await loadKey();
  if (!st.key) { status('нужен ключ Gemini API'); return; }
  st.running = true;
  st.handle = null;
  st.lastEvent = 0;
  $('#start').disabled = true;
  $('#stop').disabled = false;
  log('sys', 'Подключаюсь к Gemini Live…');
  connect();
  st.eventsTimer = setInterval(pollEvents, 2000);
}

function stop(reason) {
  st.running = false;
  clearInterval(st.eventsTimer);
  try { st.ws && st.ws.close(); } catch (e) { /* */ }
  st.ws = null;
  if (st.micStream) st.micStream.getTracks().forEach((t) => t.stop());
  if (st.micCtx) st.micCtx.close();
  st.micCtx = st.micNode = st.micStream = null;
  stopPlayback();
  st.setupDone = false;
  $('#start').disabled = false;
  $('#stop').disabled = true;
  status(reason || 'остановлено');
  if (reason) log('sys', reason);
}

$('#start').addEventListener('click', start);
$('#stop').addEventListener('click', () => stop());
