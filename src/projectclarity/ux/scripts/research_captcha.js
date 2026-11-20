/* Let the human answer a search CAPTCHA without opening a separate browser session.
 * Polling shows cached tiles only. Explicit buttons submit the human's choices,
 * skip this provider or cancel research; no image is interpreted automatically.
 */
'use strict';
let captchaState = null;
let captchaSubmitting = false;
let captchaTimer;
let captchaLastActivity = 0;

/** Display the current one-use puzzle while preserving selections during polling.
 * Args: state — public session snapshot with optional safe CAPTCHA metadata.
 * Returns: nothing; opens/closes a modal, uses same-origin image URLs, and polls
 * while searches are active so renewed puzzles can replace the previous one.
 */
function renderCaptcha(state = {}) {
  captchaState = state;
  clearTimeout(captchaTimer);
  let dialog = document.getElementById('captcha-dialog');
  const challenge = state.captcha;
  if (!challenge) { if (dialog?.open) dialog.close(); }
  else {
    if (!dialog) {
      dialog = document.createElement('dialog'); dialog.id = 'captcha-dialog';
      dialog.setAttribute('aria-labelledby', 'captcha-heading');
      dialog.setAttribute('aria-describedby', 'captcha-instruction');
      const title = document.createElement('h2'); title.id = 'captcha-heading'; title.textContent = 'DuckDuckGo needs human verification';
      const instruction = document.createElement('p'); instruction.id = 'captcha-instruction';
      const help = document.createElement('p'); help.textContent = 'Research is paused. Select the matching images yourself and submit, or try the next search provider.';
      const timeout = document.createElement('p'); timeout.id = 'captcha-timeout';
      const grid = document.createElement('div'); grid.id = 'captcha-grid';
      const error = document.createElement('p'); error.id = 'captcha-error'; error.setAttribute('role', 'alert');
      dialog.append(title, instruction, help, timeout, grid, error);
      dialog.addEventListener('pointerdown', recordCaptchaActivity);
      dialog.addEventListener('keydown', recordCaptchaActivity);
      for (const [action, label] of [['submit', 'Submit answer'], ['skip', 'Try next search provider'], ['cancel', 'Cancel research']]) {
        const button = document.createElement('button'); button.type = 'button'; button.dataset.action = action; button.textContent = label;
        button.addEventListener('click', () => submitCaptcha(action)); dialog.append(button);
      }
      dialog.addEventListener('cancel', event => event.preventDefault());
      document.querySelector('main').append(dialog);
    }
    if (dialog.dataset.challenge !== challenge.id) {
      captchaLastActivity = 0;
      dialog.dataset.challenge = challenge.id;
      document.getElementById('captcha-instruction').textContent = challenge.instruction;
      document.getElementById('captcha-error').textContent = '';
      const grid = document.getElementById('captcha-grid'); grid.replaceChildren();
      for (let index = 0; index < challenge.tile_count; index++) {
        const label = document.createElement('label');
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = index;
        const image = document.createElement('img'); image.alt = `Challenge image ${index + 1}`;
        image.src = `/api/setup/${state.session_id}/captcha/${challenge.id}/tiles/${index}`;
        label.append(checkbox, document.createTextNode(` Image ${index + 1}`), image); grid.append(label);
      }
    }
    document.getElementById('captcha-timeout').textContent = Number.isFinite(challenge.idle_seconds_remaining)
      ? `Automatically tries the next provider after 30 seconds without interaction (${challenge.idle_seconds_remaining}s remaining).` : '';
    if (!dialog.open) { dialog.showModal(); dialog.querySelector('input').focus(); }
  }
  if (state.session_id && state.status === 'run_running' && state.run?.stage === 'searching') {
    captchaTimer = setTimeout(refreshCaptcha, 1500);
  }
}

/** Reset the server's inactivity deadline only for genuine puzzle interaction.
 * Args: event — trusted pointer or keyboard event inside the challenge dialog.
 * Returns: promise; rate-limits activity notices to one per second. Polling,
 * automatic focus and page reloads do not count. Expired challenges stay expired;
 * a failed activity notice is shown so the user knows the timer was not reset.
 */
async function recordCaptchaActivity(event) {
  if (!event.isTrusted || captchaSubmitting || !Number.isFinite(captchaState?.captcha?.idle_seconds_remaining)) return;
  const now = performance.now();
  if (captchaLastActivity && now - captchaLastActivity < 1000) return;
  captchaLastActivity = now;
  const state = captchaState;
  try {
    const response = await fetch(`/api/setup/${state.session_id}/captcha`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({challenge_id: state.captcha.id, action: 'activity', tiles: []})
    });
    if (!response.ok) throw new Error('Verification timer could not be extended; the provider may already have been skipped.');
  } catch (error) {
    document.getElementById('captcha-error').textContent = error.message;
  }
}

/** Read CAPTCHA progress without navigating or starting research.
 * Args: none; uses the latest session identity.
 * Returns: promise; updates the modal, retrying status reads on transient failure.
 */
async function refreshCaptcha() {
  const sid = captchaState?.session_id;
  if (!sid) return;
  try {
    const response = await fetch(`/api/setup/${sid}`);
    if (!response.ok) throw new Error('Could not refresh verification status.');
    const next = await response.json();
    if (captchaState?.session_id === sid) renderCaptcha(next);
  } catch (error) {
    const target = document.getElementById('captcha-error');
    if (target) target.textContent = error.message;
    captchaTimer = setTimeout(refreshCaptcha, 3000);
  }
}

/** Submit only an explicit human action bound to the displayed puzzle.
 * Args: action — submit, skip or cancel; selected checkboxes supply the answer.
 * Returns: promise; disables duplicate clicks and displays rejected/stale actions.
 * Neither polling nor pressing Escape submits an answer or bypasses a challenge.
 */
async function submitCaptcha(action) {
  if (captchaSubmitting || !captchaState?.captcha) return;
  captchaSubmitting = true;
  const state = captchaState, dialog = document.getElementById('captcha-dialog');
  const tiles = [...dialog.querySelectorAll('input:checked')].map(input => Number(input.value));
  for (const button of dialog.querySelectorAll('button')) button.disabled = true;
  try {
    const response = await fetch(`/api/setup/${state.session_id}/captcha`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({challenge_id: state.captcha.id, action, tiles})
    });
    const next = await response.json();
    if (!response.ok) throw new Error(typeof next.detail === 'string' ? next.detail : 'Could not submit the decision.');
    renderCaptcha(next);
  } catch (error) { document.getElementById('captcha-error').textContent = error.message; }
  finally {
    captchaSubmitting = false;
    for (const button of dialog.querySelectorAll('button')) button.disabled = false;
  }
}
