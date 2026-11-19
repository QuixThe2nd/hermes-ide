#!/usr/bin/env node
// @ts-check
/**
 * Expand the install/update support matrix into concrete E2E combinations.
 *
 * One source of truth for every {os, install-method, update-method} pair a
 * user could be on. This file only DECLARES and EXPANDS: it knows nothing
 * about which combinations CI can drive. Every combination is dispatched to
 * its OS's run workflow, and THAT workflow natively skips the method pairs
 * its driver cannot run yet -- capability knowledge lives next to each
 * driver (install-e2e-run.yml for linux, install-e2e-macos-run.yml,
 * and install-e2e-windows-run.yml). Correctness here is enforced by the type
 * unions below (checked via `tsc --checkJs`), not by runtime validation --
 * anything the types can't catch is self-evident on the next CI run.
 *
 * Used by .github/workflows/install-e2e.yml, which runs it with the picked
 * release tags:
 *
 *   node scripts/sandbox/generate-e2e-matrix.mjs \
 *     --tags '["v2026.8.3"]'
 *
 * Prints JSON: { linux: {include:[...]}, windows: {include:[...]},
 * macos: {include:[...]} } -- every entry is {name, install_method,
 * update_method, install_ref}.
 */

import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';

/**
 * The closed method/version vocabulary. Workflows key off these exact
 * strings, so they are types, not conventions.
 *
 * @typedef {'installer-script'} InstallMethod
 *   The platform's one-liner (curl | bash on linux/macos, irm | iex on
 *   windows).
 * @typedef {InstallMethod | 'hermes-update'} UpdateMethod
 *   Re-run the installer over the existing install, or use the updater CLI.
 * @typedef {'linux' | 'windows' | 'macos'} Os
 *
 * @typedef {{method: InstallMethod}} InstallEntry
 * @typedef {{method: UpdateMethod}} UpdateEntry
 * @typedef {{install: InstallEntry[], update: UpdateEntry[], secondUpdate?: never[]}} OsSpec
 *   secondUpdate (install -> update -> update again) is a real future axis
 *   -- the updater that RESULTS from an update must itself update -- typed
 *   `never[]` so declaring one is a type error until a leg implements it.
 *
 * @typedef {string} TagAnnotation
 *   A picked release tag.
 *
 * @typedef {{name: string, leg_id: string, install_method: string, update_method: string,
 *            install_ref: string}} MatrixEntry
 */

/**
 * Artifact-safe key for one leg: the matrix name with every character
 * outside [A-Za-z0-9._-] collapsed to '-'. Reconstructed by the results
 * renderer from the parsed job name (same formula, same bytes), and used
 * by every run workflow to name its logs/player artifacts, so the report
 * job can map a concluded leg back to its artifacts without GitHub
 * linking jobs to artifacts.
 * @param {string} name
 * @returns {string}
 */
export function legId(name) {
  return name.replace(/[^A-Za-z0-9._-]+/g, '-');
}

/** @type {Record<Os, OsSpec>} */
export const SPEC = {
  windows: {
    install: [{ method: 'installer-script' }],
    update: [{ method: 'installer-script' }, { method: 'hermes-update' }],
  },
  macos: {
    install: [{ method: 'installer-script' }],
    update: [{ method: 'installer-script' }, { method: 'hermes-update' }],
  },
  linux: {
    install: [{ method: 'installer-script' }],
    update: [{ method: 'installer-script' }, { method: 'hermes-update' }],
  },
};

/**
 * Expand one method entry into its concrete id.
 * @param {InstallEntry | UpdateEntry} entry
 * @returns {string[]}
 */
export function expandMethod(entry) {
  return [entry.method];
}

/**
 * Every {os, install, update} combination in SPEC.
 * @param {Record<Os, OsSpec>} spec
 * @returns {{os: Os, install: string, update: string}[]}
 */
export function generateEnvironments(spec) {
  /** @type {{os: Os, install: string, update: string}[]} */
  const envs = [];
  for (const [os, osSpec] of /** @type {[Os, OsSpec][]} */ (Object.entries(spec))) {
    for (const install of osSpec.install.flatMap(expandMethod)) {
      for (const update of osSpec.update.flatMap(expandMethod)) {
        envs.push({ os, install, update });
      }
    }
  }
  return envs;
}

/**
 * Split the combinations into one matrix per OS.
 *
 * `tags` (the released versions we test updating FROM) is the OUTER axis:
 * for each tag, for each combination, one dispatch that installs the tag
 * and updates to HEAD. No capability filtering happens here -- every
 * declared combination is dispatched, and the OS's run workflow natively
 * skips what its driver cannot run yet. Entry names carry everything (os,
 * method pair, tag transition) because slash-joined leg names are all the
 * graph renders.
 *
 * @param {{os: Os, install: string, update: string}[]} envs
 * @param {TagAnnotation[]} tags
 * @returns {Record<Os, {include: MatrixEntry[]}>}
 */
export function buildMatrices(envs, tags) {
  /** @type {Record<Os, {include: MatrixEntry[]}>} */
  const byOs = { linux: { include: [] }, windows: { include: [] }, macos: { include: [] } };
  for (const env of envs) {
    for (const tag of tags) {
      /** @type {MatrixEntry} */
      const entry = {
        name: `${env.os}: ${env.install} -> ${env.update} (${tag} -> HEAD)`,
        leg_id: legId(`${env.os}: ${env.install} -> ${env.update} (${tag} -> HEAD)`),
        install_method: env.install,
        update_method: env.update,
        install_ref: tag,
      };
      byOs[env.os].include.push(entry);
    }
  }
  return byOs;
}

/**
 * Render the plan as a markdown cross-table for $GITHUB_STEP_SUMMARY:
 * one row per {os, install -> update} combination, one column per
 * starting tag. Every cell is dispatched; whether it RUNS or greys out
 * is the run workflow's call (capability lives there, not here).
 *
 * @param {{os: Os, install: string, update: string}[]} envs
 * @param {TagAnnotation[]} tags
 * @returns {string}
 */
export function renderMarkdownPlan(envs, tags) {
  const lines = [
    '### Install & Update E2E plan',
    '',
    `${envs.length} combinations x ${tags.length} starting tags = ${envs.length * tags.length} legs`,
    '',
    `| combination | ${tags.join(' | ')} |`,
    `|---|${tags.map(() => '---').join('|')}|`,
  ];
  for (const env of envs) {
    const cells = tags.map(() => '⏳ 	');
    lines.push(`| \`${env.os}: ${env.install} -> ${env.update}\` | ${cells.join(' | ')} |`);
  }
  return lines.join('\n');
}

/**
 * Render the run's OUTCOME as the same cross-table, from the run's own job
 * list (GitHub Actions API): one row per combination, one column per tag,
 * each cell the leg's conclusion. Input is NDJSON {name, conclusion} lines
 * -- what `gh api --paginate --jq '.jobs[] | {name, conclusion}'` emits --
 * and legs are recognized by the exact name shape buildMatrices mints
 * ("os: install -> update (tag -> HEAD) / ..."), so unrelated jobs
 * (pick-releases, the report job itself) fall out naturally.
 *
 * @param {{name: string, conclusion: string | null}[]} jobs
 * @param {Map<string, number>} [artifactById] Artifact name -> id for this
 *   run (the report job's --artifacts). Legs that RAN (success or failure)
 *   get TWO links: 📼 to the run's single player artifact (playback.html,
 *   archive:false names artifacts after the FILE) with the leg's logs zip
 *   as a #zip= HASH param — the artifact URL 307s server-side and strips
 *   the query, the hash survives — and ⬇️ to the raw logs zip. Logs
 *   artifacts are `install-e2e-logs-<leg_id>` (windows) or
 *   `install-e2e-logs-<leg_id>-<sha>` (posix arms append the sha at
 *   upload) — matched by prefix. leg_id is rebuilt from the parsed job
 *   name with the same formula the generator mints (legId).
 * @returns {string}
 */
export function renderMarkdownResults(jobs, artifactById = new Map()) {
  const knownRules = JSON.parse(fs.readFileSync(new URL('../../tests/install/e2e-assets/known-failures.json', import.meta.url), 'utf8'));
  /** @type {Map<string, {number: number, rule: any}>} */
  const footnotes = new Map();
  const LEG = /^(linux|windows|macos): (\S+) -> (\S+) \(([^)]+) -> HEAD\) \//;
  // A combination can surface as SEVERAL jobs with the same leg name (a
  // run workflow may have one inner job per driver arm; exactly one runs
  // and the others natively skip), so cells merge by significance: a real
  // outcome always beats a skip, and a bad outcome beats a good one.
  const RANK = ['skip', 'TODO', '&#x2705;', 'known', 'running', 'cancelled', '&#x274C;'];
  const SKIPS = ['skip', 'TODO'];
  // Rendered success/failure cells carry artifact links after the glyph;
  // rank by the leading token or every such cell would rank as unknown (-1)
  // and lose to any skip already in the map.
  /** @param {string} cell */
  const rankOf = (cell) => RANK.findIndex((t) => cell === t || cell.startsWith(`${t} `));
  /** @type {Map<string, Map<string, string>>} */
  const rows = new Map();
  /** @type {string[]} */
  const tags = [];
  for (const job of jobs) {
    const m = job.name.match(LEG);
    if (!m) continue;
    const combo = `${m[1]}: ${m[2]} -> ${m[3]}`;
    const tag = m[4];
    if (!tags.includes(tag)) tags.push(tag);
    if (!rows.has(combo)) rows.set(combo, new Map());
    const cell = (() => {
    const legId2 = legId(`${m[1]}: ${m[2]} -> ${m[3]} (${m[4]} -> HEAD)`);
    // Logs artifacts: `install-e2e-logs-<leg_id>` (windows) or with a
    // trailing `-<sha>` (posix arms append it at upload). Match by prefix.
    const logsName = [...artifactById.keys()].find((n) => n.startsWith(`install-e2e-logs-${legId2}`));
    // Player artifacts: the single per-run leg-player upload; archive:
    // false names it after the FILE (playback.html), ignoring `name:`.
    const playerName = [...artifactById.keys()].find((n) => n === 'playback.html');
    const playerId = playerName !== undefined ? artifactById.get(playerName) : undefined;
    const logsId = logsName !== undefined ? artifactById.get(logsName) : undefined;
    // Two links per ran leg: the player with the zip as a HASH param
    // (GitHub's artifact URL 307s to /suites/... and strips the query —
    // the hash survives client-side), and the raw zip download.
    const runBase = `https://github.com/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}`;
    const reel = (playerId !== undefined && logsId !== undefined)
      ? ` [📼](${runBase}/artifacts/${playerId}#zip=${encodeURIComponent(`${runBase}/artifacts/${logsId}`)}) [⬇️](${runBase}/artifacts/${logsId})`
      : '';
    switch (job.conclusion) {
      case 'success': {
        // Only the classifier's uploaded receipt turns a successful job into
        // a known-failure cell. Tag membership alone never suppresses a red.
        const rule = knownRules.find((/** @type {any} */ r) => artifactById.has(`install-e2e-known-${r.id}--${legId2}`));
        if (!rule) return `&#x2705;${reel}`;
        if (!footnotes.has(rule.id)) footnotes.set(rule.id, { number: footnotes.size + 1, rule });
        return `known [^${footnotes.get(rule.id)?.number}]${reel}`;
      }
      case 'failure': return `&#x274C;${reel}`;
      case 'skipped': return 'TODO';
      case 'cancelled': return 'cancelled';
      default: return 'running';
    }
  })();
    const byTag = /** @type {Map<string, string>} */ (rows.get(combo));
    const prev = byTag.get(tag);
    if (prev === undefined || rankOf(cell) > rankOf(prev)) {
      byTag.set(tag, cell);
    }
  }
  if (rows.size === 0) return '### Install & Update E2E results\n\n(no legs found in this run)\n';
  const cells = [...rows.values()].flatMap((r) => [...r.values()]);
  const passed = cells.filter((c) => c.startsWith('&#x2705;')).length;
  const failed = cells.filter((c) => c.startsWith('&#x274C;')).length;
  const skipped = cells.filter((c) => SKIPS.includes(c)).length;
  const known = cells.filter((c) => c.startsWith('known ')).length;
  const lines = [
    '### Install & Update E2E results',
    '',
    `${passed} passed, ${failed} failed, ${known} known failures, ${skipped} skipped (TODO = declared, no driver arm yet), ${cells.length} legs total`,
    '',
    `| combination | ${tags.join(' | ')} |`,
    `|---|${tags.map(() => '---').join('|')}|`,
  ];
  for (const [combo, byTag] of rows) {
    lines.push(`| \`${combo}\` | ${tags.map((t) => byTag.get(t) || '-').join(' | ')} |`);
  }
  lines.push('');
  for (const { number, rule } of footnotes.values()) {
    lines.push(`[^${number}]: **${rule.title}.** ${rule.explanation} [Evidence](${rule.evidence}).`);
  }
  return lines.join('\n');
}

/** @returns {Promise<string>} all of stdin */
function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
  });
}

async function main() {
  const { values } = parseArgs({
    options: {
      tags: { type: 'string', default: '[]' },
      format: { type: 'string', default: 'json' },
      artifacts: { type: 'string' },
    },
  });
  if (values.format === 'results') {
    const jobs = (await readStdin()).split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
    /** @type {Map<string, number>} */
    const artifactById = new Map();
    if (values.artifacts) {
      for (const line of (await fs.promises.readFile(values.artifacts, 'utf8')).split('\n')) {
        if (!line.trim()) continue;
        const a = JSON.parse(line);
        if (typeof a.name === 'string' && typeof a.id === 'number') artifactById.set(a.name, a.id);
      }
    }
    process.stdout.write(renderMarkdownResults(jobs, artifactById));
    return;
  }
  const tags = /** @type {TagAnnotation[]} */ (JSON.parse(values.tags));
  const envs = generateEnvironments(SPEC);
  if (values.format === 'markdown') {
    process.stdout.write(renderMarkdownPlan(envs, tags));
    return;
  }
  const matrices = buildMatrices(envs, tags);
  process.stdout.write(`${JSON.stringify(matrices, null, 2)}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  await main();
}
