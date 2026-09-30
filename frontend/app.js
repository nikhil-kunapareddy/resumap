'use strict';

const S = {
  status: null, resumes: [], resume: null, job: null, sel: null, step: 1, showSkipped: false,
  experiences: [], backends: [], pick: null, prompts: [], promptKind: 'rewrite',
  examples: [],
};
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const KIND_LABELS = { project: 'Project', work: 'Work experience', soft_skill: 'Soft skill', other: 'Other' };

const MODE_HINTS = {
  aggressive: 'Maximum ATS overlap: may introduce tools named in the JD. Numbers are never invented, but check every flagged claim you could be asked about in an interview.',
  bridge: 'Keeps your metrics and re-expresses existing tools in the JD’s vocabulary. Every bridge is flagged for approval.',
  truthful: 'Only rewording, emphasis and tone. Nothing enters the bullet that wasn’t already in it.',
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind = '', ms = 4200) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), ms);
}

function setMsg(sel, text, kind = '') {
  const el = $(sel);
  el.textContent = text;
  el.className = 'msg ' + kind;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...opts,
  });
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

function busy(btn, on, label) {
  if (on) {
    btn.dataset.label = btn.innerHTML;
    btn.innerHTML = '<span class="spin"></span>' + (label || 'Working…');
    btn.disabled = true;
  } else {
    btn.innerHTML = btn.dataset.label || btn.innerHTML;
    btn.disabled = false;
  }
}

// ── navigation ──────────────────────────────────────────────
function setStep(n) {
  if (n >= 2 && !S.resume) return toast('Pick or upload a resume first.', 'err');
  if (n >= 3 && !S.job) return toast('Parse a job description first.', 'err');
  S.step = n;
  saveSession();
  $$('.panel').forEach((p) => p.classList.toggle('hidden', +p.dataset.panel !== n));
  $$('.step').forEach((b) => {
    const k = +b.dataset.step;
    b.classList.toggle('active', k === n);
    b.classList.toggle('done', k < n);
  });
  if (n === 3) renderMap();
  if (n === 4) renderReview();
  if (n === 5) { refreshPreview(); loadChat(); }
  if (n === 6) renderOutreach();
}

// ── session: survive a browser reload ───────────────────────
// There is no job picker, so without this a page refresh dropped you back on
// step 1 with the job you were tailoring out of reach.
const SESSION_KEY = 'resumap.session';

function saveSession() {
  localStorage.setItem(SESSION_KEY, JSON.stringify({
    resume: S.resume?.id ?? null, job: S.job?.id ?? null, step: S.step,
  }));
}

/** Put the resume and job from the last visit back. Returns the step to show. */
async function restoreSession() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); } catch (e) { /* none */ }
  if (!saved?.resume || !S.resumes.some((r) => r.id === saved.resume)) return 1;
  S.resume = await api(`/api/resumes/${saved.resume}`);
  if (saved.job) {
    try {
      const j = await api(`/api/jobs/${saved.job}`);
      if (j.resume_id === S.resume.id) loadJob(j);
    } catch (e) { /* the job is gone; start from its resume */ }
  }
  const step = saved.step || 1;
  if (step >= 3 && !S.job) return 2;
  return step;
}

/** Make a job from the server the one on screen. */
function loadJob(j) {
  S.job = { ...j };
  $('#company').value = j.company || '';
  $('#role').value = j.role || '';
  $('#mode').value = j.mode;
  $('#modeHint').textContent = MODE_HINTS[j.mode] || '';
  if (j.raw != null) $('#jdText').value = j.raw;
  renderJd();
}

/** Re-fetch everything the current step shows, without a page reload. */
async function reloadStep(btn) {
  busy(btn, true, 'Reloading…');
  try {
    await loadStatus();
    await loadExperiences();
    await loadExamples();
    await loadResumes();
    if (S.resume) {
      try {
        S.resume = await api(`/api/resumes/${S.resume.id}`);
      } catch (e) {
        S.resume = null; S.job = null;
      }
    }
    if (S.job) {
      try {
        loadJob(await api(`/api/jobs/${S.job.id}`));
      } catch (e) {
        S.job = null;
      }
    }
    await loadResumes();
    renderResumePreview();
    const step = S.step >= 3 && !S.job ? (S.resume ? 2 : 1) : (S.step >= 2 && !S.resume ? 1 : S.step);
    setStep(step);
    toast('Reloaded from the server.', 'ok', 1800);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    busy(btn, false);
  }
}

// ── status ──────────────────────────────────────────────────
async function loadStatus() {
  S.status = await api('/api/status');
  renderStatus();
  $('#firstName').value = S.status.first_name;
}

function renderStatus() {
  const b = $('#keyBadge');
  b.textContent = `${S.status.label} ${S.status.ready ? 'ready' : 'unavailable'}`;
  b.className = 'badge ' + (S.status.ready ? 'ok' : 'bad');
  b.title = S.status.detail || '';
  $('#modelBadge').textContent = S.status.model;
}

/** What to call the live backend in a busy label or a prompt. */
function agentName() {
  return S.status?.label || 'the model';
}

// ── settings: backend & theme ───────────────────────────────
const THEME_KEY = 'resumap.theme';
const darkQuery = matchMedia('(prefers-color-scheme: dark)');

const BACKEND_NOTES = {
  claude_code: 'Your local Claude Code install and its sign-in, usually a Claude subscription.',
  codex: 'The OpenAI Codex CLI, signed in with `codex login`, usually a ChatGPT plan.',
  pi: 'The pi coding agent, with whichever provider and model pi is set up for.',
  api: 'The Anthropic API, with ANTHROPIC_API_KEY from .env. Billed per token.',
};
const MODEL_HINTS = {
  claude_code: 'e.g. claude-opus-5',
  codex: 'blank uses Codex’s own configured model; e.g. gpt-5.5',
  pi: 'blank uses pi’s default; provider/model, e.g. google/gemini-2.5-pro',
  api: 'e.g. claude-opus-5',
};

function themeChoice() {
  return localStorage.getItem(THEME_KEY) || 'system';
}

function applyTheme(choice) {
  const dark = choice === 'dark' || (choice === 'system' && darkQuery.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $$('#themeSeg button').forEach((b) => b.classList.toggle('on', b.dataset.theme === choice));
}

async function openSettings() {
  $('#settings').classList.remove('hidden');
  applyTheme(themeChoice());
  setMsg('#backendMsg', '');
  $('#backendList').innerHTML = '<p class="hint">Checking what is installed…</p>';
  try {
    const r = await api('/api/backends');
    S.backends = r.backends;
    S.pick = r.selected;
    renderBackends();
  } catch (e) { setMsg('#backendMsg', e.message, 'err'); }
}

function closeSettings() {
  $('#settings').classList.add('hidden');
}

/** A backend or model choice that differs from what is saved, or null. */
function pendingBackend() {
  const cur = S.backends.find((b) => b.name === S.pick);
  if (!cur) return null;
  const live = S.backends.find((b) => b.selected);
  const model = $('#backendModel').value.trim();
  return cur.name !== live?.name || model !== (cur.model || '') ? { backend: cur.name, model } : null;
}

/**
 * Leaving the dialog saves what changed. A choice that fails its test call
 * keeps the dialog open with the reason; leaving again discards it.
 */
async function dismissSettings() {
  if (S.savingSettings) return;
  if (!pendingBackend() || S.discardSettings) {
    S.discardSettings = false;
    closeSettings();
    return;
  }
  S.savingSettings = true;
  const ok = await saveBackend($('#saveBackend'));
  S.savingSettings = false;
  if (ok) {
    closeSettings();
    toast(`Saved. Using ${S.status.label} (${S.status.model}).`, 'ok');
  } else {
    S.discardSettings = true;
    $('#backendMsg').textContent += ' Not saved. Fix it, or click outside again to discard the change.';
  }
}

function renderBackends() {
  $('#backendList').innerHTML = S.backends.map((b) => `
    <label class="backend ${b.name === S.pick ? 'on' : ''}">
      <input type="radio" name="backend" value="${b.name}" ${b.name === S.pick ? 'checked' : ''}>
      <div class="bd">
        <div class="nm">${esc(b.label)}${b.selected ? ' <span class="pill accepted">in use</span>' : ''}</div>
        <div class="hint">${esc(BACKEND_NOTES[b.name] || '')}</div>
        <div class="st ${b.ready ? 'ok' : 'bad'}">${b.ready ? 'Installed. Press Test to check it can answer.' : esc(b.detail || 'Unavailable')}</div>
      </div>
    </label>`).join('');
  $$('#backendList input').forEach((i) => i.addEventListener('change', () => {
    S.pick = i.value;
    S.discardSettings = false;
    setMsg('#backendMsg', '');
    renderBackends();
  }));
  const b = S.backends.find((x) => x.name === S.pick);
  $('#backendModel').value = b ? b.model : '';
  $('#backendModel').placeholder = b ? (b.default_model || 'CLI default') : '';
  $('#modelHint').textContent = MODEL_HINTS[S.pick] ? `(${MODEL_HINTS[S.pick]})` : '';
}

async function saveBackend(btn) {
  // The server tests the choice with one real call before switching to it.
  busy(btn, true, 'Checking…');
  try {
    S.status = await api('/api/backends', {
      method: 'POST', body: JSON.stringify({ backend: S.pick, model: $('#backendModel').value.trim() }),
    });
    renderStatus();
    S.backends = (await api('/api/backends')).backends;
    renderBackends();
    setMsg('#backendMsg', S.status.ready
      ? `Now using ${S.status.label} (${S.status.model}).`
      : `Switched to ${S.status.label}, but it is not ready: ${S.status.detail}`,
    S.status.ready ? 'ok' : 'warn');
    return true;
  } catch (e) {
    setMsg('#backendMsg', e.message, 'err');
    return false;
  } finally {
    busy(btn, false);
  }
}

async function testBackend(btn) {
  busy(btn, true, 'Testing…');
  setMsg('#backendMsg', '');
  try {
    const r = await api(`/api/backends/${S.pick}/test`, {
      method: 'POST', body: JSON.stringify({ model: $('#backendModel').value.trim() }),
    });
    setMsg('#backendMsg', r.detail, r.ok ? 'ok' : 'err');
  } catch (e) {
    setMsg('#backendMsg', e.message, 'err');
  } finally {
    busy(btn, false);
  }
}

// ── step 1: resumes ─────────────────────────────────────────
async function loadResumes() {
  S.resumes = await api('/api/resumes');
  const el = $('#resumeList');
  if (!S.resumes.length) {
    el.innerHTML = '<p class="hint">Nothing stored yet.</p>';
    return;
  }
  el.innerHTML = S.resumes.map((r) => `
    <div class="res-item ${S.resume && S.resume.id === r.id ? 'sel' : ''}" data-id="${r.id}">
      <div>
        <div class="nm">${esc(r.label)}</div>
        <div class="sub">${r.job_count} job${r.job_count === 1 ? '' : 's'} tailored</div>
      </div>
      <div class="sp"></div>
      <button class="tiny del" data-id="${r.id}">Delete</button>
    </div>`).join('');

  el.querySelectorAll('.res-item').forEach((n) =>
    n.addEventListener('click', (e) => {
      if (e.target.classList.contains('del')) return;
      selectResume(+n.dataset.id);
    }));
  el.querySelectorAll('.del').forEach((n) =>
    n.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm('Delete this resume and all of its tailored jobs?')) return;
      await api(`/api/resumes/${n.dataset.id}`, { method: 'DELETE' });
      if (S.resume && S.resume.id === +n.dataset.id) { S.resume = null; S.job = null; saveSession(); }
      await loadResumes();
      renderResumePreview();
    }));
}

async function selectResume(id) {
  S.resume = await api(`/api/resumes/${id}`);
  S.job = null;
  S.sel = null;
  saveSession();
  await loadResumes();
  renderResumePreview();
  setMsg('#uploadMsg', `Loaded "${S.resume.label}".`, 'ok');
}

function blockHtml(b, interactive) {
  const mapped = interactive && (S.job?.mappings?.[b.id] || []).length;
  const replace = interactive && isReplaced(b.id);
  const chips = mapped || replace
    ? `<div class="chips">${replace ? '<span class="chip replace">replace</span>' : ''}${
      (S.job.mappings[b.id] || []).map((p) => `<span class="chip">${esc(p)}</span>`).join('')}</div>`
    : '';
  const cls = [
    'blk box',
    interactive ? '' : 'static',
    b.mappable ? '' : 'skipped',
    b.exp ? 'exp' : '',
    S.sel === b.id ? 'sel' : '',
    mapped ? 'mapped' : '',
  ].filter(Boolean).join(' ');
  // A point that still stops on a connector means the parser could not recover
  // its continuation line. Say so rather than shipping half a sentence.
  const warn = b.truncated
    ? '<span class="trunc" title="This point looks cut off. Edit it after transforming, or check the source PDF.">may be cut off</span>'
    : '';
  return `<div class="${cls}" data-id="${b.id}">
      <span class="dot">${esc(b.bullet_char || '\u2022')}</span>
      <div class="bd">${b.tag ? `<span class="chip gray">${esc(b.tag)}</span>` : ''}${esc(b.text)}${warn}${chips}</div>
    </div>`;
}

/**
 * Both resume views show accomplishment points only -- never the name, contact
 * line, job titles, dates, skills inventory, degrees or summary. Those are still
 * parsed and still rendered into the exported PDF; they are just not something
 * you map, so they are not worth the screen space. The Map step can additionally
 * reveal skipped lines on demand.
 */
function visibleBlock(b, interactive) {
  if (b.mappable) return true;
  if (!interactive || !S.showSkipped) return false;
  return (b.kind === 'bullet' || b.kind === 'para') && b.text.length >= 15;
}

function renderBlocks(target, interactive) {
  const el = $(target);
  if (!S.resume) { el.innerHTML = '<p class="hint">No resume selected.</p>'; return; }
  const blocks = S.resume.blocks;
  let html = '';
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    if (b.kind === 'section') {
      // Only keep a heading when something under it is actually on screen.
      let has = false;
      for (let j = i + 1; j < blocks.length && blocks[j].kind !== 'section'; j++) {
        if (visibleBlock(blocks[j], interactive)) { has = true; break; }
      }
      if (has) html += `<div class="sec-head">${esc(b.text)}</div>`;
      continue;
    }
    if (!visibleBlock(b, interactive)) continue;
    html += blockHtml(b, interactive);
  }
  if (interactive && S.experiences.length) {
    html += '<div class="sec-head">Your experiences · not on the resume</div>';
    html += S.experiences.map((e) => blockHtml(expBlock(e), true)).join('');
  }
  el.innerHTML = html || `<p class="hint">No accomplishment points found in this resume.${
    interactive ? ' Use "Show skipped lines" to map something anyway.' : ''}</p>`;
  if (interactive) {
    el.querySelectorAll('.box').forEach((n) =>
      n.addEventListener('click', () => { S.sel = n.dataset.id; renderMap(); }));
  }
}

/** An experience shaped like a resume block, so the Map step can treat both alike. */
function expBlock(e) {
  return {
    id: e.key, exp: true, mappable: true, bullet_char: '+',
    tag: KIND_LABELS[e.kind] || 'Other',
    text: e.title ? `${e.title}: ${e.text}` : e.text,
  };
}

// ── step 1: experiences ─────────────────────────────────────
async function loadExperiences() {
  S.experiences = await api('/api/experiences');
  renderExperiences();
}

function renderExperiences() {
  const el = $('#expList');
  const n = S.experiences.length;
  $('#expCount').textContent = n ? `${n} stored` : '';
  if (!n) { el.innerHTML = '<p class="hint">None yet.</p>'; return; }
  el.innerHTML = S.experiences.map((e) => `
    <div class="exp-item">
      <div class="bd">
        <div class="nm"><span class="chip gray">${esc(KIND_LABELS[e.kind] || 'Other')}</span>${esc(e.title)}</div>
        <div class="tx">${esc(e.text)}</div>
      </div>
      <button class="tiny del" data-id="${e.id}">Delete</button>
    </div>`).join('');
  el.querySelectorAll('.del').forEach((b) =>
    b.addEventListener('click', () => deleteExperience(+b.dataset.id)));
}

async function addExperience(btn) {
  const text = $('#expText').value.trim();
  if (!text) return setMsg('#expMsg', 'Describe the experience first.', 'err');
  busy(btn, true, 'Saving…');
  try {
    const e = await api('/api/experiences', {
      method: 'POST',
      body: JSON.stringify({ kind: $('#expKind').value, title: $('#expTitle').value, text }),
    });
    S.experiences.push(e);
    $('#expTitle').value = '';
    $('#expText').value = '';
    renderExperiences();
    setMsg('#expMsg', 'Saved. It shows up on the Map step for every job.', 'ok');
  } catch (err) {
    setMsg('#expMsg', err.message, 'err');
  } finally {
    busy(btn, false);
  }
}

async function deleteExperience(id) {
  const e = S.experiences.find((x) => x.id === id);
  if (!e || !confirm('Delete this experience? Any bullet written from it is removed from every job.')) return;
  try {
    await api(`/api/experiences/${id}`, { method: 'DELETE' });
    S.experiences = S.experiences.filter((x) => x.id !== id);
    // The server unpicked it everywhere; mirror that for the job on screen.
    if (S.job) {
      delete S.job.mappings[e.key];
      if (S.job.rewrites) delete S.job.rewrites[e.key];
      if (S.sel === e.key) S.sel = null;
    }
    renderExperiences();
    setMsg('#expMsg', '');
    toast('Experience deleted.', 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

function renderResumePreview() {
  renderBlocks('#resumePreview', false);
  if (!S.resume) { $('#blockCount').textContent = ''; return; }
  const pts = S.resume.blocks.filter((b) => b.mappable).length;
  $('#blockCount').textContent = `${pts} point${pts === 1 ? '' : 's'}`;
}

async function upload(file) {
  const fd = new FormData();
  fd.append('file', file);
  setMsg('#uploadMsg', 'Parsing…');
  try {
    const r = await api('/api/resumes', { method: 'POST', body: fd });
    await loadResumes();
    await selectResume(r.id);
    toast('Resume stored and parsed.', 'ok');
  } catch (e) {
    setMsg('#uploadMsg', e.message, 'err');
  }
}

// ── step 2: JD ──────────────────────────────────────────────
async function parseJd(btn) {
  const text = $('#jdText').value.trim();
  if (text.length < 40) return setMsg('#jdMsg', 'Paste the job description first.', 'err');
  busy(btn, true, 'Parsing…');
  setMsg('#jdMsg', '');
  try {
    const j = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ resume_id: S.resume.id, text, mode: $('#mode').value }),
    });
    S.job = { ...j, mappings: {}, box_modes: {}, rewrites: {}, outreach: null };
    S.sel = null;
    saveSession();
    $('#company').value = j.company;
    $('#role').value = j.role;
    renderJd();
    if (j.warning) setMsg('#jdMsg', j.warning, 'warn');
    else setMsg('#jdMsg', `Found ${j.points.length} points, word for word from your paste.`, 'ok');
    toast(`Parsed ${j.points.length} JD points.`, 'ok');
  } catch (e) {
    setMsg('#jdMsg', e.message, 'err');
  } finally {
    busy(btn, false);
  }
}

function keptPoints() {
  return S.job ? S.job.points.filter((p) => !p.dropped) : [];
}

function renderJd() {
  const el = $('#jdPreview');
  if (!S.job) { el.innerHTML = '<p class="hint">Nothing parsed yet.</p>'; $('#pointCount').textContent = ''; return; }
  const kept = keptPoints().length;
  const gone = S.job.points.length - kept;
  $('#pointCount').textContent = `${kept} points${gone ? ` \u00b7 ${gone} removed` : ''}`;
  el.innerHTML = S.job.points.map((p) => `
    <div class="pt ${p.dropped ? 'dropped' : 'disabled'}">
      <div class="tx">
        <div class="cat">${esc(p.id)} &middot; ${esc(p.category.replace('_', ' '))}</div>
        ${esc(p.text)}
        ${p.keywords?.length ? `<div class="kw">${p.keywords.map((k) => `<span>${esc(k)}</span>`).join('')}</div>` : ''}
      </div>
      <button class="ptdel" data-id="${p.id}" data-keep="${p.dropped ? '1' : '0'}"
        title="${p.dropped ? 'Put this point back' : 'Remove this point before mapping'}"
        >${p.dropped ? 'Undo' : 'Remove'}</button>
    </div>`).join('');
  el.querySelectorAll('.ptdel').forEach((b) =>
    b.addEventListener('click', () => keepPoint(b.dataset.id, b.dataset.keep === '1')));
}

async function keepPoint(id, keep) {
  const ids = S.job.points.filter((p) => (p.id === id ? keep : !p.dropped)).map((p) => p.id);
  if (!ids.length) return toast('Keep at least one point to map against.', 'err');
  try {
    const r = await api(`/api/jobs/${S.job.id}/points`, {
      method: 'PUT', body: JSON.stringify({ keep: ids }),
    });
    S.job.points = r.points;
    renderJd();
    if (r.unwired) {
      // Dropping a mapped point unpicks it server-side; resync so step 3 agrees.
      S.job.mappings = (await api(`/api/jobs/${S.job.id}`)).mappings;
      toast(`Removed ${id} and unpicked ${r.unwired} mapping${r.unwired === 1 ? '' : 's'}.`, 'ok');
    } else {
      toast(keep ? `Restored ${id}.` : `Removed ${id}.`, 'ok');
    }
  } catch (e) {
    setMsg('#jdMsg', e.message, 'err');
  }
}

async function saveJobField(field, value) {
  if (!S.job) return;
  S.job[field] = value;
  await api(`/api/jobs/${S.job.id}`, { method: 'PATCH', body: JSON.stringify({ [field]: value }) });
}

// ── step 3: mapping ─────────────────────────────────────────
function renderMap() {
  renderBlocks('#mapBlocks', true);
  const pts = S.resume.blocks.filter((b) => b.mappable).length;
  const skipped = S.resume.blocks.filter(
    (b) => !b.mappable && (b.kind === 'bullet' || b.kind === 'para') && b.text.length >= 15).length;
  const nx = S.experiences.length;
  $('#mapPointCount').textContent = `${pts} point${pts === 1 ? '' : 's'}${
    nx ? ` · ${nx} experience${nx === 1 ? '' : 's'}` : ''}`;
  $('#toggleSkipped').textContent = S.showSkipped
    ? 'Hide skipped lines'
    : `Show ${skipped} skipped line${skipped === 1 ? '' : 's'}`;
  const el = $('#mapPoints');
  if (!S.job) return;

  const counts = {};
  Object.values(S.job.mappings).flat().forEach((p) => { counts[p] = (counts[p] || 0) + 1; });
  const active = S.sel ? (S.job.mappings[S.sel] || []) : null;

  $('#mapSelected').textContent = S.sel
    ? 'Tick every JD point this box should answer.'
    : 'Select a resume box on the left to start mapping.';
  // Experiences are new bullets already, so only a resume box can be replaced.
  const canReplace = !!S.sel && !S.sel.startsWith('exp');
  $('#replaceOpt').classList.toggle('hidden', !canReplace);
  $('#replaceOpt').classList.toggle('on', canReplace && isReplaced(S.sel));
  $('#replaceBox').checked = canReplace && isReplaced(S.sel);

  el.innerHTML = keptPoints().map((p) => {
    const on = active && active.includes(p.id);
    return `<label class="pt ${on ? 'on' : ''} ${S.sel ? '' : 'disabled'}" data-id="${p.id}">
        <input type="checkbox" ${on ? 'checked' : ''} ${S.sel ? '' : 'disabled'}>
        <div class="tx">
          <div class="cat">${esc(p.id)} &middot; ${esc(p.category.replace('_', ' '))}</div>
          ${esc(p.text)}
          ${p.keywords?.length ? `<div class="kw">${p.keywords.map((k) => `<span>${esc(k)}</span>`).join('')}</div>` : ''}
        </div>
        ${counts[p.id] ? `<span class="usage">${counts[p.id]}&times;</span>` : ''}
      </label>`;
  }).join('');

  el.querySelectorAll('.pt input').forEach((cb) =>
    cb.addEventListener('change', () => toggleMap(cb.closest('.pt').dataset.id, cb.checked)));

  const mappedBoxes = Object.values(S.job.mappings).filter((v) => v.length).length;
  const links = Object.values(S.job.mappings).flat().length;
  $('#mapSummary').textContent = `${mappedBoxes} box${mappedBoxes === 1 ? '' : 'es'}, ${links} link${links === 1 ? '' : 's'}`;
  $('#runTransform').disabled = !mappedBoxes;
}

async function toggleMap(pointId, on) {
  if (!S.sel) return;
  const cur = new Set(S.job.mappings[S.sel] || []);
  on ? cur.add(pointId) : cur.delete(pointId);
  const ids = S.job.points.map((p) => p.id).filter((id) => cur.has(id));
  S.job.mappings[S.sel] = ids;
  renderMap();
  try {
    await api(`/api/jobs/${S.job.id}/mappings`, {
      method: 'POST',
      body: JSON.stringify({ block_id: S.sel, point_ids: ids }),
    });
  } catch (e) { toast(e.message, 'err'); }
}

function isReplaced(id) {
  return S.job?.box_modes?.[id] === 'replace';
}

async function setReplace(on) {
  const id = S.sel;
  if (!id || !S.job) return;
  S.job.box_modes = { ...(S.job.box_modes || {}) };
  if (on) S.job.box_modes[id] = 'replace'; else delete S.job.box_modes[id];
  renderMap();
  try {
    await api(`/api/jobs/${S.job.id}/box-modes`, {
      method: 'POST', body: JSON.stringify({ block_id: id, mode: on ? 'replace' : '' }),
    });
  } catch (e) {
    if (on) delete S.job.box_modes[id]; else S.job.box_modes[id] = 'replace';
    renderMap();
    toast(e.message, 'err');
  }
}

/** Which Transform prompt a box gets: see _transform_jobs in main.py. */
function promptKindOf(id) {
  if (id && id.startsWith('exp')) return 'experience';
  return isReplaced(id) ? 'replace' : 'rewrite';
}

async function runTransform(btn, blockIds, extra) {
  busy(btn, true, `Calling ${agentName()}…`);
  try {
    const r = await api(`/api/jobs/${S.job.id}/transform`, {
      method: 'POST',
      body: JSON.stringify({ block_ids: blockIds || null, extra: extra || '' }),
    });
    S.job.rewrites = r.rewrites;
    const failed = r.results.filter((x) => !x.ok);
    if (failed.length) toast(`${failed.length} rewrite(s) failed: ${failed[0].error}`, 'err', 7000);
    else toast(`Rewrote ${r.results.length} bullet${r.results.length === 1 ? '' : 's'}.`, 'ok');
    setStep(4);
  } catch (e) {
    toast(e.message, 'err', 7000);
  } finally {
    busy(btn, false);
  }
}

// ── step 3: transform prompt ────────────────────────────────
async function openPrompt() {
  S.promptKind = promptKindOf(S.sel);
  $('#promptDlg').classList.remove('hidden');
  $('#promptEditors').innerHTML = '<p class="hint">Loading…</p>';
  try {
    S.prompts = (await api('/api/prompts')).prompts;
    renderPromptEditors();
    loadPromptPreview();
  } catch (e) { toast(e.message, 'err'); }
}

async function closePrompt() {
  await flushPrompts();
  $('#promptDlg').classList.add('hidden');
}

/** Save any editor whose text differs from what the server has. Closing or
 *  switching tabs does not reliably blur a textarea, so this does not rely on it. */
async function flushPrompts() {
  for (const el of $$('#promptEditors .pe')) {
    const p = S.prompts.find((x) => x.key === el.dataset.key);
    const text = el.querySelector('textarea').value;
    if (p && text !== p.text) await savePrompt(p.key, text);
  }
}

function renderPromptEditors() {
  $$('#promptTabs button').forEach((b) => b.classList.toggle('on', b.dataset.kind === S.promptKind));
  const mode = S.job?.mode || 'aggressive';
  // A replaced bullet ignores the mode rules: they all keep the original's work.
  const keys = S.promptKind === 'replace' ? ['replace'] : [S.promptKind, `mode.${mode}`];
  $('#promptEditors').innerHTML = keys.map((key) => {
    const p = S.prompts.find((x) => x.key === key);
    if (!p) return '';
    const isMode = key.startsWith('mode.');
    return `<div class="pe ${isMode ? 'mode' : ''}" data-key="${key}">
      <div class="row between">
        <span class="lbl">${esc(isMode ? `${p.label} (this job’s mode, for bullets and experiences)` : `Instructions: ${p.label}`)}
          <span class="pill ${p.custom ? 'edited' : ''}">${p.custom ? 'edited' : 'default'}</span></span>
        <button class="tiny reset" ${p.custom ? '' : 'disabled'}>Reset to default</button>
      </div>
      <textarea spellcheck="false">${esc(p.text)}</textarea>
    </div>`;
  }).join('');
  $$('#promptEditors .pe').forEach((el) => {
    const key = el.dataset.key;
    el.querySelector('textarea').addEventListener('change', (e) => savePrompt(key, e.target.value));
    el.querySelector('.reset').addEventListener('click', () => resetPrompt(key));
  });
}

/** Update one editor's badge in place, so saving never steals focus. */
function markPrompt(p) {
  S.prompts = S.prompts.map((x) => (x.key === p.key ? p : x));
  const el = $(`#promptEditors .pe[data-key="${p.key}"]`);
  if (!el) return;
  const pill = el.querySelector('.pill');
  pill.textContent = p.custom ? 'edited' : 'default';
  pill.classList.toggle('edited', p.custom);
  el.querySelector('.reset').disabled = !p.custom;
}

async function savePrompt(key, text) {
  const cur = S.prompts.find((x) => x.key === key);
  if (cur && text === cur.text) return;
  try {
    const p = await api(`/api/prompts/${encodeURIComponent(key)}`, {
      method: 'PUT', body: JSON.stringify({ text }),
    });
    markPrompt(p);
    loadPromptPreview();
    toast(p.custom ? 'Prompt saved. Transform uses it from now on.' : 'Same as the default, so the default is back in use.', 'ok', 2600);
  } catch (e) { toast(e.message, 'err'); }
}

async function resetPrompt(key) {
  if (!confirm('Put the default instructions back? Your edited version is discarded.')) return;
  try {
    const p = await api(`/api/prompts/${encodeURIComponent(key)}`, { method: 'DELETE' });
    markPrompt(p);
    const ta = $(`#promptEditors .pe[data-key="${key}"] textarea`);
    if (ta) ta.value = p.text;
    loadPromptPreview();
    toast('Default prompt restored.', 'ok', 2200);
  } catch (e) { toast(e.message, 'err'); }
}

async function loadPromptPreview() {
  const el = $('#promptPreview');
  const id = S.sel;
  if (!id || !(S.job?.mappings?.[id] || []).length) {
    el.textContent = 'Select a box on the left and tick at least one JD point to see the exact prompt it gets.';
    return;
  }
  const kind = promptKindOf(id);
  if (kind !== S.promptKind) {
    const what = { experience: 'an experience', replace: 'a replaced bullet', rewrite: 'a resume bullet' }[kind];
    el.textContent = `The selected box is ${what}. Switch tabs to preview it.`;
    return;
  }
  try {
    const r = await api(`/api/jobs/${S.job.id}/prompt?block_id=${encodeURIComponent(id)}`);
    el.innerHTML = `${esc(r.system)}<span class="cut">──── then this message ────</span>${esc(r.user)}`;
  } catch (e) { el.textContent = e.message; }
}

// ── step 4: review ──────────────────────────────────────────
function renderReview() {
  const el = $('#reviewList');
  const rws = S.job?.rewrites || {};
  const ids = Object.keys(rws);
  if (!ids.length) {
    el.innerHTML = '<p class="hint">Nothing transformed yet. Map some boxes on step 3 first.</p>';
    $('#reviewCount').textContent = '';
    return;
  }
  const blocks = Object.fromEntries(S.resume.blocks.map((b) => [b.id, b]));
  const exps = Object.fromEntries(S.experiences.map((e) => [e.key, e]));
  // Resume blocks in page order, then new bullets from experiences.
  const order = [
    ...S.resume.blocks.map((b) => b.id).filter((id) => ids.includes(id)),
    ...ids.filter((id) => exps[id]).sort(),
  ];
  const accepted = ids.filter((i) => ['accepted', 'edited'].includes(rws[i].status)).length;
  $('#reviewCount').textContent = `${accepted} of ${ids.length} will be applied`;

  el.innerHTML = order.map((id) => {
    const rw = rws[id];
    const b = blocks[id] || {};
    const meta = rw.meta || {};
    const subs = (meta.substitutions || []).filter((s) => !s.grounded);
    const risky = meta.defensible === false;
    const exp = exps[id];
    const sec = exp ? `new bullet · ${KIND_LABELS[exp.kind] || 'experience'}` : (b.section || 'resume');
    const replaced = !exp && meta.replaced;
    return `<div class="rev ${rw.status}" data-id="${id}">
      <div class="rev-head">
        <span class="sec">${esc(sec)}</span>
        <span>${(S.job.mappings[id] || []).join(', ') || 'unmapped'}</span>
        <div class="sp"></div>
        ${replaced ? '<span class="pill replace" title="Written from scratch for its JD points, not reworded">replaced</span>' : ''}
        ${risky ? '<span class="pill risk">not fully defensible</span>' : ''}
        ${(meta.keywords_used || []).length ? `<span class="pill">+${meta.keywords_used.length} keywords</span>` : ''}
        <span class="pill ${rw.status}">${esc(rw.status)}</span>
      </div>
      <div class="sbs">
        <div><div class="lbl">${exp ? 'Your notes' : replaced ? 'Original (replaced)' : 'Original'}</div>${esc(rw.original)}</div>
        <div><div class="lbl">Tailored</div><textarea class="ta">${esc(rw.rewritten)}</textarea></div>
      </div>
      ${subs.length ? `<div class="subs"><b>Introduced, not in your original:</b><ul>${
        subs.map((s) => `<li>${esc(s.replacement)}${s.original ? ` (was: ${esc(s.original)})` : ''} — ${esc(s.note)}</li>`).join('')
      }</ul></div>` : ''}
      ${exp ? `<div class="place">Goes under <select class="placement">${placementOptions(meta.placement)}</select></div>` : ''}
      <div class="rev-foot">
        <button class="tiny primary act" data-a="accepted">Accept</button>
        <button class="tiny act" data-a="rejected">Reject</button>
        <button class="tiny save">Save edit</button>
        <button class="tiny reroll">Re-roll</button>
        <button class="tiny reroll-note">Re-roll with a note…</button>
      </div>
    </div>`;
  }).join('');

  el.querySelectorAll('.rev').forEach((card) => {
    const id = card.dataset.id;
    card.querySelectorAll('.act').forEach((btn) =>
      btn.addEventListener('click', () => patchRewrite(id, { status: btn.dataset.a })));
    card.querySelector('.placement')?.addEventListener('change', (e) =>
      patchRewrite(id, { placement: e.target.value }));
    card.querySelector('.save').addEventListener('click', () =>
      patchRewrite(id, { text: card.querySelector('.ta').value, status: 'edited' }));
    card.querySelector('.reroll').addEventListener('click', (e) =>
      runTransform(e.target, [id], ''));
    card.querySelector('.reroll-note').addEventListener('click', (e) => {
      const note = prompt(`What should ${agentName()} do differently for this bullet?`);
      if (note) runTransform(e.target, [id], note);
    });
  });
}

const NEW_SECTIONS = ['PROJECTS', 'ADDITIONAL EXPERIENCE', 'LEADERSHIP'];

/** Where a new bullet can go: its own section, under an entry, or a section's end. */
function placementOptions(current) {
  const cur = current || '';
  const news = [...NEW_SECTIONS];
  if (cur.startsWith('new:') && !news.includes(cur.slice(4))) news.unshift(cur.slice(4));
  const opt = (v, label) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(label)}</option>`;
  const short = (t) => (t.length > 70 ? t.slice(0, 69) + '…' : t);
  const blocks = S.resume.blocks;
  return `<optgroup label="A section of its own">${
      news.map((h) => opt('new:' + h, `New section: ${h}`)).join('')}</optgroup>
    <optgroup label="Under a role or entry (only if it happened there)">${
      blocks.filter((b) => b.kind === 'entry').map((b) => opt(b.id, short(b.left || b.text))).join('')}</optgroup>
    <optgroup label="At the end of a section">${
      blocks.filter((b) => b.kind === 'section').map((b) => opt(b.id, b.text)).join('')}</optgroup>`;
}

async function patchRewrite(blockId, body) {
  try {
    const rw = await api(`/api/jobs/${S.job.id}/rewrites/${blockId}`, {
      method: 'PATCH', body: JSON.stringify(body),
    });
    S.job.rewrites[blockId] = rw;
    renderReview();
  } catch (e) { toast(e.message, 'err'); }
}

// ── step 5: export ──────────────────────────────────────────
async function refreshPreview() {
  if (!S.job) return;
  $('#pdf').src = `/api/jobs/${S.job.id}/preview.pdf?t=${Date.now()}`;
  const rws = S.job.rewrites || {};
  const n = Object.values(rws).filter((r) => ['accepted', 'edited'].includes(r.status)).length;
  let msg = `${n} rewrite${n === 1 ? '' : 's'} applied out of ${Object.keys(rws).length}.`;
  let kind = n ? 'ok' : 'warn';
  try {
    const fit = await api(`/api/jobs/${S.job.id}/fit`);
    const pct = Math.round(fit.font_scale * 100);
    if (fit.pages > 1) {
      msg += ` Could not reach one page even at ${pct}% type \u2014 trim some content.`;
      kind = 'err';
    } else if (fit.cramped) {
      msg += ` Fitted to one page at ${pct}% type, which is tight \u2014 consider trimming.`;
      kind = 'warn';
    } else if (fit.compressed) {
      msg += ' Fitted to one page at full type size.';
    }
  } catch (e) { /* preview still renders; the fit note is advisory */ }
  setMsg('#exportMsg', msg, kind);
}

async function doExport(btn) {
  busy(btn, true, 'Rendering…');
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({ first_name: $('#firstName').value }) });
    const r = await api(`/api/jobs/${S.job.id}/export`, { method: 'POST' });
    const a = document.createElement('a');
    a.href = r.url;
    a.download = r.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(`Downloaded ${r.filename} (${r.applied}/${r.total} rewrites applied).`, 'ok', 6000);
  } catch (e) {
    setMsg('#exportMsg', e.message, 'err');
  } finally {
    busy(btn, false);
  }
}

// ── step 5: corrections chat ────────────────────────────────
const CHAT_SUGGESTIONS = [
  'Shorten any bullet that now takes more lines than in my original.',
  'Make the font slightly bigger if it still fits on one page.',
  'Add a little more space between sections.',
];

const LAYOUT_LABELS = {
  font: 'font size', leading: 'line spacing', spacing: 'block spacing',
  margin_x: 'side margins', margin_y: 'top and bottom margins', justify: 'justification',
};

function layoutChange(key, value) {
  const label = LAYOUT_LABELS[key] || key;
  if (key === 'justify') return `${label}: ${value}`;
  if (key.startsWith('margin')) return `${label}: ${value == null ? 'as uploaded' : Math.round(value) + 'pt'}`;
  return `${label}: ${Math.round(value * 100)}%`;
}

function blockSnippet(id) {
  const b = (S.resume?.blocks || []).find((x) => x.id === id);
  const text = b ? b.text : S.job?.rewrites?.[id]?.rewritten;
  if (!text) return id;
  const words = text.split(/\s+/);
  return words.slice(0, 6).join(' ') + (words.length > 6 ? '…' : '');
}

async function loadChat() {
  if (!S.job) return;
  try {
    const r = await api(`/api/jobs/${S.job.id}/chat`);
    S.job.chat = r.messages;
    renderChat();
  } catch (e) { toast(e.message, 'err'); }
}

function renderChat() {
  const el = $('#chatLog');
  const msgs = S.job?.chat || [];
  if (!msgs.length) {
    el.innerHTML = `<p class="hint">Nothing asked yet. For example:</p><div class="chat-sugg">${
      CHAT_SUGGESTIONS.map((s) => `<button class="tiny sugg">${esc(s)}</button>`).join('')}</div>`;
    el.querySelectorAll('.sugg').forEach((b) =>
      b.addEventListener('click', () => sendChat(b.textContent)));
    return;
  }
  el.innerHTML = msgs.map((m) => {
    if (m.role === 'user') {
      return `<div class="cm user ${m.pending ? 'pending' : ''}"><div class="tx">${esc(m.text)}</div></div>`;
    }
    const meta = m.meta || {};
    const changes = [
      ...(meta.edits || []).map((id) => `Reworded “${esc(blockSnippet(id))}”`),
      ...Object.entries(meta.layout || {}).map(([k, v]) => esc(layoutChange(k, v))),
    ];
    return `<div class="cm bot ${meta.undone ? 'undone' : ''}">
        <div class="tx">${esc(m.text)}</div>
        ${changes.length ? `<ul class="cm-changes">${changes.map((c) => `<li>${c}</li>`).join('')}</ul>` : ''}
        ${m.can_undo ? `<button class="tiny undo" data-id="${m.id}">Undo</button>` : ''}
        ${meta.undone ? '<span class="pill">undone</span>' : ''}
      </div>`;
  }).join('');
  el.querySelectorAll('.undo').forEach((b) =>
    b.addEventListener('click', () => undoChat(+b.dataset.id, b)));
  el.scrollTop = el.scrollHeight;
}

function applyChat(r) {
  S.job.chat = r.messages;
  S.job.rewrites = r.rewrites;
  renderChat();
  refreshPreview();
}

async function sendChat(text) {
  const btn = $('#chatSend');
  const msg = (text ?? $('#chatInput').value).trim();
  if (!msg || !S.job || btn.disabled) return;
  // Shown straight away; the server only stores it once it has an answer.
  S.job.chat = [...(S.job.chat || []), { id: 0, role: 'user', text: msg, pending: true }];
  renderChat();
  $('#chatInput').value = '';
  busy(btn, true, 'Thinking…');
  try {
    applyChat(await api(`/api/jobs/${S.job.id}/chat`, {
      method: 'POST', body: JSON.stringify({ message: msg }),
    }));
  } catch (e) {
    S.job.chat = S.job.chat.filter((m) => !m.pending);
    renderChat();
    $('#chatInput').value = msg;
    toast(e.message, 'err', 7000);
  } finally {
    busy(btn, false);
  }
}

async function undoChat(id, btn) {
  busy(btn, true, 'Undoing…');
  try {
    applyChat(await api(`/api/jobs/${S.job.id}/chat/${id}/undo`, { method: 'POST' }));
  } catch (e) {
    toast(e.message, 'err');
    busy(btn, false);
  }
}

// ── step 6: recruiter outreach ──────────────────────────────
const LI_LIMIT = 300;

function renderOutreach() {
  const o = S.job?.outreach;
  // Cleared, not left alone, when there is no draft: the boxes would otherwise
  // still be showing the previous job's messages.
  $('#recruiter').value = (o && o.recruiter) || '';
  $('#liSubject').value = (o && o.linkedin_subject) || '';
  $('#liText').value = (o && o.linkedin) || '';
  $('#emSubject').value = (o && o.email_subject) || '';
  $('#emBody').value = (o && o.email_body) || '';
  outreachLabel();
  const used = (o && o.meta && o.meta.highlights_used) || [];
  const src = $('#outreachSrc');
  src.classList.toggle('hidden', !used.length);
  if (used.length) {
    src.innerHTML = `<b>Written from these resume points:</b><ul>${
      used.map((h) => `<li>${esc(h)}</li>`).join('')}</ul>`;
  }
  syncOutreach();
}

function outreachLabel() {
  $('#makeOutreach').textContent = S.job && S.job.outreach ? 'Re-draft' : 'Draft messages';
}

/** Character budget and the mailto link both follow whatever is in the boxes. */
function syncOutreach() {
  const n = $('#liText').value.length;
  const c = $('#liCount');
  c.textContent = `${n} / ${LI_LIMIT}`;
  c.className = 'count' + (n > LI_LIMIT ? ' over' : '');
  $('#mailto').href = 'mailto:?subject=' + encodeURIComponent($('#emSubject').value) +
    '&body=' + encodeURIComponent($('#emBody').value);
}

async function makeOutreach(btn) {
  if (!S.job) return;
  busy(btn, true, 'Writing…');
  setMsg('#outreachMsg', '');
  try {
    const r = await api(`/api/jobs/${S.job.id}/outreach`, {
      method: 'POST',
      body: JSON.stringify({ recruiter: $('#recruiter').value, extra: $('#outreachNote').value }),
    });
    S.job.outreach = r.outreach;
    renderOutreach();
    const over = r.outreach.linkedin.length - LI_LIMIT;
    if (over > 0) {
      setMsg('#outreachMsg', `The note came back ${over} characters over LinkedIn's limit. Trim it before sending.`, 'warn');
    } else {
      setMsg('#outreachMsg', 'Drafted. Check every claim before you send it.', 'ok');
    }
    toast('Outreach drafted.', 'ok');
  } catch (e) {
    setMsg('#outreachMsg', e.message, 'err');
  } finally {
    busy(btn, false);   // restores the label busy() captured, so re-apply ours
    outreachLabel();
  }
}

async function saveOutreach() {
  if (!S.job || !S.job.outreach) return;
  const body = {
    recruiter: $('#recruiter').value,
    linkedin: $('#liText').value,
    linkedin_subject: $('#liSubject').value,
    email_subject: $('#emSubject').value,
    email_body: $('#emBody').value,
  };
  try {
    const r = await api(`/api/jobs/${S.job.id}/outreach`, { method: 'PATCH', body: JSON.stringify(body) });
    S.job.outreach = r.outreach;
    setMsg('#outreachMsg', 'Saved.', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

// ── step 6: liked examples ──────────────────────────────────
// Shared by every job, like experiences. The newest few of each kind go into
// every draft as style references; the prompt forbids taking facts from them.
const EX_LIMIT = 5;

/** The list and paste box under each message, built once so a re-render of
 *  the list never wipes an example half-pasted into the box. */
function buildExampleSlots() {
  $$('.examples').forEach((el) => {
    const kind = el.dataset.kind;
    el.innerHTML = `
      <h3>Examples you like <span class="count ex-count"></span></h3>
      <p class="hint">Drafts copy their voice (tone, length, structure, the ask), never their facts. The newest ${EX_LIMIT} are sent with every draft.</p>
      <div class="list ex-list"></div>
      <details class="ex-add">
        <summary>Paste an example</summary>
        <label class="field"><span>Subject <em>optional</em></span><input class="ex-subject"></label>
        <textarea class="ex-body" rows="6" style="margin-top:8px"
          placeholder="A ${kind === 'linkedin' ? 'LinkedIn message' : 'recruiter email'} you liked, whoever wrote it."></textarea>
        <div class="actions" style="margin-top:8px"><button class="tiny ex-save">Add example</button></div>
      </details>`;
    el.querySelector('.ex-save').addEventListener('click', async (e) => {
      const subj = el.querySelector('.ex-subject');
      const body = el.querySelector('.ex-body');
      if (await addExample(kind, subj.value, body.value, e.currentTarget)) {
        subj.value = '';
        body.value = '';
        el.querySelector('.ex-add').open = false;
      }
    });
  });
}

async function loadExamples() {
  S.examples = await api('/api/outreach-examples');
  renderExamples();
}

function renderExamples() {
  $$('.examples').forEach((el) => {
    const mine = S.examples.filter((e) => e.kind === el.dataset.kind);
    el.querySelector('.ex-count').textContent = !mine.length ? '' :
      `${mine.length} stored${mine.length > EX_LIMIT ? ` · newest ${EX_LIMIT} used` : ''}`;
    const list = el.querySelector('.ex-list');
    list.innerHTML = !mine.length
      ? '<p class="hint">None yet. Save a draft you like, or paste one below.</p>'
      : mine.map((e, i) => `
        <div class="exp-item${i >= EX_LIMIT ? ' unused' : ''}">
          <div class="bd">
            ${e.subject ? `<div class="nm">${esc(e.subject)}</div>` : ''}
            <div class="tx">${esc(e.body)}</div>
          </div>
          <button class="tiny del" data-id="${e.id}">Delete</button>
        </div>`).join('');
    list.querySelectorAll('.del').forEach((b) =>
      b.addEventListener('click', () => deleteExample(+b.dataset.id)));
  });
}

/** True when the example was stored, so the caller can clear its inputs. */
async function addExample(kind, subject, body, btn) {
  if (!body.trim()) { toast('Nothing to save: the message is empty.', 'err'); return false; }
  if (S.examples.some((e) => e.kind === kind && e.body === body.trim())) {
    toast('That message is already saved as an example.', 'err');
    return false;
  }
  busy(btn, true, 'Saving…');
  try {
    const e = await api('/api/outreach-examples', {
      method: 'POST', body: JSON.stringify({ kind, subject, body }),
    });
    S.examples.unshift(e);
    renderExamples();
    toast('Example saved. The next draft will follow its voice.', 'ok');
    return true;
  } catch (err) {
    toast(err.message, 'err');
    return false;
  } finally {
    busy(btn, false);
  }
}

async function deleteExample(id) {
  if (!confirm('Delete this example? Future drafts stop using it.')) return;
  try {
    await api(`/api/outreach-examples/${id}`, { method: 'DELETE' });
    S.examples = S.examples.filter((e) => e.id !== id);
    renderExamples();
    toast('Example deleted.', 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function copyText(text, what) {
  if (!text.trim()) return toast('Nothing to copy yet.', 'err');
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied.`, 'ok', 2200);
  } catch (e) {
    toast('Could not reach the clipboard. Select the text and copy it manually.', 'err');
  }
}

// ── wiring ──────────────────────────────────────────────────
$$('.step').forEach((b) => b.addEventListener('click', () => setStep(+b.dataset.step)));
$('#drop').addEventListener('click', () => $('#file').click());
$('#file').addEventListener('change', (e) => e.target.files[0] && upload(e.target.files[0]));
['dragover', 'dragleave', 'drop'].forEach((ev) =>
  $('#drop').addEventListener(ev, (e) => {
    e.preventDefault();
    $('#drop').classList.toggle('over', ev === 'dragover');
    if (ev === 'drop' && e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  }));
$('#addExp').addEventListener('click', (e) => addExperience(e.currentTarget));
$('#openSettings').addEventListener('click', openSettings);
$('#closeSettings').addEventListener('click', dismissSettings);
$('#settings').addEventListener('click', (e) => { if (e.target.id === 'settings') dismissSettings(); });
$('#backendModel').addEventListener('input', () => { S.discardSettings = false; });
$('#reloadStep').addEventListener('click', (e) => reloadStep(e.currentTarget));
$('#viewPrompt').addEventListener('click', openPrompt);
$('#closePrompt').addEventListener('click', closePrompt);
$('#promptDlg').addEventListener('click', (e) => { if (e.target.id === 'promptDlg') closePrompt(); });
$$('#promptTabs button').forEach((b) => b.addEventListener('click', async () => {
  await flushPrompts();
  S.promptKind = b.dataset.kind;
  renderPromptEditors();
  loadPromptPreview();
}));
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#settings').classList.contains('hidden')) dismissSettings();
  else if (!$('#promptDlg').classList.contains('hidden')) closePrompt();
});
$('#firstName').addEventListener('change', async (e) => {
  try {
    const r = await api('/api/settings', { method: 'POST', body: JSON.stringify({ first_name: e.target.value }) });
    e.target.value = r.first_name;
    toast('Filename prefix saved.', 'ok', 1800);
  } catch (err) { toast(err.message, 'err'); }
});
$('#saveBackend').addEventListener('click', (e) => saveBackend(e.currentTarget));
$('#testBackend').addEventListener('click', (e) => testBackend(e.currentTarget));
$$('#themeSeg button').forEach((b) => b.addEventListener('click', () => {
  localStorage.setItem(THEME_KEY, b.dataset.theme);
  applyTheme(b.dataset.theme);
}));
// "System" follows the OS live, not just at load.
darkQuery.addEventListener('change', () => applyTheme(themeChoice()));
$('#parseJd').addEventListener('click', (e) => parseJd(e.target));
$('#mode').addEventListener('change', (e) => {
  $('#modeHint').textContent = MODE_HINTS[e.target.value];
  if (S.job) saveJobField('mode', e.target.value);
});
$('#company').addEventListener('change', (e) => saveJobField('company', e.target.value));
$('#role').addEventListener('change', (e) => saveJobField('role', e.target.value));
$('#runTransform').addEventListener('click', (e) => runTransform(e.target, null, ''));
$('#acceptAll').addEventListener('click', async () => {
  S.job.rewrites = await api(`/api/jobs/${S.job.id}/accept-all`, { method: 'POST' });
  renderReview();
});
$('#replaceBox').addEventListener('change', (e) => setReplace(e.target.checked));
$('#toggleSkipped').addEventListener('click', () => { S.showSkipped = !S.showSkipped; renderMap(); });
$('#toExport').addEventListener('click', () => setStep(5));
$('#refreshPreview').addEventListener('click', refreshPreview);
$('#download').addEventListener('click', (e) => doExport(e.target));
$('#toOutreach').addEventListener('click', () => setStep(6));
$('#chatSend').addEventListener('click', () => sendChat());
$('#chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendChat();
  }
});
$('#makeOutreach').addEventListener('click', (e) => makeOutreach(e.target));
$('#liText').addEventListener('input', syncOutreach);
['#emSubject', '#emBody'].forEach((s) => $(s).addEventListener('input', syncOutreach));
['#recruiter', '#liSubject', '#liText', '#emSubject', '#emBody'].forEach((s) =>
  $(s).addEventListener('change', saveOutreach));
$('#copyLiSubject').addEventListener('click', () => copyText($('#liSubject').value, 'LinkedIn subject'));
$('#copyLi').addEventListener('click', () => copyText($('#liText').value, 'LinkedIn note'));
$$('.save-ex').forEach((b) => b.addEventListener('click', () => {
  const li = b.dataset.kind === 'linkedin';
  addExample(b.dataset.kind, $(li ? '#liSubject' : '#emSubject').value,
    $(li ? '#liText' : '#emBody').value, b);
}));
$('#copyEmail').addEventListener('click', () =>
  copyText(`Subject: ${$('#emSubject').value}\n\n${$('#emBody').value}`, 'Email'));

(async function init() {
  $('#modeHint').textContent = MODE_HINTS.aggressive;
  buildExampleSlots();
  await loadStatus();
  await loadResumes();
  await loadExperiences();
  await loadExamples();
  const step = await restoreSession().catch(() => 1);
  await loadResumes();   // again, now that a restored resume can be marked selected
  renderResumePreview();
  setStep(step);
  if (!S.status.ready) {
    toast(S.status.detail || 'No model backend available.', 'err', 9000);
  }
})();
