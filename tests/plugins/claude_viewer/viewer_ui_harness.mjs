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
 * requestAnimationFrame delay is configurable via VIEWER_UI_RAF_DELAY_MS
 * (default 0) so the delayed-frame race — hidden, background, or busy tabs
 * stalling rAF well past the 100 ms render timer — can be exercised
 * deterministically, e.g. VIEWER_UI_RAF_DELAY_MS=250. Scenarios may
 * additionally hold frames outright (holdFrames + releaseFrames) and defer
 * /api/head responses (deferHeads + deferredHeads) to script overlapping
 * A→B→A loads and render coalescing with exact ordering.
 *
 * Usage: node viewer_ui_harness.mjs <ui.html> [scenario ...]
 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';

/* Deterministic rAF stall: every requestAnimationFrame callback fires this
 * many ms late. 0 keeps the historical immediate-frame behavior. */
const RAF_DELAY_MS = Math.max(0, Number(process.env.VIEWER_UI_RAF_DELAY_MS) || 0);

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

function headEvents(tag) {
  tag = tag || '';
  const lines = [
    { type: 'system', subtype: 'init', model: 'claude-fable-5', cwd: '/tmp/demo' },
  ];
  for (let i = 0; i < 38; i++) {
    lines.push({
      type: 'assistant',
      message: { model: 'claude-fable-5', content: [{ type: 'text', text: 'turn ' + (tag ? tag + '-' : '') + i }] },
    });
  }
  lines.push({
    type: 'assistant',
    message: { content: [{ type: 'tool_use', id: 'tool-1', name: 'Read', input: { file_path: '/tmp/demo/x.py' } }] },
  });
  lines.push({
    type: 'user',
    message: { content: [{ type: 'tool_result', tool_use_id: 'tool-1', content: tag + '42 lines', is_error: false }] },
  });
  lines.push({ type: 'result', duration_ms: 65000, num_turns: 12, total_cost_usd: 0.42, is_error: false });
  return lines;
}

/* A full /api/head payload for a deferred head response. The tag makes every
 * rendered line unique to that response, so a DOM containing 'turn A1-0'
 * proves a stale load painted and a DOM missing it proves it did not. The
 * tail_offset mirrors the real server: the tail cursor captured with the
 * head snapshot. */
function headPayload(tag, prompt) {
  const lines = headEvents(tag);
  return { lines, before: 4096, has_more: true, prompt, total_lines: lines.length, tail_offset: 4096 };
}

/* Byte-accurate virtual jsonl file: every event knows its exact start offset,
 * so /api/head can snapshot (lines + tail_offset) together and /api/tail can
 * honestly return only events starting at or after the requested offset. A UI
 * that anchors its tail at a later size probe skips events appended between
 * the snapshot and the probe — the gap this model exists to expose. */
function makeFileModel(initialEvents) {
  const entries = [];          // { start, end, obj }
  let size = 0;
  const lineBytes = (obj) => Buffer.byteLength(JSON.stringify(obj), 'utf8') + 1;
  const append = (obj) => {
    const len = lineBytes(obj);
    entries.push({ start: size, end: size + len, obj });
    size += len;
  };
  for (const ev of initialEvents) append(ev);
  return {
    append,
    size: () => size,
    linesFrom: (offset) => entries.filter((e) => e.start >= offset).map((e) => e.obj),
  };
}

/* Rows a head payload adds to the transcript: the init event only sets
 * state, everything else renders one row. */
function headEventRows(payload) {
  return payload.lines.filter((ev) => !(ev.type === 'system' && ev.subtype === 'init')).length;
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
  /* scenario.deferHeads: /api/head responses are captured here instead of
   * resolving, so the scenario can settle overlapping loads in an
   * adversarial order. Each entry: { url, resolve(payload) }. */
  const deferredHeads = [];
  /* scenario.holdFrames: requestAnimationFrame callbacks queue up instead of
   * firing, so the scenario decides exactly when the pending frame runs —
   * a deterministic "delayed frame" (hidden/background/busy tab). */
  const frameQueue = [];
  let framesHeld = !!scenario.holdFrames;

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
  /* scenario.virtualFile: a byte-accurate jsonl model (see makeFileModel)
   * backing RUN_FILE. /api/head snapshots lines+tail_offset together;
   * /api/tail serves exactly the events starting at or after the requested
   * offset, and world.appendEvents() appends after the snapshot. */
  const model = scenario.virtualFile ? makeFileModel(scenario.virtualFile) : null;
  let capturedTailOffset = null;   // tail_offset served with the head snapshot
  /* /api/runs fixture, one active run by default. A scenario may pass several
   * runs (with per-run /api/head prompts) so the no-hash fallback and a hash
   * route can land on different runs — see promptDeepLinkReload. A run with
   * `empty: true` gets an empty first page from /api/head. */
  const runsFixture = scenario.runs
    || [{ file: RUN_FILE, active: true, mtime: 1777000000, prompt: scenario.prompt }];
  const runsPayload = runsFixture.map((r) => ({
    file: r.file, size: 9000, mtime: r.mtime || 1777000000,
    active: !!r.active, project: 'demo', title: 'Demo run',
  }));
  const runForUrl = (url) =>
    runsFixture.find((r) => url.includes('file=' + encodeURIComponent(r.file)));
  const promptForFile = (url) => {
    const run = runForUrl(url);
    return run && run.prompt != null ? run.prompt : '';
  };
  const route = async (url) => {
    const u = String(url);
    if (u.startsWith('/api/runs')) {
      return { now_ms: 1777000000000, runs: runsPayload };
    }
    if (u.startsWith('/api/head')) {
      const run = runForUrl(u);
      if (u.includes('before=')) {
        // History page: a mid-file window, so no tail cursor rides along.
        return { lines: [], before: 0, has_more: false, prompt: promptForFile(u) };
      }
      if (model) {
        // Snapshot semantics: the page and its tail cursor are captured
        // together, at response time. Events appended after this response
        // start exactly at tail_offset.
        const lines = model.linesFrom(0);
        capturedTailOffset = model.size();
        return { lines, before: 0, has_more: false, prompt: promptForFile(u), total_lines: lines.length, tail_offset: capturedTailOffset };
      }
      // First page: the tail cursor captured with the head snapshot rides
      // along — except in legacyHead mode, which withholds the field to pin
      // the size-probe compatibility fallback.
      const cursor = scenario.legacyHead ? {} : { tail_offset: 4096 };
      if (run && run.empty) {
        return { lines: [], before: 0, has_more: false, prompt: promptForFile(u), total_lines: 0, ...cursor };
      }
      return { lines: head, before: 4096, has_more: true, prompt: promptForFile(u), total_lines: head.length, ...cursor };
    }
    if (u.startsWith('/api/tail')) {
      const offMatch = u.match(/[?&]offset=(\d+)/);
      const off = offMatch ? Number(offMatch[1]) : 0;
      if (model) {
        if (!u.includes('wait=1')) return { offset: model.size() };   // legacy probe: the LATER size
        // Honest tail: only events starting at or after the requested offset.
        // Anything earlier was covered by the head snapshot — or is skipped
        // forever by a UI that anchored its cursor at a later size probe.
        let lines = model.linesFrom(off);
        while (!lines.length) {
          await new Promise((resolve) => tailWaiters.push(resolve));
          lines = model.linesFrom(off);
        }
        return { offset: model.size(), lines };
      }
      if (!u.includes('wait=1')) return { offset: 4096 };   // the getFileSize probe
      const lines = await takeTail();
      tailOffset += 512;
      return { offset: tailOffset, lines };
    }
    throw new Error('harness fetch: unexpected url ' + u);
  };
  const fetchFn = (url) => {
    requests.push(String(url));
    if (scenario.deferHeads && String(url).startsWith('/api/head')) {
      return new Promise((resolve) => {
        deferredHeads.push({
          url: String(url),
          resolve: (payload) => resolve({ json: () => Promise.resolve(payload) }),
        });
      });
    }
    return Promise.resolve({ json: () => route(url) });
  };

  const sandbox = {
    document: doc,
    window: { addEventListener() {} },
    location: { hash: scenario.hash || '' },
    history: { replaceState: (_state, _title, url) => { replaceStates.push(String(url)); } },
    fetch: fetchFn,
    requestAnimationFrame: (cb) => {
      if (framesHeld) { frameQueue.push(cb); return; }
      setTimeout(cb, RAF_DELAY_MS);
    },
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
    runList: ids['run-list'],
    requests, replaceStates, deliverTail,
    deferredHeads,
    /* virtualFile mode: append events to the model AFTER the head snapshot
     * and wake one parked long-poll, which then rereads from its offset. */
    appendEvents: (events) => {
      for (const ev of events) model.append(ev);
      const waiter = tailWaiters.shift();
      if (waiter) waiter([]);
    },
    headTailOffset: () => capturedTailOffset,
    heldFrames: () => frameQueue.length,
    /* Fire every held frame, still honoring RAF_DELAY_MS: releasing gates
     * WHEN the frames become eligible, the delay keeps them stalled past
     * the render timer like a hidden/background tab. Releasing also
     * un-stalls the tab — later frames fire on their own, so tail batches
     * render after the held head flush; holdFrames() re-arms the stall for
     * scenarios with a second held window. */
    releaseFrames: () => {
      framesHeld = false;
      const queued = frameQueue.splice(0);
      for (const cb of queued) setTimeout(cb, RAF_DELAY_MS);
    },
    holdFrames: () => { framesHeld = true; },
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

/* Settled = the head batch has rendered and the reveal has run. The tail
 * long-poll only starts after loadHead() has awaited the actual head flush
 * and revealed, so its arrival proves the initial load sequence completed. */
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
    /* The head batch is history, not live output: after the reveal the pill
     * stays hidden with nothing counted, so the first streamed lines below
     * must read exactly "2 new lines", not head-size + 2. */
    check('initial_head_not_counted_as_new',
      !world.newPill.classList.contains('visible'),
      `pill visible after initial head batch: text=${JSON.stringify(world.newPill.textContent)}`);

    // Streamed events after the reveal stay offscreen behind the pill.
    let rows = world.rows();
    world.deliverTail([tailEvent('streamed A'), tailEvent('streamed B')]);
    await waitFor('tail batch rendered', () => world.rows() === rows + 2);
    check('tail_events_stay_offscreen',
      world.transcript.scrollTop === EXPECTED_CARD_TOP,
      `scrollTop=${world.transcript.scrollTop} expected=${EXPECTED_CARD_TOP}`);
    check('pill_advertises_new_lines',
      world.newPill.classList.contains('visible') && /↓ 2 new lines/.test(world.newPill.textContent),
      `pill must count exactly the 2 streamed lines; visible=${world.newPill.classList.contains('visible')} text=${JSON.stringify(world.newPill.textContent)}`);

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

/* Follow state must not leak across run selections in one session: open a
 * prompt-bearing run (which legitimately leaves the viewer off-tail), then
 * switch to a no-prompt run through the sidebar row click — the second run
 * has to land on and keep following its tail with no new-lines pill. A
 * fresh-VM no-prompt scenario cannot see this: the leak only exists because
 * the first run left userNearBottom false. */
async function promptThenNoPromptSwitch(script, check) {
  const world = makeWorld({
    runs: [
      { file: RUN_FILE, active: true, mtime: 1777000500, prompt: 'fix the login loop' },
      { file: OTHER_FILE, active: false, mtime: 1777000000, prompt: '' },
    ],
  });
  vm.runInContext(script, world.sandbox);
  try {
    await settle(world);
    check('prompt_run_starts_off_tail',
      world.transcript.scrollTop === EXPECTED_CARD_TOP
        && world.transcript.scrollTop !== world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()} expected=${EXPECTED_CARD_TOP}`);

    const row = world.runList.querySelectorAll('.run-row')
      .find((r) => r.dataset.file === OTHER_FILE);
    if (!row) throw new Error('sidebar row for ' + OTHER_FILE + ' not rendered');
    row._emit('click');

    await waitFor('head request for the no-prompt run', () =>
      world.requests.some((r) => r.startsWith('/api/head') && r.includes('file=' + OTHER_FILE)));
    await waitFor('no-prompt run tail poll', () => {
      const headAt = world.requests.findIndex((r) =>
        r.startsWith('/api/head') && r.includes('file=' + OTHER_FILE));
      return world.requests.slice(headAt).some((r) => r.includes('/api/tail') && r.includes('wait=1'));
    });
    await sleep(30);   // let the first flush and its scroll event land

    check('no_prompt_run_lands_at_tail',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
    check('no_prompt_run_has_no_pill',
      !world.newPill.classList.contains('visible'),
      `pillVisible=${world.newPill.classList.contains('visible')} text=${JSON.stringify(world.newPill.textContent)}`);

    /* The superseded run's long-poll is still parked ahead of the new run's
     * poll, so the first delivery lands on the dead poll (dropped there) and
     * the second one reaches the live tail. */
    world.deliverTail([tailEvent('stale drain')]);
    await sleep(20);
    const rows = world.rows();
    world.deliverTail([tailEvent('follow me')]);
    await waitFor('switched-run tail batch rendered', () => world.rows() === rows + 1);
    check('no_prompt_run_follows_tail',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
  } finally {
    world.stopTimers();
  }
}

/* ── load-overlap scenarios (generation identity) ───────────────────── */

function clickRunRow(world, file) {
  const row = world.runList.querySelectorAll('.run-row')
    .find((r) => r.dataset.file === file);
  if (!row) throw new Error('sidebar row for ' + file + ' not rendered');
  row._emit('click');
}

const tailPolls = (world) =>
  world.requests.filter((r) => r.includes('/api/tail') && r.includes('wait=1'));
const sizeProbes = (world) =>
  world.requests.filter((r) => r.includes('/api/tail') && !r.includes('wait=1'));

/* An empty first page schedules no flush, so there is nothing to wait for:
 * the load must settle straight into the size probe and the tail long-poll
 * instead of hanging on a head flush that never comes. */
async function emptyHeadSettles(script, check) {
  const world = makeWorld({
    runs: [{ file: RUN_FILE, active: true, mtime: 1777000000, prompt: '', empty: true }],
  });
  vm.runInContext(script, world.sandbox);
  try {
    await settle(world);   // times out loudly if the empty head hangs the load
    check('empty_head_starts_tail_polling',
      tailPolls(world).length === 1 && sizeProbes(world).length === 0,
      `polls=${tailPolls(world).length} probes=${sizeProbes(world).length} (the head snapshot carries the tail cursor, so no size probe); requests: ${world.requests.join(' | ')}`);
    check('empty_head_has_no_prompt_card',
      !world.transcript.querySelector('.prompt-card'),
      'an empty, prompt-less head must not render a prompt card');
    check('empty_head_hides_the_pill',
      !world.newPill.classList.contains('visible'),
      `pillVisible=${world.newPill.classList.contains('visible')} text=${JSON.stringify(world.newPill.textContent)}`);

    // Live lines arriving after the empty head render and follow normally.
    const rows = world.rows();
    world.deliverTail([tailEvent('streamed A'), tailEvent('streamed B')]);
    await waitFor('post-empty-head tail batch rendered', () => world.rows() === rows + 2);
    check('tail_follows_after_empty_head',
      world.transcript.scrollTop === Math.max(0, world.bottom())
        && !world.newPill.classList.contains('visible'),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()} pillVisible=${world.newPill.classList.contains('visible')}`);
  } finally {
    world.stopTimers();
  }
}

/* A prior scheduled frame already exists when the current run's head batch
 * is scheduled (the previous load's frame is still pending, so
 * scheduleRender coalesces instead of queueing a second frame). The current
 * generation's waiter must still resolve when that shared frame paints. */
async function coalescedHeadFlush(script, check) {
  const world = makeWorld({
    deferHeads: true,
    holdFrames: true,
    runs: [
      { file: RUN_FILE, active: true, mtime: 1777000500 },
      { file: OTHER_FILE, active: false, mtime: 1777000000 },
    ],
  });
  vm.runInContext(script, world.sandbox);
  try {
    const headsFor = (file) => world.deferredHeads.filter((h) => h.url.includes('file=' + file));
    await waitFor('first head request', () => headsFor(RUN_FILE).length === 1);

    // The first run claims its head flush and schedules a frame, which the
    // harness holds: the frame is still pending when the run is switched.
    headsFor(RUN_FILE)[0].resolve(headPayload('A', 'first run question'));
    await waitFor('first run head scheduled its frame', () => world.heldFrames() === 1);

    clickRunRow(world, OTHER_FILE);
    await waitFor('head request for the second run', () => headsFor(OTHER_FILE).length === 1);
    headsFor(OTHER_FILE)[0].resolve(headPayload('B', 'second run question'));
    await sleep(20);   // let the head continuation claim and schedule

    // The second run's head scheduling coalesced onto the prior frame: no
    // new frame was queued, and the pending one belongs to nobody stale.
    check('current_head_coalesced_onto_prior_frame',
      world.heldFrames() === 1,
      `held frames after current head scheduling: ${world.heldFrames()} (expected the 1 prior frame)`);

    world.releaseFrames();
    await settle(world);

    const text = world.transcript.textContent;
    check('current_generation_rendered',
      !text.includes('turn A') && text.includes('turn B-0'),
      `stale first-run content must not paint; text head: ${JSON.stringify(text.slice(0, 120))}`);
    const card = world.transcript.querySelector('.prompt-card');
    check('current_generation_revealed',
      !!card && card.textContent.includes('second run question')
        && world.transcript.scrollTop === EXPECTED_CARD_TOP,
      `scrollTop=${world.transcript.scrollTop} expected=${EXPECTED_CARD_TOP} card=${card ? JSON.stringify(card.textContent) : 'none'}`);
    check('coalesced_load_polled_once_without_size_probe',
      tailPolls(world).length === 1 && sizeProbes(world).length === 0,
      `polls=${tailPolls(world).length} probes=${sizeProbes(world).length} (tail_offset from the head snapshot replaces the probe)`);
    check('coalesced_load_hides_the_pill',
      !world.newPill.classList.contains('visible'),
      `pillVisible=${world.newPill.classList.contains('visible')} text=${JSON.stringify(world.newPill.textContent)}`);
  } finally {
    world.stopTimers();
  }
}

/* A→B→A with every head response deferred and resolved in an adversarial
 * order before the delayed frame: stale A1 (same filename as current A2 —
 * filename equality cannot protect A2 here, only the load generation can),
 * stale B, then A2. Only A2 may render, reveal, probe the size, and start
 * exactly one tail loop. */
async function supersededAbaOverlap(script, check) {
  const world = makeWorld({
    deferHeads: true,
    holdFrames: true,
    runs: [
      { file: RUN_FILE, active: true, mtime: 1777000500 },
      { file: OTHER_FILE, active: false, mtime: 1777000000 },
    ],
  });
  vm.runInContext(script, world.sandbox);
  try {
    const headsFor = (file) => world.deferredHeads.filter((h) => h.url.includes('file=' + file));
    await waitFor('first head request for A', () => headsFor(RUN_FILE).length === 1);

    // A1 is still current: it claims the generation-bound head flush, then
    // parks awaiting the held frame.
    headsFor(RUN_FILE)[0].resolve(headPayload('A1', 'stale A1 question'));
    await waitFor('A1 head scheduled its frame', () => world.heldFrames() === 1);

    // A→B→A: both switches happen while A1 is parked. The B selection
    // releases A1's waiter; A2 becomes the current generation.
    clickRunRow(world, OTHER_FILE);
    await waitFor('head request for B', () => headsFor(OTHER_FILE).length === 1);
    clickRunRow(world, RUN_FILE);
    await waitFor('second head request for A', () => headsFor(RUN_FILE).length === 2);

    // Adversarial order, all before the delayed frame: stale B first, then
    // current A2. Stale A1 was already released by the B selection above.
    headsFor(OTHER_FILE)[0].resolve(headPayload('B', 'stale B question'));
    headsFor(RUN_FILE)[1].resolve(headPayload('A2', 'the original A question'));
    await sleep(20);   // let both head continuations run to their verdict

    // A2's head scheduling coalesced onto A1's still-held frame.
    check('current_head_coalesced_onto_prior_frame',
      world.heldFrames() === 1,
      `held frames after adversarial resolution: ${world.heldFrames()} (expected the 1 prior frame)`);

    world.releaseFrames();
    await settle(world);
    /* Quiesce: on a broken (e.g. pre-generation-fix) UI the superseded
     * loads' continuations are timer-driven and fire their stray size
     * probes and tail polls well after the first poll arrives. Give them
     * room to land so the exactly-once counts below fail deterministically
     * instead of racing the checks. On a correct UI nothing else comes. */
    await sleep(150);

    const text = world.transcript.textContent;
    // Welcome box + prompt card + exactly A2's event rows: a stale batch
    // painting ahead of (or behind) A2's would change the row count.
    const expectedRows = 2 + headEventRows(headPayload('A2'));
    check('only_latest_a_renders',
      world.rows() === expectedRows
        && text.includes('turn A2-0')
        && !text.includes('turn A1')
        && !text.includes('turn B'),
      `rows=${world.rows()} expected=${expectedRows} hasA1=${text.includes('turn A1')} hasB=${text.includes('turn B')}`);
    const card = world.transcript.querySelector('.prompt-card');
    check('only_latest_a_reveals',
      !!card && card.textContent.includes('the original A question')
        && world.transcript.scrollTop === EXPECTED_CARD_TOP
        && world.transcript._bottomWriteCount() === 0,
      `scrollTop=${world.transcript.scrollTop} expected=${EXPECTED_CARD_TOP} card=${card ? JSON.stringify(card.textContent) : 'none'} bottomWrites=${world.transcript._bottomWriteCount()}`);
    check('exactly_one_tail_loop',
      tailPolls(world).length === 1,
      `wait=1 polls=${tailPolls(world).length}; requests: ${world.requests.join(' | ')}`);
    check('no_size_probe_when_head_carries_cursor',
      sizeProbes(world).length === 0,
      `size probes=${sizeProbes(world).length} (the head snapshot's tail_offset replaces the probe)`);
    check('tail_started_from_head_cursor',
      tailPolls(world).length === 1 && tailPolls(world)[0].includes('offset=4096'),
      `first tail poll must start at the head snapshot cursor 4096; polls: ${tailPolls(world).join(' | ')}`);
    check('no_stale_error_marker',
      !text.includes('Failed to load run'),
      'a superseded load must not paint its outcome into the current run');
    check('pill_stays_hidden',
      !world.newPill.classList.contains('visible'),
      `pillVisible=${world.newPill.classList.contains('visible')} text=${JSON.stringify(world.newPill.textContent)}`);
  } finally {
    world.stopTimers();
  }
}

/* ── head→tail gap: events appended between snapshot and flush ──────── */

/* The head response snapshots the file; the flush that paints it is held
 * back (a hidden/background/busy tab), and two events land in the gap. The
 * tail must start at the head snapshot's tail_offset — not a later size
 * probe — so every gap event arrives exactly once through tail polling. On a
 * UI that probes the size after the flush, the probe jumps past the gap
 * events and they are skipped forever: the checks below fail on counts and
 * cursor, not on a timeout. */
async function headTailGap(script, check) {
  const world = makeWorld({
    holdFrames: true,
    prompt: '',
    virtualFile: [
      { type: 'system', subtype: 'init', model: 'claude-fable-5', cwd: '/tmp/demo' },
      tailEvent('head line one'),
      tailEvent('head line two'),
    ],
  });
  vm.runInContext(script, world.sandbox);
  try {
    await waitFor('head request', () =>
      world.requests.some((r) => r.startsWith('/api/head')));
    await waitFor('head scheduled its frame', () => world.heldFrames() === 1);
    const snapshotCursor = world.headTailOffset();

    // The gap: bytes appended after the head snapshot, before the flush.
    world.appendEvents([tailEvent('gap event A'), tailEvent('gap event B')]);
    world.releaseFrames();   // RAF_DELAY_MS still stalls the flush past the gap

    await settle(world);   // first long-poll proves the load sequence finished
    /* Quiesce instead of waiting for the gap rows: on a size-probe UI the
     * poll is anchored past them and they never arrive, so a waitFor would
     * turn the behavioral miss into a generic timeout. Give the first poll
     * and its (still RAF-stalled) render frame room to land, then count. */
    await sleep(300 + 2 * RAF_DELAY_MS);

    const text = world.transcript.textContent;
    const countOf = (needle) => text.split(needle).length - 1;
    check('tail_started_from_head_cursor',
      snapshotCursor !== null && tailPolls(world).length > 0
        && tailPolls(world)[0].includes('offset=' + snapshotCursor),
      `first poll must start at the head snapshot cursor ${snapshotCursor}; polls: ${tailPolls(world).join(' | ')}`);
    check('no_size_probe_when_head_carries_cursor',
      sizeProbes(world).length === 0,
      `size probes=${sizeProbes(world).length} (tail_offset from the head snapshot replaces the probe)`);
    check('gap_events_rendered_exactly_once',
      countOf('gap event A') === 1 && countOf('gap event B') === 1
        && countOf('head line one') === 1
        && world.rows() === 1 + 2 + 2,   // welcome + 2 head rows + 2 gap rows
      `rows=${world.rows()} gapA=${countOf('gap event A')} gapB=${countOf('gap event B')} head1=${countOf('head line one')}`);
  } finally {
    world.stopTimers();
  }
}

/* A legacy head payload without tail_offset: the UI falls back to the old
 * size probe, exactly once, and the tail starts from the probed offset. */
async function legacyHeadFallback(script, check) {
  const world = makeWorld({ legacyHead: true, prompt: '' });
  vm.runInContext(script, world.sandbox);
  try {
    await settle(world);
    check('legacy_head_triggers_one_size_probe',
      sizeProbes(world).length === 1,
      `size probes=${sizeProbes(world).length}; requests: ${world.requests.join(' | ')}`);
    check('legacy_tail_starts_from_probed_offset',
      tailPolls(world).length === 1 && tailPolls(world)[0].includes('offset=4096'),
      `polls: ${tailPolls(world).join(' | ')}`);

    const rows = world.rows();
    world.deliverTail([tailEvent('streamed A')]);
    await waitFor('legacy tail batch rendered', () => world.rows() === rows + 1);
    check('legacy_tail_still_follows',
      world.transcript.scrollTop === world.bottom(),
      `scrollTop=${world.transcript.scrollTop} bottom=${world.bottom()}`);
  } finally {
    world.stopTimers();
  }
}

/* ── stale history page surviving A→B→A ─────────────────────────────── */

/* A1 loads fully, then pages older history; while that history request is
 * in flight the user goes A→B→A. The stale A1 history response lands in the
 * middle of A2's load, before A2's (held) flush. It must not prepend rows,
 * must not flush (which would release A2's generation-bound head waiter
 * early), and must not touch A2's paging state: only A2 renders, reveals,
 * and polls, and A2's waiter is released only by A2's own head flush. */
async function staleHistoryAba(script, check) {
  const world = makeWorld({
    deferHeads: true,
    holdFrames: true,
    runs: [
      { file: RUN_FILE, active: true, mtime: 1777000500 },
      { file: OTHER_FILE, active: false, mtime: 1777000000 },
    ],
  });
  vm.runInContext(script, world.sandbox);
  const headsFor = (file) => world.deferredHeads.filter((h) =>
    h.url.includes('file=' + file) && !h.url.includes('before='));
  const historyFor = (file) => world.deferredHeads.filter((h) =>
    h.url.includes('file=' + file) && h.url.includes('before='));
  try {
    // A1 loads and settles completely: revealed, tail poll parked.
    await waitFor('A1 head request', () => headsFor(RUN_FILE).length === 1);
    headsFor(RUN_FILE)[0].resolve(headPayload('A1', 'the A question'));
    await waitFor('A1 head scheduled its frame', () => world.heldFrames() === 1);
    world.releaseFrames();
    await settle(world);
    world.holdFrames();   // re-stall the tab for the A→B→A window below

    // Scrolling to the top pages older history; the request stays deferred.
    world.transcript.scrollTop = 0;
    await waitFor('A1 history request', () => historyFor(RUN_FILE).length === 1);

    // A→B→A while the A1 history page is in flight. B's head never resolves.
    clickRunRow(world, OTHER_FILE);
    await waitFor('head request for B', () => headsFor(OTHER_FILE).length === 1);
    clickRunRow(world, RUN_FILE);
    await waitFor('A2 head request', () => headsFor(RUN_FILE).length === 2);
    headsFor(RUN_FILE)[1].resolve(headPayload('A2', 'the A question'));
    await waitFor('A2 head scheduled its frame', () => world.heldFrames() === 1);
    const pollsBeforeStale = tailPolls(world).length;

    // The stale A1 history response lands mid-A2-load, before A2's flush.
    historyFor(RUN_FILE)[0].resolve({
      lines: [
        tailEvent('stale OLD history line'),
        tailEvent('stale OLD history line 2'),
      ],
      before: 0, has_more: false, prompt: 'the A question',
    });
    await sleep(30);   // let the stale continuation run to its verdict

    const textBeforeFlush = world.transcript.textContent;
    check('stale_history_did_not_paint_before_a2_flush',
      !textBeforeFlush.includes('stale OLD') && !textBeforeFlush.includes('turn A2'),
      `stale page must not paint or flush ahead of A2's held frame; text head: ${JSON.stringify(textBeforeFlush.slice(0, 160))}`);
    check('a2_waiter_not_released_by_stale_history',
      world.transcript.scrollTop !== EXPECTED_CARD_TOP
        && tailPolls(world).length === pollsBeforeStale,
      `a stale history flush would reveal A2 early and start its tail; scrollTop=${world.transcript.scrollTop} polls=${tailPolls(world).length} before=${pollsBeforeStale}`);

    world.releaseFrames();
    await waitFor('A2 tail poll after its own flush', () =>
      tailPolls(world).length === pollsBeforeStale + 1);
    await sleep(30);   // let the reveal's scroll event land

    const text = world.transcript.textContent;
    const expectedRows = 2 + headEventRows(headPayload('A2'));
    check('only_a2_renders_after_stale_history',
      world.rows() === expectedRows
        && text.includes('turn A2-0')
        && !text.includes('stale OLD')
        && !text.includes('turn B'),
      `rows=${world.rows()} expected=${expectedRows} hasOld=${text.includes('stale OLD')} hasB=${text.includes('turn B')}`);
    const card = world.transcript.querySelector('.prompt-card');
    check('a2_revealed_after_own_flush',
      !!card && card.textContent.includes('the A question')
        && world.transcript.scrollTop === EXPECTED_CARD_TOP,
      `scrollTop=${world.transcript.scrollTop} expected=${EXPECTED_CARD_TOP} card=${card ? JSON.stringify(card.textContent) : 'none'}`);
    check('a2_tail_polls_exactly_once',
      tailPolls(world).length === pollsBeforeStale + 1
        && sizeProbes(world).length === 0,
      `polls=${tailPolls(world).length} expected=${pollsBeforeStale + 1} probes=${sizeProbes(world).length}`);
  } finally {
    world.stopTimers();
  }
}

const SCENARIOS = {
  prompt_first_load: promptFirstLoad,
  prompt_deep_link_reload: promptDeepLinkReload,
  no_prompt_tail_follow: noPromptTailFollow,
  prompt_then_no_prompt_switch: promptThenNoPromptSwitch,
  empty_head_settles: emptyHeadSettles,
  coalesced_head_flush: coalescedHeadFlush,
  superseded_aba_overlap: supersededAbaOverlap,
  head_tail_gap: headTailGap,
  legacy_head_fallback: legacyHeadFallback,
  stale_history_aba: staleHistoryAba,
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
