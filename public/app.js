'use strict';

const $ = (id) => document.getElementById(id);
const SCREENS = ['loading', 'auth', 'lobby', 'game'];
const MARKS = {
  X: '<svg viewBox="0 0 100 100" aria-hidden="true"><path class="x" d="M24 24 76 76M76 24 24 76"/></svg>',
  O: '<svg viewBox="0 0 100 100" aria-hidden="true"><circle class="o" cx="50" cy="50" r="28"/></svg>',
};

let me = null;          // logged-in user
let game = null;        // latest game state from the server
let pollId = 0;         // bumping this stops the current polling loop
let authMode = 'login';
let moving = false;

// ---------------------------------------------------------------- helpers

function show(name) {
  for (const s of SCREENS) $('screen-' + s).classList.toggle('active', s === name);
  window.scrollTo(0, 0);
}

function showError(el, message) {
  el.textContent = message || '';
  el.hidden = !message;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw Object.assign(new Error("Can't reach the server. Check your connection."), { status: 0 });
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || 'Something went wrong.'), { status: res.status });
  return data;
}

// ---------------------------------------------------------------- start-up

async function boot() {
  try {
    const data = await api('/api/me');
    if (!data.user) {
      me = null;
      show('auth');
      return;
    }
    me = data.user;
    if (data.activeGame) openGame(data.activeGame);
    else showLobby();
  } catch (err) {
    show('auth');
    showError($('auth-error'), err.message);
  }
}

// ---------------------------------------------------------------- log in / sign up

function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.mode === mode));
  $('auth-submit').textContent = mode === 'login' ? 'Log in' : 'Create account';
  $('auth-password').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  $('auth-hint').hidden = mode === 'login';
  showError($('auth-error'));
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => setAuthMode(tab.dataset.mode));
});

$('auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('auth-username').value.trim();
  const password = $('auth-password').value;
  if (!username || !password) return showError($('auth-error'), 'Enter a username and password.');
  const button = $('auth-submit');
  button.disabled = true;
  showError($('auth-error'));
  try {
    await api('/api/' + (authMode === 'login' ? 'login' : 'register'), { username, password });
    $('auth-password').value = '';
    await boot();
  } catch (err) {
    showError($('auth-error'), err.message);
  } finally {
    button.disabled = false;
  }
});

$('logout').addEventListener('click', async () => {
  stopPolling();
  await api('/api/logout', {}).catch(() => {});
  me = null;
  setAuthMode('login');
  show('auth');
});

// ---------------------------------------------------------------- lobby

async function showLobby(message) {
  stopPolling();
  game = null;
  showError($('lobby-error'), message);
  $('join-code').value = '';
  try {
    const data = await api('/api/me');
    if (!data.user) return show('auth');
    me = data.user;
  } catch { /* show what we have */ }
  $('lobby-name').textContent = me.name;
  const stats = me.stats || { wins: 0, losses: 0, draws: 0 };
  $('stat-wins').textContent = stats.wins;
  $('stat-losses').textContent = stats.losses;
  $('stat-draws').textContent = stats.draws;
  show('lobby');
}

async function startGame(path, body = {}) {
  showError($('lobby-error'));
  try {
    enterGame(await api(path, body));
  } catch (err) {
    showError($('lobby-error'), err.message);
  }
}

$('quick').addEventListener('click', () => startGame('/api/games/quick'));
$('create').addEventListener('click', () => startGame('/api/games'));
$('join-code').addEventListener('input', (e) => {
  e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
});
$('join-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const code = $('join-code').value.trim();
  if (code.length !== 4) return showError($('lobby-error'), 'Enter the 4-character code.');
  startGame('/api/games/join', { code });
});

// ---------------------------------------------------------------- game

const board = $('board');
const cells = [];
for (let i = 0; i < 9; i++) {
  const cell = document.createElement('button');
  cell.type = 'button';
  cell.className = 'cell';
  cell.addEventListener('click', () => play(i));
  board.appendChild(cell);
  cells.push(cell);
}

function enterGame(g) {
  render(g);
  show('game');
  startPolling(g.id, g.version);
}

function openGame(id) {
  game = null;
  show('game');
  startPolling(id, -1);
}

function stopPolling() {
  pollId++;
}

async function startPolling(id, version) {
  const myPoll = ++pollId;
  let v = version;
  while (myPoll === pollId) {
    try {
      const g = await api(`/api/games/${id}?v=${v}`);
      if (myPoll !== pollId) return;
      v = g.version;
      if (g.status === 'closed') return showLobby('That game was closed.');
      render(g);
    } catch (err) {
      if (myPoll !== pollId) return;
      if (err.status === 401) { stopPolling(); return show('auth'); }
      if (err.status === 404) return showLobby('That game has ended.');
      $('presence').textContent = 'Reconnecting…';
      await sleep(2000);
    }
  }
}

function render(g) {
  game = g;
  const you = g.you;
  const opp = you === 'X' ? 'O' : 'X';
  const oppName = g.players[opp] || 'Opponent';
  const waiting = g.status === 'waiting';

  for (const s of ['X', 'O']) {
    const el = $('player-' + s);
    const name = g.players[s];
    el.querySelector('.pname').textContent = name ? name + (s === you ? ' (you)' : '') : 'Waiting…';
    el.classList.toggle('turn', g.status === 'playing' && g.turn === s);
    el.classList.toggle('winner', g.status === 'finished' && g.winner === s);
  }

  // Waiting for a second player
  $('waiting').hidden = !waiting;
  if (waiting) {
    $('code-block').hidden = g.public;
    $('game-code').textContent = g.code;
    $('share-code').hidden = !(window.AndroidApp || navigator.share || navigator.clipboard);
    $('waiting-text').textContent = g.public ? 'Looking for an opponent…' : 'Waiting for your friend to join…';
  }
  board.hidden = waiting;

  // Board
  const myTurn = g.status === 'playing' && g.turn === you;
  board.classList.toggle('my-turn', myTurn);
  cells.forEach((cell, i) => {
    const mark = g.board[i] || '';
    if (cell.dataset.mark !== mark) {
      cell.dataset.mark = mark;
      cell.innerHTML = mark ? MARKS[mark] : '';
    }
    cell.disabled = !myTurn || !!mark;
    cell.classList.toggle('win', !!g.line && g.line.includes(i));
    const row = Math.floor(i / 3) + 1, col = (i % 3) + 1;
    cell.setAttribute('aria-label', `Row ${row}, column ${col}: ${mark || 'empty'}`);
  });

  // Status line
  let status = '';
  if (g.status === 'playing') status = myTurn ? 'Your turn' : `${oppName}'s turn`;
  else if (g.status === 'finished') {
    if (g.winner === 'draw') status = "It's a draw!";
    else if (g.winner === you) status = 'You win! 🎉';
    else status = `${oppName} wins`;
  }
  $('status').textContent = status;
  $('status').hidden = waiting;

  // Presence line
  let presence = '';
  if (g.leftBy === opp) presence = `${oppName} left the game.`;
  else if (g.status === 'playing' && !g.opponentOnline) presence = `${oppName} seems to be offline…`;
  else if (g.status === 'finished' && g.rematchOpponent) presence = `${oppName} wants a rematch!`;
  else if (g.round > 1 && g.status === 'playing') presence = `Round ${g.round}`;
  $('presence').textContent = presence;

  // Buttons
  const rematch = $('rematch');
  rematch.hidden = !(g.status === 'finished' && !g.leftBy);
  rematch.disabled = g.rematchYou;
  rematch.textContent = g.rematchYou ? 'Waiting for opponent…' : 'Play again';
  $('leave').textContent = waiting ? 'Cancel' : g.status === 'playing' ? 'Give up' : 'Back to lobby';
}

async function play(i) {
  if (!game || moving || cells[i].disabled) return;
  moving = true;
  try {
    render(await api(`/api/games/${game.id}/move`, { cell: i }));
  } catch (err) {
    $('presence').textContent = err.message;
  } finally {
    moving = false;
  }
}

$('rematch').addEventListener('click', async () => {
  if (!game) return;
  try {
    render(await api(`/api/games/${game.id}/rematch`, {}));
  } catch (err) {
    $('presence').textContent = err.message;
  }
});

$('leave').addEventListener('click', async () => {
  if (!game) return showLobby();
  if (game.status === 'playing' && !confirm('Give up this game? It will count as a loss.')) return;
  stopPolling();
  await api(`/api/games/${game.id}/leave`, {}).catch(() => {});
  showLobby();
});

$('share-code').addEventListener('click', async () => {
  if (!game) return;
  const text = `Join my Tic-Tac-Toe game with code ${game.code}`;
  try {
    if (window.AndroidApp) window.AndroidApp.share(text);   // inside the Android app
    else if (navigator.share) await navigator.share({ text });
    else {
      await navigator.clipboard.writeText(game.code);
      $('share-code').textContent = 'Copied!';
      setTimeout(() => { $('share-code').textContent = 'Share code'; }, 1500);
    }
  } catch { /* user cancelled */ }
});

boot();
