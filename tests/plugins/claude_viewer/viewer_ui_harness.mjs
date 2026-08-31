#!/usr/bin/env node
/*
 * Behavior harness for the claude-viewer inline UI script.
 *
 * Executes the real <script> block from plugins/claude_viewer/viewer/ui.html
 * in a fresh Node vm per scenario, against a minimal DOM stub whose layout
 * is tall enough for #transcript to overflow and scroll. Each scenario
 * reports named pass/fail verdicts as one JSON object on stdout; the exit
 * code is 1 when any check failed.
 *
 * The HTML path is an argument (never hardcoded) so the same harness can be
 * pointed at a pre-fix or mutated copy of ui.html to prove the checks bite.
 *
 * Usage: node viewer_ui_harness.mjs <ui.html> [scenario ...]
 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';

/* ── stub layout model ────────────────────────────────────────────────
 * Deterministic geometry: every rendered event row is one fixed height,
 * the welcome box and prompt card get their own heights, and #transcript
 * is a 600px scroller with the same padding shape as the real CSS. With
 * ~40 head events the transcript overflows far enough that "aimed at the
 * prompt card" and "scrolled to the tail" are ~2,000px apart. */
const PAD_TOP = 46;
const PAD_BOTTOM = 36;
const CLIENT_HEIGHT = 600;
const ROW_HEIGHT = 60;
const CLASS_HEIGHTS = { 'welcome-box': 200, 'prompt-card': 120 };

const RUN_FILE = '20260831-010101.jsonl';
const RUN_STEM = '20260831-010101';

/* A second run that every no-hash auto-select fallback (first listed, newest,
 * first active) picks instead of RUN_FILE. The deep-link fixture serves both
 * runs and points the hash at RUN_FILE, so a broken runFromHash() routes to
 * OTHER_FILE and fails the scenario loudly — with a single-run fixture the
 * fallback silently selects the same run and the check stays vacuously green. */
const OTHER_FILE = '20260831-020202.jsonl';
const OTHER_STEM = '20260831-020202';

function headEvents() {
  const lines = [
    { type: 'system', subtype: 'init', model: 'claude-fable-5', cwd: '/tmp/demo' },
  ];
  for (let i = 0; i < 38; i++) {
    lines.push({
      type: 'assistant',
      message: { model: 'claude-fable-5', content: [{ type: 'text', text: 'turn ' + i }] },
    });
  }
  lines.push({
    type: 'assistant',
    message: { content: [{ type: 'tool_use', id: 'tool-1', name: 'Read', input: { file_path: '/tmp/demo/x.py' } }] },
  });
  lines.push({
    type: 'user',
    message: { content: [{ type: 'tool_result', tool_use_id: 'tool-1', content: '42 lines', is_error: false }] },
  });
  lines.push({ type: 'result', duration_ms: 65000, num_turns: 12, total_cost_usd: 0.42, is_error: false });
  return lines;
}

const tailEvent = (text) => ({
  type: 'assistant',
  message: { content: [{ type: 'text', text }] },
});

/* ── minimal DOM ────────────────────────────────────────────────────── */

class StubText {
  constructor(text) {
    this.text = String(text);
    this.parentNode = null;
    this.children = [];          // leaf: layout sees through it
    this._height = () => 0;      // its text folds into the parent row's height
  }
  get textContent() { return this.text; }
}

function matchSelector(el, selector) {
  if (!(el instanceof StubEl)) return false;
  const tokens = String(selector).match(/[#.\[]?[^#.\[]+/g) || [];
  return tokens.every((token) => {
    if (token[0] === '#') return el.id === token.slice(1);
    if (token[0] === '.') return el._classSet.has(token.slice(1));
    if (token[0] === '[') {
      const m = token.match(/^\[\s*([\w-]+)\s*=\s*"([^"]*)"\s*\]$/);
      if (!m) return false;
      const key = m[1].replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase());
      return String(el.dataset[key] == null ? '' : el.dataset[key]) === m[2];
    }
    return el.tagName === token.toUpperCase();
  });
}

let elementSeq = 0;

class StubEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.id = '';
    this.children = [];
    this.parentNode = null;
    this._classSet = new Set();
    this.dataset = {};
    this.style = { cssText: '' };
    this.title = '';
    this._ownText = '';
    this._listeners = new Map();
    this._seq = elementSeq++;
    this._scrollTop = 0;
    this._scroller = false;
    this._scrollWrites = [];     // {value, max} per assignment, in order
    this.getBoundingClientRect = () => ({ width: 324, height: 12 });
  }

  get className() { return [...this._classSet].join(' '); }
  set className(v) {
    this._classSet = new Set(String(v).split(/\s+/).filter(Boolean));
  }

  get classList() {
    const set = this._classSet;
    return {
      add: (...cs) => { cs.forEach((c) => set.add(c)); },
      remove: (...cs) => { cs.forEach((c) => set.delete(c)); },
      toggle: (c, force) => {
        const on = force === undefined ? !set.has(c) : Boolean(force);
        if (on) set.add(c); else set.delete(c);
        return on;
      },
      contains: (c) => set.has(c),
    };
  }

  get textContent() {
    let out = this._ownText;
    for (const child of this.children) out += child.textContent == null ? '' : child.textContent;
    return out;
  }
  set textContent(v) {
    this._detachChildren();
    this._ownText = v == null ? '' : String(v);
  }

  set innerHTML(_v) { this._detachChildren(); this._ownText = ''; }

  _detachChildren() {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
  }

  appendChild(node) {
    this._take(node);
    node.parentNode = this;
    this.children.push(node);
    return node;
  }

  insertBefore(node, ref) {
    this._take(node);
    node.parentNode = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(node);
    else this.children.splice(at, 0, node);
    return node;
  }

  remove() { this._take(this); }

  _take(node) {
    const parent = node.parentNode;
    if (!parent) return;
    const at = parent.children.indexOf(node);
    if (at >= 0) parent.children.splice(at, 1);
    node.parentNode = null;
  }

  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    const parent = this.parentNode;
    if (!parent) return null;
    const at = parent.children.indexOf(this);
    return at < 0 ? null : parent.children[at + 1] || null;
  }

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }

  _emit(type, evt) {
    evt = evt || {};
    evt.type = type;
    evt.target = evt.target || this;
    if (!evt.preventDefault) evt.preventDefault = () => {};
    for (const fn of this._listeners.get(type) || []) fn(evt);
    return true;
  }

  querySelector(selector) {
    return this._descendants().find((d) => matchSelector(d, selector)) || null;
  }
  querySelectorAll(selector) {
    return this._descendants().filter((d) => matchSelector(d, selector));
  }
  _descendants() {
    const out = [];
    const walk = (el) => {
      for (const child of el.children) { out.push(child); walk(child); }
    };
    walk(this);
    return out;
  }

  /* height of one rendered row — fixed per class so offsets are predictable */
  _height() {
    for (const [cls, h] of Object.entries(CLASS_HEIGHTS)) {
      if (this._classSet.has(cls)) return h;
    }
    return ROW_HEIGHT;
  }

  _nearestScroller() {
    let parent = this.parentNode;
    while (parent) {
      if (parent._scroller) return parent;
      parent = parent.parentNode;
    }
    return null;
  }

  /* offsetParent is the positioned #transcript-wrap; #transcript is its
   * first child (offsetTop 0), so a transcript child's offsetTop counts
   * the scroller's top padding plus every row above it. */
  get offsetTop() {
    const scroller = this._nearestScroller();
    if (!scroller) return 0;
    let y = scroller.offsetTop + PAD_TOP;
    for (const sibling of scroller.children) {
      if (sibling === this) return y;
      y += sibling._height();
    }
    return y;
  }

  get scrollHeight() {
    if (!this._scroller) return this._height();
    let h = PAD_TOP + PAD_BOTTOM;
    for (const child of this.children) h += child._height();
    return h;
  }
  get clientHeight() { return this._scroller ? CLIENT_HEIGHT : this._height(); }
  get clientWidth() { return this._scroller ? 820 : 800; }

  get scrollTop() { return this._scrollTop; }
  set scrollTop(v) {
    const max = Math.max(0, this.scrollHeight - this.clientHeight);
    this._scrollTop = Math.min(Math.max(0, Number(v) || 0), max);
    if (this._scroller) this._scrollWrites.push({ value: this._scrollTop, max });
    // Real browsers dispatch 'scroll' asynchronously after the assignment.
    if (this._scroller && (this._listeners.get('scroll') || []).length) {
      setTimeout(() => this._emit('scroll', { target: this }), 0);
    }
  }

  /* Assignments that landed on the clamp ceiling — i.e. the viewport was
   * pushed to the very tail. The initial prompt load must produce none:
   * the only allowed write is the reveal aiming at the prompt card. */
  _bottomWriteCount() {
    return this._scrollWrites.filter((w) => w.max > 0 && w.value === w.max).length;
  }
}

/* ── world: document + fetch script for one scenario ────────────────── */

function makeWorld(scenario) {
  const requests = [];
  const replaceStates = [];
  const intervals = [];
  const tailQueue = [];
  const tailWaiters = [];

  const takeTail = () => {
    if (tailQueue.length) return Promise.resolve(tailQueue.shift());
    return new Promise((resolve) => tailWaiters.push(resolve));
  };
  const deliverTail = (lines) => {
    const waiter = tailWaiters.shift();
    if (waiter) waiter(lines);
    else tailQueue.push(lines);
  };

  const ids = {};
  const byId = (id) => {
    const el = new StubEl('div');
    el.id = id;
    ids[id] = el;
    return el;
  };
  for (const id of [
    'run-list', 'transcript', 'status-bar', 'history-chip', 'new-pill',
    'pause-btn', 'spinner-line', 'spin-text', 'input-box', 'input-area',
  ]) byId(id);
  ids.transcript._scroller = true;

  const docListeners = new Map();
  const doc = {
    createElement: (tag) => new StubEl(tag),
    createTextNode: (text) => new StubText(text),
    querySelector: (sel) => (sel.startsWith('#') ? ids[sel.slice(1)] || null : null),
    querySelectorAll: () => [],
    addEventListener: (type, fn) => {
      if (!docListeners.has(type)) docListeners.set(type, []);
      docListeners.get(type).push(fn);
    },
    emitDocumentEvent: (type, evt) => {
      for (const fn of docListeners.get(type) || []) fn(evt);
    },
  };

  let tailOffset = 4096;
  const head = headEvents();
  /* /api/runs fixture, one active run by default. A scenario may pass several
   * runs (with per-run /api/head prompts) so the no-hash fallback and a hash
   * route can land on different runs — see promptDeepLinkReload. */
  const runsFixture = scenario.runs
    || [{ file: RUN_FILE, active: true, mtime: 1777000000, prompt: scenario.prompt }];
  const runsPayload = runsFixture.map((r) => ({
    file: r.file, size: 9000, mtime: r.mtime || 1777000000,
    active: !!r.active, project: 'demo', title: 'Demo run',
  }));
  const promptForFile = (url) => {
    const run = runsFixture.find((r) => url.includes('file=' + encodeURIComponent(r.file)));
    return run && run.prompt != null ? run.prompt : '';
  };
  const route = async (url) => {
    const u = String(url);
    if (u.startsWith('/api/runs')) {
      return { now_ms: 1777000000000, runs: runsPayload };
    }
    if (u.startsWith('/api/head')) {
      if (u.includes('before=')) {
        return { lines: [], before: 0, has_more: false, prompt: promptForFile(u) };
      }
      return { lines: head, before: 4096, has_more: true, prompt: promptForFile(u), total_lines: head.length };
    }
    if (u.startsWith('/api/tail')) {
      if (!u.includes('wait=1')) return { offset: 4096 };   // the getFileSize probe
      const lines = await takeTail();
      tailOffset += 512;
      return { offset: tailOffset, lines };
    }
    throw new Error('harness fetch: unexpected url ' + u);
  };
  const fetchFn = (url) => {
    requests.push(String(url));
    return Promise.resolve({ json: () => route(url) });
  };

  const sandbox = {
    document: doc,
    window: { addEventListener() {} },
    location: { hash: scenario.hash || '' },
    history: { replaceState: (_state, _title, url) => { replaceStates.push(String(url)); } },
    fetch: fetchFn,
    requestAnimationFrame: (cb) => setTimeout(cb, 0),
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => {
      const id = setInterval(fn, ms);
      intervals.push(id);
      return id;
    },
    clearInterval,
    getComputedStyle: () => ({ paddingLeft: PAD_TOP + 'px', paddingRight: '24px' }),
    console: { log() {}, warn() {}, error() {} },
  };
  vm.createContext(sandbox);

  return {
    sandbox, doc,
    transcript: ids.transcript,
    newPill: ids['new-pill'],
    requests, replaceStates, deliverTail,
    bottom: () => ids.transcript.scrollHeight - ids.transcript.clientHeight,
    rows: () => ids.transcript.children.length,
    stopTimers: () => intervals.forEach((id) => clearInterval(id)),
  };
}

/* ── scenarios ──────────────────────────────────────────────────────── */

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(label, predicate, timeoutMs = 5000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) {
      throw new Error('timeout waiting for: ' + label);
    }
    await sleep(10);
  }
}

/* Settled = the head batch has rendered, loadHead's reveal window has
 * elapsed, and the tail long-poll is in flight (it only starts after the
 * reveal, so its arrival proves the initial load sequence completed). */
async function settle(world) {
  await waitFor('first long-poll tail request', () =>
    world.requests.some((r) => r.includes('/api/tail') && r.includes('wait=1')));
  await sleep(30);   // let the reveal's asynchronous scroll event land
}

const EXPECTED_CARD_TOP = PAD_TOP + CLASS_HEIGHTS['welcome-box'];

async function promptFirstLoad(script, check) {
  const world = makeWorld({ prompt: 'fix the login loop' });
  vm.runInContext(script, world.sandbox);
  try {
    await settle(world);

    const card = world.transcript.querySelector('.prompt-card');
    check('prompt_card_is_in_view',
      !!card && world.transcript.scrollTop === EXPECTED_CARD_TOP && card.offsetTop === EXPECTED_CARD_TOP,
      `scrollTop=${world.transcript.scrollTop} cardTop=${card ? card.offsetTop : 'none'} expected=${EXPECTED_CARD_TOP}`);
    check('viewport_is_not_at_bottom',
      world.transcript.scrollTop !== world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
    check('no_tail_flash_before_reveal',
      world.transcript._bottomWriteCount() === 0,
      `initial load must not visit the tail before the reveal; writes: ${JSON.stringify(world.transcript._scrollWrites)}`);
    check('no_history_page_from_reveal',
      !world.requests.some((r) => r.includes('before=')),
      `reveal must not page older history; requests: ${world.requests.join(' | ')}`);

    // Streamed events after the reveal stay offscreen behind the pill.
    let rows = world.rows();
    world.deliverTail([tailEvent('streamed A'), tailEvent('streamed B')]);
    await waitFor('tail batch rendered', () => world.rows() === rows + 2);
    check('tail_events_stay_offscreen',
      world.transcript.scrollTop === EXPECTED_CARD_TOP,
      `scrollTop=${world.transcript.scrollTop} expected=${EXPECTED_CARD_TOP}`);
    check('pill_advertises_new_lines',
      world.newPill.classList.contains('visible') && /new lines/.test(world.newPill.textContent),
      `visible=${world.newPill.classList.contains('visible')} text=${JSON.stringify(world.newPill.textContent)}`);

    // G jumps to the tail and tail-follow resumes from there.
    world.doc.emitDocumentEvent('keydown', {
      code: 'KeyG', target: { tagName: 'BODY' }, preventDefault() {},
    });
    await sleep(30);
    check('key_g_jumps_to_tail',
      world.transcript.scrollTop === world.bottom() && !world.newPill.classList.contains('visible'),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);

    rows = world.rows();
    world.deliverTail([tailEvent('streamed C')]);
    await waitFor('post-G tail batch rendered', () => world.rows() === rows + 1);
    check('tail_follow_resumes_after_jump',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);

    // Scrolling back up re-arms the pill; clicking the pill re-enters follow.
    world.transcript.scrollTop = EXPECTED_CARD_TOP;
    await sleep(30);
    rows = world.rows();
    world.deliverTail([tailEvent('streamed D')]);
    await waitFor('post-scroll-up tail batch rendered', () => world.rows() === rows + 1);
    check('scrolled_up_keeps_new_events_offscreen',
      world.transcript.scrollTop === EXPECTED_CARD_TOP && world.newPill.classList.contains('visible'),
      `scrollTop=${world.transcript.scrollTop} pillVisible=${world.newPill.classList.contains('visible')}`);

    world.newPill._emit('click');
    await sleep(30);
    check('pill_click_jumps_to_tail',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
  } finally {
    world.stopTimers();
  }
}

async function promptDeepLinkReload(script, check) {
  /* Cold reload with #<RUN_STEM> in the URL, against two runs whose
   * auto-select default (first active run) is OTHER_FILE. Only the hash
   * itself can select RUN_FILE here, so the scenario fails if runFromHash()
   * is broken — and the checks below pin the requested run's data, not just
   * "some run loaded". */
  const world = makeWorld({
    hash: '#' + RUN_STEM,
    runs: [
      { file: OTHER_FILE, active: true, mtime: 1777000500, prompt: '' },
      { file: RUN_FILE, active: false, mtime: 1777000000, prompt: 'fix the login loop' },
    ],
  });
  vm.runInContext(script, world.sandbox);
  try {
    await settle(world);

    const headRequests = world.requests.filter((r) => r.startsWith('/api/head'));
    check('hashed_run_was_loaded',
      headRequests.length > 0 && headRequests.every((r) => r.includes('file=' + RUN_FILE)),
      `expected every /api/head to load ${RUN_FILE}; requests: ${headRequests.join(' | ')}`);
    check('hash_routed_to_the_run',
      world.replaceStates.length > 0
        && world.replaceStates[world.replaceStates.length - 1] === '#' + RUN_STEM
        && !world.replaceStates.includes('#' + OTHER_STEM),
      `replaceState urls: ${world.replaceStates.join(' | ')}`);
    const card = world.transcript.querySelector('.prompt-card');
    check('prompt_card_is_in_view',
      !!card && /fix the login loop/.test(card.textContent)
        && world.transcript.scrollTop === EXPECTED_CARD_TOP && card.offsetTop === EXPECTED_CARD_TOP,
      `scrollTop=${world.transcript.scrollTop} expected=${EXPECTED_CARD_TOP} card=${!!card} text=${card ? JSON.stringify(card.textContent) : 'none'}`);
    check('viewport_is_not_at_bottom',
      world.transcript.scrollTop !== world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
    check('no_tail_flash_before_reveal',
      world.transcript._bottomWriteCount() === 0,
      `initial load must not visit the tail before the reveal; writes: ${JSON.stringify(world.transcript._scrollWrites)}`);
  } finally {
    world.stopTimers();
  }
}

async function noPromptTailFollow(script, check) {
  const world = makeWorld({ prompt: '' });
  vm.runInContext(script, world.sandbox);
  try {
    await settle(world);
    check('first_render_lands_at_bottom',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
    check('pill_hidden_after_first_render',
      !world.newPill.classList.contains('visible'),
      `pillVisible=${world.newPill.classList.contains('visible')}`);

    let rows = world.rows();
    world.deliverTail([tailEvent('streamed A'), tailEvent('streamed B')]);
    await waitFor('tail batch rendered', () => world.rows() === rows + 2);
    check('tail_stays_followed',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
  } finally {
    world.stopTimers();
  }
}

const SCENARIOS = {
  prompt_first_load: promptFirstLoad,
  prompt_deep_link_reload: promptDeepLinkReload,
  no_prompt_tail_follow: noPromptTailFollow,
};

/* ── entry point ────────────────────────────────────────────────────── */

function extractInlineScript(html) {
  const start = html.indexOf('<script>');
  const end = html.lastIndexOf('</script>');
  if (start < 0 || end < 0 || end < start) {
    throw new Error('ui.html has no inline <script> block');
  }
  return html.slice(start + '<script>'.length, end);
}

async function main() {
  const htmlPath = process.argv[2];
  if (!htmlPath) {
    console.error('usage: node viewer_ui_harness.mjs <ui.html> [scenario ...]');
    process.exit(2);
  }
  const wanted = process.argv.slice(3);
  const names = wanted.length ? wanted.filter((n) => {
    if (!SCENARIOS[n]) {
      console.error('unknown scenario: ' + n + ' (known: ' + Object.keys(SCENARIOS).join(', ') + ')');
      process.exit(2);
    }
    return true;
  }) : Object.keys(SCENARIOS);

  const script = extractInlineScript(readFileSync(htmlPath, 'utf8'));

  const verdicts = {};
  let failed = false;
  for (const name of names) {
    const verdict = {};
    const check = (checkName, ok, detail) => {
      verdict[checkName] = { pass: Boolean(ok), detail: String(detail) };
    };
    verdicts[name] = verdict;
    try {
      await SCENARIOS[name](script, check);
    } catch (err) {
      verdict.harness_error = { pass: false, detail: String((err && err.stack) || err) };
    }
    if (Object.values(verdict).some((v) => !v.pass)) failed = true;
  }

  process.stdout.write(JSON.stringify(verdicts));
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error(String((err && err.stack) || err));
  process.exit(2);
});
