'use strict';

const ids = { 'Локально': 'localActions', 'Проверки': 'checkActions', 'Сервер': 'serverActions' };
const isFile = location.protocol === 'file:';
const modal = document.getElementById('confirmModal');
let pendingAction = null;
let toastTimer = null;
let zoomFactor = Number(localStorage.getItem('grosh-zoom') || 1);

function toast(message) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2600);
}

function applyZoom() {
  document.body.style.zoom = String(zoomFactor);
  document.getElementById('zoomValue').textContent = `${Math.round(zoomFactor * 100)}%`;
  localStorage.setItem('grosh-zoom', String(zoomFactor));
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); return; } catch { /* fallback below */ }
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.append(input);
  input.select();
  const copied = document.execCommand('copy');
  input.remove();
  if (!copied) throw new Error('Не удалось скопировать');
}

async function post(endpoint, id) {
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Grosh-Panel': '1' },
    body: JSON.stringify({ id }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Ошибка');
  return body;
}

function makeCard(action) {
  const card = document.createElement('article');
  card.className = 'action-card';
  if (action.confirm) card.dataset.kind = 'danger';
  const title = document.createElement('h3');
  title.textContent = action.title;
  const detail = document.createElement('p');
  detail.textContent = action.detail;
  const command = document.createElement('div');
  command.className = 'command';
  const prompt = document.createElement('span');
  prompt.className = 'prompt';
  prompt.textContent = '›';
  const code = document.createElement('code');
  code.textContent = action.command;
  command.append(prompt, code);
  const controls = document.createElement('div');
  controls.className = 'card-actions';
  const run = document.createElement('button');
  run.type = 'button';
  run.className = 'run-button';
  run.textContent = action.label;
  run.disabled = isFile;
  run.title = isFile ? 'Откройте страницу через Пульт Грош.cmd' : '';
  run.addEventListener('click', () => requestRun(action));
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'copy-button';
  copy.textContent = 'Копировать';
  copy.addEventListener('click', async () => {
    try { await copyText(action.command); toast('Команда скопирована'); }
    catch { toast('Не удалось скопировать команду'); }
  });
  controls.append(run, copy);
  card.append(title, detail, command, controls);
  return card;
}

function requestRun(action) {
  if (!action.confirm) { run(action.id); return; }
  pendingAction = action.id;
  document.getElementById('modalTitle').textContent = action.title;
  document.getElementById('modalText').textContent = action.confirm;
  modal.hidden = false;
  document.getElementById('cancelModal').focus();
}

async function run(id) {
  try { await post('/api/run', id); toast('Консоль открыта'); setTimeout(refreshStatus, 2000); }
  catch (error) { toast(error.message || 'Не удалось открыть консоль'); }
}

function closeModal() { modal.hidden = true; pendingAction = null; }
document.getElementById('cancelModal').addEventListener('click', closeModal);
document.getElementById('acceptModal').addEventListener('click', () => {
  const id = pendingAction;
  closeModal();
  if (id) run(id);
});
modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(); });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !modal.hidden) closeModal(); });

async function refreshStatus() {
  if (isFile) return;
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error('Нет связи');
    const status = await response.json();
    document.getElementById('apiStatus').textContent = status.alive ? 'Работает · порт 8081' : 'Остановлено';
    document.getElementById('branchStatus').textContent = status.branch;
    document.getElementById('changesStatus').textContent = status.changes === null ? 'Недоступно' : status.changes === 0 ? 'Нет' : String(status.changes);
  } catch {
    document.getElementById('apiStatus').textContent = 'Нет связи с пультом';
  }
}

document.getElementById('refresh').addEventListener('click', refreshStatus);
document.querySelectorAll('[data-open]').forEach((button) => button.addEventListener('click', async () => {
  if (isFile) { toast('Откройте страницу через Пульт Грош.cmd'); return; }
  try { await post('/api/open', button.dataset.open); }
  catch (error) { toast(error.message || 'Не удалось открыть'); }
}));
document.querySelectorAll('[data-zoom]').forEach((button) => button.addEventListener('click', () => {
  zoomFactor = Math.min(1.6, Math.max(0.7, Math.round((zoomFactor + Number(button.dataset.zoom) * 0.1) * 10) / 10));
  applyZoom();
}));

window.GROSH_ACTIONS.forEach((action) => document.getElementById(ids[action.group]).append(makeCard(action)));
if (isFile) {
  document.getElementById('fileNotice').hidden = false;
  document.getElementById('apiStatus').textContent = 'Откройте через пульт';
  document.getElementById('branchStatus').textContent = '—';
  document.getElementById('changesStatus').textContent = '—';
}
applyZoom();
refreshStatus();
if (!isFile) setInterval(refreshStatus, 15000);
