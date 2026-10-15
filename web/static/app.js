/* ChessBot web app — board, engine client, coach panel. */

import { Chess } from './vendor/chess.js';
import { pieceSvg } from './pieces.js';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const FILES = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'];
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

const state = {
  game: new Chess(),
  levels: [],
  level: 4,
  humanColor: 'w',
  flipped: false,
  selected: null,
  legalTargets: [],
  lastMove: null,
  thinking: false,
  evalCp: 0,
  mateIn: null,
  stats: { depth: 0, seldepth: 0, nodes: 0, nps: 0, time: 0 },
  pv: [],
  pvBook: false,
  opening: null,
  grades: new Map(),      // ply index -> {grade, loss_cp, best, note}
  reviewPly: null,        // null = live position
  coach: true,
  sound: true,
  autoEval: true,
  engineOnline: false,
  botBusy: false,
  pvStartTurn: 'w',
  pvFullmove: 1,
};

const SQUARE_ELS = new Map();
let drag = null;

/* ------------------------------------------------------------------ engine */

class EngineClient {
  /* Talks to the engine over a WebSocket, falling back to plain HTTP requests
   * when a proxy in front of the app does not carry websockets. */

  constructor() {
    this.url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    this.socket = null;
    this.seq = 0;
    this.pending = new Map();
    this.ready = false;
    this.backoff = 400;
    this.failures = 0;
    // /?transport=rest forces the HTTP path (handy when debugging deployments).
    this.mode = new URLSearchParams(location.search).get('transport') === 'rest' ? 'rest' : 'ws';
    if (this.mode === 'ws') this.connect();
    else setTimeout(() => dispatchEvent(new CustomEvent('engine-online')), 0);
  }

  connect() {
    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (err) {
      this.fail('Could not open the engine connection.');
      return;
    }
    this.socket = socket;

    socket.addEventListener('open', () => {
      const wasRest = this.mode === 'rest';
      this.ready = true;
      this.mode = 'ws';
      this.backoff = 400;
      this.failures = 0;
      setBanner(false);
      dispatchEvent(new CustomEvent('engine-online'));
      if (wasRest) toast('Streaming restored', 'Live search updates are back.', 'good');
    });

    socket.addEventListener('message', (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      const pending = this.pending.get(msg.id);
      if (!pending) return;
      if (msg.type === 'info') {
        pending.onInfo?.(msg);
        return;
      }
      this.pending.delete(msg.id);
      if (msg.type === 'error') pending.reject(new Error(msg.message || 'engine error'));
      else pending.resolve(msg);
    });

    const close = () => {
      this.ready = false;
      this.failures += 1;
      for (const [, pending] of this.pending) pending.reject(new Error('engine disconnected'));
      this.pending.clear();

      if (this.mode === 'ws' && this.failures >= 2) {
        // Websockets look unusable (a proxy that strips Upgrade headers, say):
        // keep playing through the REST endpoints and retry the socket quietly.
        this.mode = 'rest';
        setBanner(false);
        dispatchEvent(new CustomEvent('engine-online'));
        toast('Streaming unavailable',
          'Playing through plain HTTP requests instead — live search stats are limited.', 'warn');
        setTimeout(() => this.connect(), 15000);
        return;
      }
      if (this.mode === 'ws') {
        dispatchEvent(new CustomEvent('engine-offline'));
        setBanner(true, 'Lost the connection to the engine — retrying…');
        this.backoff = Math.min(this.backoff * 1.7, 6000);
        setTimeout(() => this.connect(), this.backoff);
      }
    };
    socket.addEventListener('close', close);
    socket.addEventListener('error', () => { /* close handler takes over */ });
  }

  fail(message) {
    toast('Engine offline', message, 'bad');
    setBanner(true, message);
  }

  send(payload, options = {}) {
    if (this.mode === 'rest') return this.sendRest(payload, options);
    return this.sendSocket(payload, options);
  }

  /** Plain HTTP fallback: one request, one response, no progress frames. */
  async sendRest(payload, { timeout = 60000 } = {}) {
    const post = async (path, body) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        const response = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
        return data;
      } finally {
        clearTimeout(timer);
      }
    };

    switch (payload.type) {
      case 'move': {
        const data = await post('/api/move', {
          fen: payload.fen, level: payload.level,
          movetime_ms: LEVEL_TIMES[payload.level] || 600,
        });
        return { type: 'result', ...data, human_to_move: false };
      }
      case 'hint': {
        const data = await post('/api/move', { fen: payload.fen, level: 6, movetime_ms: 1200 });
        return { type: 'result', ...data };
      }
      case 'eval': {
        const data = await post('/api/evaluate', {
          fen: payload.fen, movetime_ms: Math.max(250, payload.movetime_ms || 450),
        });
        return { type: 'result', ...data };
      }
      case 'grade': {
        const report = await post('/api/analyse', {
          fen: payload.fen, move: payload.move, movetime_ms: 400,
        });
        return { type: 'result', report };
      }
      default:
        throw new Error(`unsupported request ${payload.type}`);
    }
  }

  sendSocket(payload, { onInfo, timeout = 30000 } = {}) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error('engine not connected'));
    }
    const id = ++this.seq;
    const message = { id, ...payload };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('engine timeout'));
      }, timeout);
      this.pending.set(id, {
        onInfo,
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (err) => { clearTimeout(timer); reject(err); },
      });
      this.socket.send(JSON.stringify(message));
    });
  }
}

//: fallback think times per level (the server usually decides this for us)
const LEVEL_TIMES = { 1: 50, 2: 120, 3: 250, 4: 500, 5: 1000, 6: 1800 };

const engine = new EngineClient();

/* ------------------------------------------------------------------ sound */

let audioCtx = null;

function playSound(kind) {
  if (!state.sound) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const now = audioCtx.currentTime;
    const notes = {
      move: [[300, 0.05, 'triangle']],
      capture: [[190, 0.09, 'square'], [130, 0.09, 'square']],
      check: [[660, 0.08, 'triangle'], [880, 0.09, 'triangle']],
      mate: [[523, 0.12, 'triangle'], [659, 0.12, 'triangle'], [784, 0.22, 'triangle']],
      error: [[140, 0.1, 'sawtooth']],
    }[kind] || [[300, 0.05, 'triangle']];
    notes.forEach(([freq, dur, type], index) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = type;
      osc.frequency.value = freq;
      gain.gain.value = 0.0001;
      const start = now + index * 0.055;
      gain.gain.exponentialRampToValueAtTime(0.12, start + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + dur);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(start);
      osc.stop(start + dur + 0.02);
    });
  } catch { /* audio is a nice-to-have */ }
}

/* ------------------------------------------------------------------ helpers */

function setBanner(show, message = '') {
  const el = $('#banner');
  el.classList.toggle('show', show);
  if (message) el.querySelector('span').textContent = message;
}

function toast(title, body, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<b></b><span></span>`;
  el.querySelector('b').textContent = title;
  el.querySelector('span').textContent = body || '';
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(20px)';
    setTimeout(() => el.remove(), 320);
  }, kind === 'bad' ? 6000 : 3600);
}

const toSquare = (file, rank) => FILES[file] + (rank + 1);

function squareAt(x, y) {
  const rect = $('#board').getBoundingClientRect();
  const size = rect.width / 8;
  let col = Math.floor((x - rect.left) / size);
  let row = Math.floor((y - rect.top) / size);
  if (col < 0 || col > 7 || row < 0 || row > 7) return null;
  if (state.flipped) { col = 7 - col; row = 7 - row; }
  return toSquare(col, 7 - row);
}

function squareCentre(square) {
  const rect = $('#board').getBoundingClientRect();
  const size = rect.width / 8;
  let col = FILES.indexOf(square[0]);
  let row = 8 - parseInt(square[1], 10);
  if (state.flipped) { col = 7 - col; row = 7 - row; }
  return { x: rect.left + (col + 0.5) * size, y: rect.top + (row + 0.5) * size };
}

/** Centre of a square measured inside the board box (for the overlay layers). */
function squareCentreInBoard(square) {
  const rect = $('#board').getBoundingClientRect();
  const centre = squareCentre(square);
  return { x: centre.x - rect.left, y: centre.y - rect.top };
}

function moveSan(game, move) {
  try { return game.san(move); } catch { return move.from + move.to; }
}

const pieceColour = (symbol) => (symbol === symbol.toUpperCase() ? 'w' : 'b');

/* ------------------------------------------------------------------ board */

function buildBoard() {
  const board = $('#board');
  board.innerHTML = '';
  SQUARE_ELS.clear();

  const rows = [...Array(8).keys()];
  const cols = [...Array(8).keys()];
  const order = state.flipped
    ? { rows: [...rows].reverse(), cols: [...cols].reverse() }
    : { rows, cols };

  for (const row of order.rows) {
    for (const col of order.cols) {
      const rank = 7 - row;
      const square = toSquare(col, rank);
      const el = document.createElement('div');
      el.className = `sq ${(row + col) % 2 === 0 ? 'light' : 'dark'}`;
      el.dataset.square = square;
      el.style.gridRow = `${order.rows.indexOf(row) + 1}`;
      el.style.gridColumn = `${order.cols.indexOf(col) + 1}`;
      el.style.position = 'relative';

      if (rank === (state.flipped ? 7 : 0)) {
        const file = document.createElement('span');
        file.className = 'coord file';
        file.textContent = FILES[col];
        el.appendChild(file);
      }
      if (col === (state.flipped ? 7 : 0)) {
        const rankLabel = document.createElement('span');
        rankLabel.className = 'coord rank';
        rankLabel.textContent = String(rank + 1);
        el.appendChild(rankLabel);
      }
      board.appendChild(el);
      SQUARE_ELS.set(square, el);
    }
  }
}

function isOwnPiece(square) {
  const piece = state.game.get(square);
  return piece && piece.color === state.game.turn() && piece.color === state.humanColor
    && state.reviewPly === null && !state.thinking;
}

function renderHints() {
  const layer = $('#hints');
  layer.innerHTML = '';
  if (state.selected) {
    const { x, y } = squareCentreInBoard(state.selected);
    const sel = document.createElement('div');
    sel.className = 'hint-ring';
    sel.style.background = 'none';
    sel.style.left = `${x}px`;
    sel.style.top = `${y}px`;
    layer.appendChild(sel);
  }
  for (const target of state.legalTargets) {
    const { x, y } = squareCentreInBoard(target);
    const dot = document.createElement('div');
    const capture = state.game.get(target) || target === state.game.epSquare;
    dot.className = capture ? 'hint-ring' : 'hint-dot';
    dot.style.left = `${x}px`;
    dot.style.top = `${y}px`;
    layer.appendChild(dot);
  }
}

function drawArrow(from, to, tone = 'accent') {
  const svg = $('#arrows');
  svg.innerHTML = '';
  if (!from || !to || from === to) return;
  const rect = $('#board').getBoundingClientRect();
  svg.setAttribute('viewBox', `0 0 ${rect.width} ${rect.height}`);

  const a = squareCentreInBoard(from);
  const b = squareCentreInBoard(to);
  const ax = a.x, ay = a.y;
  const bx = b.x, by = b.y;
  const size = rect.width / 8;
  const angle = Math.atan2(by - ay, bx - ax);
  const head = size * 0.42;
  const shortX = bx - Math.cos(angle) * head * 0.85;
  const shortY = by - Math.sin(angle) * head * 0.85;

  const colour = tone === 'good' ? 'rgba(87, 217, 139, 0.82)'
    : tone === 'warn' ? 'rgba(246, 195, 82, 0.85)'
    : 'rgba(124, 196, 255, 0.85)';

  const ns = 'http://www.w3.org/2000/svg';
  const line = document.createElementNS(ns, 'line');
  line.setAttribute('x1', ax); line.setAttribute('y1', ay);
  line.setAttribute('x2', shortX); line.setAttribute('y2', shortY);
  line.setAttribute('stroke', colour);
  line.setAttribute('stroke-width', String(Math.max(6, size * 0.15)));
  line.setAttribute('stroke-linecap', 'round');

  const tip = document.createElementNS(ns, 'polygon');
  const wingAngle = 0.42;
  const w1x = bx - Math.cos(angle - wingAngle) * head;
  const w1y = by - Math.sin(angle - wingAngle) * head;
  const w2x = bx - Math.cos(angle + wingAngle) * head;
  const w2y = by - Math.sin(angle + wingAngle) * head;
  tip.setAttribute('points', `${bx},${by} ${w1x},${w1y} ${w2x},${w2y}`);
  tip.setAttribute('fill', colour);

  svg.append(line, tip);
}

function render() {
  const game = state.game;
  const viewGame = viewBoard();
  const view = state.reviewPly === null ? game : viewGame;

  for (const [square, el] of SQUARE_ELS) {
    const piece = view.get(square);
    const symbol = piece ? (piece.color === 'w' ? piece.type.toUpperCase() : piece.type) : null;
    if (el.dataset.symbol !== (symbol || '')) {
      el.querySelectorAll('.piece').forEach((node) => node.remove());
      if (symbol) {
        el.insertAdjacentHTML('afterbegin', pieceSvg(symbol));
      }
      el.dataset.symbol = symbol || '';
    }

    el.classList.toggle('own', symbol !== null && isOwnPiece(square));
    el.classList.toggle('legal', state.legalTargets.includes(square));

    const overlays = el.querySelectorAll('.overlay');
    overlays.forEach((node) => node.remove());

    const paint = (cls) => {
      const node = document.createElement('div');
      node.className = `overlay ${cls}`;
      node.style.position = 'absolute';
      node.style.inset = '0';
      el.appendChild(node);
    };

    if (state.lastMove && (square === state.lastMove.from || square === state.lastMove.to)) paint('last-move');
    if (state.selected === square) paint('sel');
  }

  // check highlight on the king of the side to move
  const checked = view.isCheck?.() ?? view.inCheck?.();
  if (checked) {
    const kingSquare = findKing(view, view.turn());
    if (kingSquare && SQUARE_ELS.has(kingSquare)) {
      const el = SQUARE_ELS.get(kingSquare);
      const node = document.createElement('div');
      node.className = 'overlay check';
      node.style.position = 'absolute';
      node.style.inset = '0';
      el.appendChild(node);
    }
  }

  renderHints();
  renderStatus(view);
  renderMoves();
}

function viewBoard() {
  const replay = new Chess();
  const moves = state.game.history({ verbose: true });
  for (let i = 0; i < Math.min(state.reviewPly ?? moves.length, moves.length); i += 1) {
    replay.move(moves[i].san);
  }
  return replay;
}

function findKing(game, colour) {
  for (const file of FILES) {
    for (let rank = 1; rank <= 8; rank += 1) {
      const piece = game.get(file + rank);
      if (piece && piece.type === 'k' && piece.color === colour) return file + rank;
    }
  }
  return null;
}

function renderStatus(view) {
  const game = state.game;
  const turnName = view.turn() === 'w' ? 'White' : 'Black';
  const over = game.isGameOver();

  let headline = `${turnName} to move`;
  let sub = '';

  if (state.reviewPly !== null) {
    headline = `Reviewing move ${state.reviewPly}`;
    sub = 'press → to return to the live position';
  } else if (over) {
    if (game.isCheckmate()) headline = `Checkmate — ${game.turn() === 'w' ? 'Black' : 'White'} wins`;
    else if (game.isStalemate()) headline = 'Draw — stalemate';
    else if (game.isThreefoldRepetition()) headline = 'Draw — threefold repetition';
    else if (game.isInsufficientMaterial()) headline = 'Draw — insufficient material';
    else if (game.isDraw()) headline = 'Draw — 50-move rule';
    sub = 'game over';
  } else if (view.isCheck?.() ?? view.inCheck?.()) {
    headline = `${turnName} is in check`;
    sub = 'find the best defence';
  } else if (state.thinking) {
    headline = `${turnName} to move`;
    sub = 'the bot is thinking…';
  }

  $('#headline-turn').textContent = headline;
  $('#headline-sub').textContent = sub;
  const turn = state.reviewPly === null ? game.turn() : view.turn();
  const topColor = state.flipped ? 'w' : 'b';
  const bottomColor = state.flipped ? 'b' : 'w';
  const topIsBot = topColor !== state.humanColor;
  const bottomIsBot = bottomColor !== state.humanColor;

  $('#player-top .who').textContent = topIsBot ? 'Bot' : 'You';
  $('#player-bottom .who').textContent = bottomIsBot ? 'Bot' : 'You';
  $('#top-colour').textContent = topColor === 'w' ? 'white' : 'black';
  $('#bottom-colour').textContent = bottomColor === 'w' ? 'white' : 'black';
  $('#player-top .avatar').classList.toggle('bot', topIsBot);
  $('#player-bottom .avatar').classList.toggle('bot', bottomIsBot);
  $('#player-top .avatar').textContent = topIsBot ? '♞' : '♔';
  $('#player-bottom .avatar').textContent = bottomIsBot ? '♞' : '♔';

  const live = state.reviewPly === null;
  $('#player-top').classList.toggle('active', live && turn === topColor);
  $('#player-bottom').classList.toggle('active', live && turn === bottomColor);

  const thinking = state.thinking || state.botBusy;
  $('#thinking-top').style.display = thinking && topIsBot && live ? 'inline-flex' : 'none';
  $('#thinking-bottom').style.display = thinking && bottomIsBot && live ? 'inline-flex' : 'none';

  const level = state.levels.find((item) => item.level === state.level);
  $('#top-level-label').textContent = topIsBot && level ? level.name : '';
  $('#bottom-level-label').textContent = bottomIsBot && level ? level.name : '';
}

function renderMoves() {
  const container = $('#moves');
  const history = state.game.history({ verbose: true });
  if (!history.length) {
    container.innerHTML = '<div class="empty">No moves yet — make your first move.</div>';
    return;
  }
  let html = '';
  for (let i = 0; i < history.length; i += 2) {
    const number = i / 2 + 1;
    html += `<div class="row"><span class="no">${number}.</span>`;
    for (const offset of [0, 1]) {
      const ply = i + offset;
      const move = history[ply];
      if (!move) { html += '<span></span>'; continue; }
      const grade = state.grades.get(ply);
      const tag = grade ? `<span class="tag grade-${grade.grade}" title="${grade.grade}${grade.loss_cp ? ` (-${grade.loss_cp}cp)` : ''}${grade.note ? ` — ${grade.note}` : ''}">${tagFor(grade.grade)}</span>` : '';
      const active = state.reviewPly === ply + 1 ? ' active' : '';
      html += `<button class="mv${active}" data-ply="${ply + 1}"><span>${move.san}</span>${tag}</button>`;
    }
    html += '</div>';
  }
  container.innerHTML = html;
  container.querySelectorAll('button.mv').forEach((button) => {
    button.addEventListener('click', () => setReview(parseInt(button.dataset.ply, 10)));
  });
  const active = container.querySelector('.mv.active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

function tagFor(grade) {
  return { Brilliant: '!!', 'Best move': '!', Good: '·', Inaccuracy: '?!', Mistake: '?', Blunder: '??' }[grade] || '';
}

function renderEnginePanel(source = '') {
  state.pvFullmove = state.pvFullmove || parseInt(state.game.fen().split(' ')[5], 10) || 1;
  $('#stat-depth').textContent = state.stats.depth ? `${state.stats.depth}/${state.stats.seldepth}` : '—';
  $('#stat-nodes').textContent = state.stats.nodes ? formatNumber(state.stats.nodes) : '—';
  $('#stat-nps').textContent = state.stats.nps ? formatNumber(state.stats.nps) : '—';
  $('#stat-time').textContent = state.stats.time ? `${(state.stats.time / 1000).toFixed(2)}s` : '—';

  const pvEl = $('#pv');
  if (state.pv.length) {
    const startTurn = state.pvStartTurn || state.game.turn();
    const fullmove = state.pvFullmove || parseInt(state.game.fen().split(' ')[5], 10) || 1;
    let html = '';
    state.pv.forEach((san, index) => {
      const whiteMove = startTurn === 'w' ? index % 2 === 0 : index % 2 === 1;
      if (whiteMove) {
        html += `<span class="num">${fullmove + Math.floor((index + (startTurn === 'w' ? 0 : 1)) / 2)}.</span> `;
      } else if (index === 0) {
        html += `<span class="num">${fullmove}…</span> `;
      }
      html += `<span class="${state.pvBook ? 'book' : ''}">${san}</span> `;
    });
    if (state.opening) html += `<div class="hint-text">Opening: ${state.opening.eco} ${state.opening.name}</div>`;
    pvEl.innerHTML = html;
  } else {
    pvEl.innerHTML = `<span class="empty">${source || 'The engine line will appear here.'}</span>`;
  }
  $('#pv-badge').textContent = state.pvBook ? 'book' : '';
}

function formatNumber(value) {
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
  return String(value);
}

function updateEvalBar() {
  const cp = Math.max(-1000, Math.min(1000, state.evalCp));
  const percent = 50 + (cp / 20);  // 1 pawn = 5%
  const fill = $('#eval-fill');
  fill.style.height = `${percent}%`;
  fill.classList.toggle('negative', state.evalCp < 0);

  const label = $('#eval-value');
  let text;
  if (state.mateIn !== null && state.mateIn !== undefined && Math.abs(state.mateIn) <= 30) {
    text = `${state.mateIn > 0 ? 'M' : '-M'}${Math.abs(state.mateIn)}`;
    label.classList.add('big');
  } else {
    const pawns = state.evalCp / 100;
    text = `${pawns > 0 ? '+' : ''}${pawns.toFixed(2)}`;
    label.classList.remove('big');
  }
  label.textContent = text;
  label.classList.toggle('neg', state.evalCp < -30);
}

/* ------------------------------------------------------------------ engine calls */

function applyEngineInfo(msg) {
  state.stats = {
    depth: msg.depth || 0,
    seldepth: msg.seldepth || 0,
    nodes: msg.nodes || 0,
    nps: msg.nps || 0,
    time: msg.time_ms || 0,
  };
  state.pv = msg.pv_san || [];
  state.pvBook = !!msg.book;
  if (msg.opening) state.opening = msg.opening;
  state.pvStartTurn = msg.white_to_move ? 'w' : 'b';
  state.pvFullmove = msg.fullmove || state.pvFullmove || 1;
  if (msg.score_white !== undefined) {
    state.evalCp = msg.score_white;
    state.mateIn = msg.mate_in ?? null;
  }
  renderEnginePanel();
  updateEvalBar();
}

async function requestBotMove() {
  if (!state.engineOnline) {
    toast('Engine offline', 'The bot cannot move right now.', 'bad');
    return null;
  }
  state.thinking = true;
  renderStatus(viewBoard());
  const fen = state.game.fen();
  try {
    const result = await engine.send(
      { type: 'move', fen, level: state.level },
      { onInfo: applyEngineInfo, timeout: 45000 },
    );
    state.thinking = false;
    if (fen !== state.game.fen()) return null;   // position changed while thinking
    applyEngineInfo(result);
    return result;
  } catch (err) {
    state.thinking = false;
    renderStatus(viewBoard());
    toast('Bot error', err.message, 'bad');
    return null;
  }
}

async function requestEval(fen, movetime = 420) {
  if (!state.engineOnline || !state.autoEval) return;
  try {
    const result = await engine.send({ type: 'eval', fen, movetime_ms: movetime, level: state.level }, { timeout: 20000 });
    if (fen !== state.game.fen()) return;
    applyEngineInfo(result);
  } catch { /* non-fatal */ }
}

async function requestGrade(ply, fenBefore, uciMove) {
  if (!state.engineOnline || !state.coach) return;
  try {
    const result = await engine.send({ type: 'grade', fen: fenBefore, move: uciMove, movetime_ms: 380 }, { timeout: 25000 });
    state.grades.set(ply, result.report);
    renderMoves();
    const report = result.report;
    if (report.grade === 'Blunder' || report.grade === 'Mistake' || report.grade === 'Best move' || report.grade === 'Brilliant') {
      const kind = report.grade === 'Blunder' ? 'bad' : report.grade === 'Mistake' ? 'warn' : 'good';
      toast(`${report.grade}`, `${report.san}${report.best_san && report.grade !== 'Best move' ? ` — best was ${report.best_san}` : ''}${report.note ? `. ${report.note}` : ''}`, kind);
    }
  } catch { /* grading is best effort */ }
}

/* ------------------------------------------------------------------ moving */

function pushMove(move, { byHuman = false } = {}) {
  const fenBefore = state.game.fen();
  const ply = state.game.history().length;
  const played = state.game.move(move);
  if (!played) return false;

  state.lastMove = { from: played.from, to: played.to };
  state.selected = null;
  state.legalTargets = [];
  drawArrow(null, null);
  setReview(null, { silent: true });

  const capture = !!played.captured;
  if (state.game.isCheckmate()) playSound('mate');
  else if (state.game.isCheck?.() ?? state.game.inCheck?.()) playSound('check');
  else playSound(capture ? 'capture' : 'move');

  render();
  if (byHuman && state.coach) requestGrade(ply, fenBefore, `${played.from}${played.to}${played.promotion || ''}`);

  if (state.game.isGameOver()) {
    finishGame();
  } else {
    maybeBotTurn();
  }
  return true;
}

function maybeBotTurn() {
  const turn = state.game.turn();
  if (state.reviewPly !== null) return;
  if (state.game.isGameOver()) return;
  if (turn === state.humanColor) {
    requestEval(state.game.fen());
    return;
  }
  if (state.botBusy) return;
  state.botBusy = true;
  requestBotMove().then(async (result) => {
    state.botBusy = false;
    if (!result || !result.best) return;
    const move = state.game.move({ from: result.best.slice(0, 2), to: result.best.slice(2, 4), promotion: result.best[4] || 'q' });
    if (!move) return;
    state.lastMove = { from: move.from, to: move.to };
    state.selected = null;
    state.legalTargets = [];
    if (state.game.isCheckmate()) playSound('mate');
    else if (state.game.isCheck?.() ?? state.game.inCheck?.()) playSound('check');
    else playSound(move.captured ? 'capture' : 'move');
    render();
    if (state.game.isGameOver()) finishGame();
    else {
      await requestEval(state.game.fen());
    }
  });
}

function finishGame() {
  const game = state.game;
  let title = 'Game over';
  let detail = '';
  if (game.isCheckmate()) {
    const winner = game.turn() === 'w' ? 'Black' : 'White';
    title = `Checkmate`;
    detail = `${winner} wins${winner === (state.humanColor === 'w' ? 'White' : 'Black') ? ' — that’s you!' : '. Want a rematch?'}`;
  } else if (game.isStalemate()) { title = 'Stalemate'; detail = 'Nobody wins this one.'; }
  else if (game.isThreefoldRepetition()) { title = 'Draw'; detail = 'Threefold repetition.'; }
  else if (game.isInsufficientMaterial()) { title = 'Draw'; detail = 'Not enough material to mate.'; }
  else { title = 'Draw'; detail = 'Fifty-move rule.'; }

  $('#result-title').textContent = title;
  $('#result-detail').textContent = detail;
  $('#result').classList.remove('hidden');
  $('#player-top .thinking').style.display = 'none';
}

function humanMove(from, to, promotion) {
  const game = state.game;
  const legal = game.moves({ square: from, verbose: true });
  const candidate = legal.find((move) => move.to === to);
  if (!candidate) return false;

  if (candidate.promotion) {
    showPromotion(to, game.turn(), (piece) => {
      pushMove({ from, to, promotion: piece }, { byHuman: true });
    });
    return true;
  }
  return pushMove({ from, to }, { byHuman: true });
}

function selectSquare(square) {
  const game = state.game;
  const piece = game.get(square);
  if (piece && piece.color === game.turn()) {
    state.selected = square;
    state.legalTargets = game.moves({ square, verbose: true }).map((move) => move.to);
    render();
    return true;
  }
  return false;
}

function onBoardPointerDown(event) {
  if (state.reviewPly !== null) { setReview(null); return; }
  if (state.game.isGameOver() || state.thinking || state.botBusy) return;
  if (state.game.turn() !== state.humanColor) return;

  const square = squareAt(event.clientX, event.clientY);
  if (!square) return;
  const game = state.game;

  // 1. complete a move onto a highlighted target square
  if (state.selected && state.legalTargets.includes(square)) {
    humanMove(state.selected, square);
    return;
  }

  const piece = game.get(square);
  // 2. empty or opponent square: drop the selection
  if (!piece || piece.color !== state.humanColor) {
    clearSelection();
    return;
  }
  // 3. own piece: toggle off if it is already selected, else select and drag
  if (state.selected === square) { clearSelection(); return; }
  selectSquare(square);
  startDrag(square, event);
}

function clearSelection() {
  state.selected = null;
  state.legalTargets = [];
  render();
}

function startDrag(square, event) {
  const el = SQUARE_ELS.get(square);
  const svg = el.querySelector('.piece');
  if (!svg) return;

  const ghost = svg.cloneNode(true);
  const box = el.getBoundingClientRect();
  Object.assign(ghost.style, {
    position: 'fixed',
    pointerEvents: 'none',
    zIndex: '40',
    width: `${box.width * 0.95}px`,
    height: `${box.width * 0.95}px`,
    left: `${event.clientX}px`,
    top: `${event.clientY}px`,
    transform: 'translate(-50%, -50%)',
  });
  document.body.appendChild(ghost);
  el.classList.add('dragging');
  const origin = { x: event.clientX, y: event.clientY };
  drag = { square, ghost, moved: false };

  const onMove = (moveEvent) => {
    if (!drag) return;
    if (Math.hypot(moveEvent.clientX - origin.x, moveEvent.clientY - origin.y) > 4) drag.moved = true;
    drag.ghost.style.left = `${moveEvent.clientX}px`;
    drag.ghost.style.top = `${moveEvent.clientY}px`;
  };

  const onUp = (upEvent) => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
    const dragged = drag;
    drag = null;
    if (!dragged) return;
    dragged.ghost.remove();
    SQUARE_ELS.get(dragged.square)?.classList.remove('dragging');
    if (!dragged.moved) { render(); return; }   // a click: keep the selection
    const target = squareAt(upEvent.clientX, upEvent.clientY);
    if (!target || target === dragged.square) return;
    if (state.legalTargets.includes(target)) humanMove(dragged.square, target);
    else clearSelection();
  };

  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);
}

/* ------------------------------------------------------------------ promotion */

function showPromotion(square, colour, callback) {
  const picker = $('#promotion');
  const pieces = ['q', 'r', 'b', 'n'];
  picker.innerHTML = '';
  for (const type of pieces) {
    const button = document.createElement('button');
    button.title = { q: 'Queen', r: 'Rook', b: 'Bishop', n: 'Knight' }[type];
    button.innerHTML = pieceSvg(colour === 'w' ? type.toUpperCase() : type, 42);
    button.addEventListener('click', () => {
      picker.classList.add('hidden');
      callback(type);
    });
    picker.appendChild(button);
  }
  const centre = squareCentre(square);
  picker.classList.remove('hidden');
  picker.style.left = `${centre.x - 116}px`;
  picker.style.top = `${centre.y - 32}px`;
  picker.style.position = 'fixed';
}

/* ------------------------------------------------------------------ game control */

function newGame({ colour = 'w', level = state.level, random = false } = {}) {
  state.game = new Chess();
  state.humanColor = random ? (Math.random() < 0.5 ? 'w' : 'b') : colour;
  state.flipped = state.humanColor === 'b';
  state.selected = null;
  state.legalTargets = [];
  state.lastMove = null;
  state.grades.clear();
  state.reviewPly = null;
  state.thinking = false;
  state.botBusy = false;
  state.level = level;
  state.opening = null;
  state.pv = [];
  state.evalCp = 0;
  state.mateIn = null;
  state.pvBook = false;
  state.pvFullmove = 1;
  state.stats = { depth: 0, seldepth: 0, nodes: 0, nps: 0, time: 0 };
  state.autoEval = true;

  $('#result').classList.add('hidden');
  drawArrow(null, null);
  buildBoard();
  render();
  renderEnginePanel('Waiting for the first move.');
  updateEvalBar();
  syncLevelUI();

  if (state.humanColor === 'b') {
    maybeBotTurn();
  } else {
    requestEval(state.game.fen(), 300);
  }
}

function undo() {
  if (state.thinking) return;
  const game = state.game;
  if (!game.history().length) return;
  setReview(null, { silent: true });
  let plies = 0;
  // take back until it is the human's turn again (max 2 plies)
  while (game.history().length && plies < 2) {
    game.undo();
    plies += 1;
    if (game.turn() === state.humanColor) break;
  }
  state.grades.clear();
  const history = game.history({ verbose: true });
  state.lastMove = history.length ? { from: history[history.length - 1].from, to: history[history.length - 1].to } : null;
  state.selected = null;
  state.legalTargets = [];
  state.botBusy = false;
  drawArrow(null, null);
  $('#result').classList.add('hidden');
  render();
  requestEval(game.fen(), 350);
}

async function hint() {
  if (!state.engineOnline) { toast('Engine offline', 'No hints available.', 'bad'); return; }
  if (state.game.isGameOver() || state.thinking) return;
  const fen = state.game.fen();
  try {
    const result = await engine.send(
      { type: 'hint', fen, level: state.level },
      { onInfo: applyEngineInfo, timeout: 30000 },
    );
    if (fen !== state.game.fen() || !result.best) return;
    applyEngineInfo(result);
    drawArrow(result.best.slice(0, 2), result.best.slice(2, 4), 'good');
    const note = result.book ? 'Straight from the opening book.' : `Evaluation ${(result.score_white / 100).toFixed(2)} for White.`;
    toast(`Hint: ${result.best_san}`, note, 'good');
    setTimeout(() => { if (state.game.fen() === fen) drawArrow(null, null); }, 4200);
  } catch (err) {
    toast('Hint failed', err.message, 'bad');
  }
}

function setReview(ply, { silent = false } = {}) {
  if (ply !== null && ply > state.game.history().length) return;
  state.reviewPly = ply;
  if (!silent) {
    state.selected = null;
    state.legalTargets = [];
  }
  render();
}

function syncLevelUI() {
  $$('.level-grid .chip').forEach((chip) => {
    chip.classList.toggle('on', parseInt(chip.dataset.level, 10) === state.level);
  });
  const info = state.levels.find((level) => level.level === state.level);
  if (info) {
    $('#level-blurb').innerHTML = `<b>${info.name}</b> · ~${info.estimated_elo} Elo — ${info.blurb}`;
  }
}

/* ------------------------------------------------------------------ wire-up */

function addListeners() {
  $('#board').addEventListener('pointerdown', onBoardPointerDown);
  $('#board').addEventListener('contextmenu', (event) => event.preventDefault());

  $('#new-game').addEventListener('click', () => newGame({ colour: state.humanColor }));
  $('#swap-sides').addEventListener('click', () => newGame({ colour: state.humanColor === 'w' ? 'b' : 'w' }));
  $('#undo').addEventListener('click', undo);
  $('#hint').addEventListener('click', hint);
  $('#flip').addEventListener('click', () => {
    state.flipped = !state.flipped;
    buildBoard();
    render();
  });
  $('#result-rematch').addEventListener('click', () => newGame({ colour: state.humanColor }));
  $('#result-review').addEventListener('click', () => {
    $('#result').classList.add('hidden');
    setReview(Math.max(0, state.game.history().length - 6));
  });
  $('#resign').addEventListener('click', () => {
    if (state.game.isGameOver()) return;
    finishGameCustom('Resignation', `${state.humanColor === 'w' ? 'Black' : 'White'} wins — you resigned.`);
  });
  $('#copy-pgn').addEventListener('click', async () => {
    const pgn = state.game.pgn() || '(empty)';
    await copy(pgn, 'PGN copied to the clipboard.');
  });
  $('#copy-fen').addEventListener('click', async () => {
    await copy(state.game.fen(), 'FEN copied to the clipboard.');
  });
  $('#load-fen').addEventListener('click', () => {
    const value = $('#fen-input').value.trim();
    if (!value) return;
    try {
      const game = new Chess();
      game.load(value);
      state.game = game;
      state.reviewPly = null;
      state.grades.clear();
      state.selected = null;
      state.legalTargets = [];
      state.opening = null;
      const history = game.history({ verbose: true });
      state.lastMove = history.length ? { from: history[history.length - 1].from, to: history[history.length - 1].to } : null;
      drawArrow(null, null);
      $('#result').classList.add('hidden');
      render();
      toast('Position loaded', 'Good luck.', 'good');
      if (game.turn() !== state.humanColor) maybeBotTurn();
      else requestEval(game.fen(), 350);
    } catch (err) {
      toast('Invalid FEN', 'Check the position string.', 'bad');
    }
  });

  $$('.level-grid .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      state.level = parseInt(chip.dataset.level, 10);
      syncLevelUI();
      toast('Level set', `${state.levels.find((l) => l.level === state.level)?.name} will answer with ~${state.levels.find((l) => l.level === state.level)?.estimated_elo} Elo strength.`);
      if (state.game.turn() !== state.humanColor && !state.game.isGameOver()) {
        state.botBusy = false;
        maybeBotTurn();
      }
    });
  });

  $('#toggle-coach').addEventListener('click', (event) => {
    state.coach = !state.coach;
    event.currentTarget.classList.toggle('on', state.coach);
    event.currentTarget.textContent = state.coach ? 'Coach on' : 'Coach off';
  });
  $('#toggle-sound').addEventListener('click', (event) => {
    state.sound = !state.sound;
    event.currentTarget.classList.toggle('on', state.sound);
    event.currentTarget.textContent = state.sound ? 'Sound on' : 'Sound off';
  });
  $('#toggle-eval').addEventListener('click', (event) => {
    state.autoEval = !state.autoEval;
    event.currentTarget.classList.toggle('on', state.autoEval);
    event.currentTarget.textContent = state.autoEval ? 'Live eval on' : 'Live eval off';
    if (state.autoEval) requestEval(state.game.fen(), 350);
  });
  $('#toggle-theme').addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.dataset.theme === 'tournament' ? 'midnight' : 'tournament';
    root.dataset.theme = next;
    $('#toggle-theme').textContent = next === 'tournament' ? 'Midnight' : 'Tournament';
  });

  document.addEventListener('keydown', (event) => {
    if (event.target instanceof HTMLInputElement) return;
    const key = event.key.toLowerCase();
    if (key === 'n') newGame({ colour: state.humanColor });
    else if (key === 'u') undo();
    else if (key === 'f') { state.flipped = !state.flipped; buildBoard(); render(); }
    else if (key === 'h') hint();
    else if (key === 'arrowright' || key === 'arrowleft') {
      const total = state.game.history().length;
      const current = state.reviewPly ?? total;
      const next = Math.max(0, Math.min(total, current + (key === 'arrowright' ? 1 : -1)));
      setReview(next === total ? null : next);
      event.preventDefault();
    }
  });

  window.addEventListener('engine-online', () => {
    state.engineOnline = true;
    setBanner(false);
    if (state.game.history().length === 0 && state.humanColor === 'b') maybeBotTurn();
  });
  window.addEventListener('engine-offline', () => {
    state.engineOnline = false;
    setBanner(true, 'Lost the connection to the engine — retrying…');
  });
}

function finishGameCustom(title, detail) {
  $('#result-title').textContent = title;
  $('#result-detail').textContent = detail;
  $('#result').classList.remove('hidden');
}

async function copy(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied', message, 'good');
  } catch {
    toast('Copy failed', 'Your browser blocked clipboard access.', 'warn');
  }
}

async function boot() {
  addListeners();
  buildBoard();
  render();
  updateEvalBar();

  try {
    const response = await fetch('/api/levels');
    const data = await response.json();
    state.levels = data.levels;
    state.level = data.default || 4;
  } catch {
    state.levels = [{ level: 4, name: 'Rook', estimated_elo: 1250, blurb: 'Levels unavailable.' }];
  }

  const grid = $('#level-grid');
  grid.innerHTML = '';
  for (const level of state.levels) {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.dataset.level = String(level.level);
    chip.innerHTML = `<b>${level.name}</b><i>${level.estimated_elo}</i>`;
    grid.appendChild(chip);
  }
  syncLevelUI();

  newGame({ colour: 'w', level: state.level });
}

boot();
