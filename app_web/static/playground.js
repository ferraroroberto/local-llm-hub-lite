/* Playground tab — stacked label/input pairs, More-options details,
 * segmented max-tokens with a numeric override, full-width Send.
 * Plus the Transcribe card: one audio upload → hub transcription proxy.
 */

import { els } from './state.js';
import { api, jsonApi, toast } from './api.js';

export async function fetchPlaygroundModels() {
  try {
    const body = await jsonApi('/admin/api/playground/models');
    const models = body.models || [];
    if (!els.playgroundModel) return;
    els.playgroundModel.innerHTML = '';
    models.forEach(function (m) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.display_name + ' (' + m.backend + ')';
      opt.dataset.backend = m.backend;
      els.playgroundModel.appendChild(opt);
    });
  } catch (_) { /* ignore */ }
}

/* Hidden-input file picker (#215): a ghost "Choose file" button proxies the
 * native input; the selected filename shows in the label to its right. */
function wireFilePicker(input, btn, nameEl) {
  if (!input || !btn) return;
  btn.addEventListener('click', function () { input.click(); });
  input.addEventListener('change', function () {
    if (!nameEl) return;
    const f = input.files && input.files[0];
    nameEl.textContent = f ? f.name : 'No file selected';
  });
}

export function wirePlayground() {
  if (els.playgroundSendBtn) {
    els.playgroundSendBtn.addEventListener('click', sendPrompt);
  }
  wireFilePicker(els.playgroundAttachment, els.playgroundAttachmentBtn, els.playgroundAttachmentName);
  wireTranscribe();
  if (els.playgroundClearBtn) {
    els.playgroundClearBtn.addEventListener('click', function () {
      els.playgroundReply.textContent = '';
      els.playgroundUsage.innerHTML = '';
      els.playgroundLatency.textContent = '';
    });
  }
  // Segmented max-tokens — clicking a preset highlights it AND mirrors
  // the value into the numeric override. Typing into the override clears
  // the active preset highlight (numeric override wins).
  if (els.playgroundMaxTokensSeg) {
    els.playgroundMaxTokensSeg.addEventListener('click', function (ev) {
      const btn = ev.target.closest('button[data-value]');
      if (!btn) return;
      const val = parseInt(btn.dataset.value, 10) || 512;
      els.playgroundMaxTokensSeg.querySelectorAll('button').forEach(function (b) {
        b.classList.toggle('active', b === btn);
      });
      if (els.playgroundMaxTokens) els.playgroundMaxTokens.value = String(val);
    });
  }
  if (els.playgroundMaxTokens) {
    els.playgroundMaxTokens.addEventListener('input', function () {
      // User typed into the override — clear preset highlights so it's
      // visually clear the value comes from the input, not a preset.
      if (!els.playgroundMaxTokensSeg) return;
      const current = String(parseInt(els.playgroundMaxTokens.value, 10) || 0);
      els.playgroundMaxTokensSeg.querySelectorAll('button').forEach(function (b) {
        b.classList.toggle('active', b.dataset.value === current);
      });
    });
  }
}

async function sendPrompt() {
  const modelSel = els.playgroundModel;
  const prompt = (els.playgroundPrompt.value || '').trim();
  if (!modelSel.value) {
    toast('Pick a model first.', 'error');
    return;
  }
  if (!prompt) {
    toast('Prompt is empty.', 'error');
    return;
  }
  els.playgroundSendBtn.disabled = true;
  els.playgroundReply.textContent = '…';
  els.playgroundUsage.innerHTML = '';
  els.playgroundLatency.textContent = 'sending…';

  const fd = new FormData();
  fd.append('model', modelSel.value);
  fd.append('prompt', prompt);
  fd.append('max_tokens', String(parseInt(els.playgroundMaxTokens.value, 10) || 512));
  const system = (els.playgroundSystem.value || '').trim();
  if (system) fd.append('system', system);
  if (els.playgroundAttachment && els.playgroundAttachment.files && els.playgroundAttachment.files[0]) {
    fd.append('attachment', els.playgroundAttachment.files[0]);
  }

  const t0 = performance.now();
  try {
    const res = await api('/admin/api/playground/send', { method: 'POST', body: fd });
    const body = await res.json().catch(function () { return null; });
    if (!res.ok) {
      const msg = (body && body.detail) || ('HTTP ' + res.status);
      els.playgroundReply.textContent = '[' + res.status + '] ' + msg;
      els.playgroundLatency.textContent = '';
      toast(msg, 'error');
      return;
    }
    const elapsed = (performance.now() - t0).toFixed(0);
    els.playgroundLatency.textContent = elapsed + ' ms';
    els.playgroundReply.textContent = body.text || '(no text)';
    renderUsage(body.usage || {});
  } catch (exc) {
    els.playgroundReply.textContent = String(exc.message || exc);
    toast(String(exc.message || exc), 'error');
  } finally {
    els.playgroundSendBtn.disabled = false;
  }
}

function renderUsage(usage) {
  const grid = els.playgroundUsage;
  if (!grid) return;
  grid.innerHTML = '';
  const rows = [
    ['Input', usage.input_tokens || 0],
    ['Output', usage.output_tokens || 0],
    ['Cache read', usage.cache_read_input_tokens || 0],
    ['Cache write', usage.cache_creation_input_tokens || 0],
  ];
  rows.forEach(function (r) {
    const div = document.createElement('div');
    div.innerHTML = '<span class="muted">' + r[0] + '</span><span>' + r[1] + '</span>';
    grid.appendChild(div);
  });
}

// --------------------------------------------------------------- transcribe
// One audio file → POST /admin/api/playground/transcribe (multipart), which
// forwards it loopback to the hub's /v1/audio/transcriptions proxy. No mic
// recording, no streaming — just the whisper {"text": ...} transcript.
function wireTranscribe() {
  wireFilePicker(els.transcribeFile, els.transcribeFileBtn, els.transcribeFileName);
  if (els.transcribeFile) {
    els.transcribeFile.addEventListener('change', function () {
      if (!els.transcribeBtn) return;
      els.transcribeBtn.disabled = !(els.transcribeFile.files && els.transcribeFile.files[0]);
    });
  }
  if (els.transcribeBtn) {
    els.transcribeBtn.addEventListener('click', transcribe);
  }
}

async function transcribe() {
  const f = els.transcribeFile && els.transcribeFile.files && els.transcribeFile.files[0];
  if (!f) {
    toast('Choose an audio file first.', 'error');
    return;
  }
  els.transcribeBtn.disabled = true;
  els.transcribeResult.textContent = '…';
  if (els.transcribeServedModel) els.transcribeServedModel.textContent = '';
  els.transcribeLatency.textContent = 'transcribing…';

  const fd = new FormData();
  fd.append('file', f);

  const t0 = performance.now();
  try {
    const res = await api('/admin/api/playground/transcribe', { method: 'POST', body: fd });
    const body = await res.json().catch(function () { return null; });
    if (!res.ok) {
      const msg = (body && body.detail) || ('HTTP ' + res.status);
      els.transcribeResult.textContent = '[' + res.status + '] ' + msg;
      els.transcribeLatency.textContent = '';
      toast(msg, 'error');
      return;
    }
    const elapsed = (performance.now() - t0).toFixed(0);
    els.transcribeLatency.textContent = elapsed + ' ms';
    els.transcribeResult.textContent = (body && body.text) || '(no text)';
    // Served model: the X-Hub-Served-Model header the hub stamps on the
    // upstream response, relayed through the admin endpoint's JSON.
    const served = res.headers.get('X-Hub-Served-Model') || (body && body.served_model) || '';
    if (els.transcribeServedModel) {
      els.transcribeServedModel.textContent = served ? 'served by ' + served : '';
    }
  } catch (exc) {
    els.transcribeResult.textContent = String(exc.message || exc);
    els.transcribeLatency.textContent = '';
    toast(String(exc.message || exc), 'error');
  } finally {
    els.transcribeBtn.disabled = false;
  }
}
