import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

test('package.json declares link-preview-js so Baileys url generation can resolve it', () => {
  const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  assert.equal(typeof pkg.dependencies['link-preview-js'], 'string');
  assert.match(pkg.dependencies['link-preview-js'], /^\^3\./);
});

test('link-preview-js is resolvable from the WhatsApp bridge tree', () => {
  const resolved = require.resolve('link-preview-js');
  assert.match(resolved, /link-preview-js/);
  const mod = require('link-preview-js');
  assert.equal(typeof mod.getLinkPreview, 'function');
});
