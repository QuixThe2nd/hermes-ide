import assert from 'node:assert/strict'

import { test } from 'vitest'

import { githubApiHeaders, githubTokenFromEnv } from './github-api-auth'

// The exact headers the anonymous update check sends today; the change must
// leave this shape byte-identical when no token is configured.
const UPDATE_CHECK_HEADERS = {
  Accept: 'application/vnd.github.sha',
  'User-Agent': 'hermes-desktop-update-check'
}

test('GITHUB_TOKEN wins over GH_TOKEN, blanks fall through, values are trimmed', () => {
  assert.equal(githubTokenFromEnv({ GITHUB_TOKEN: 'pat-a', GH_TOKEN: 'pat-b' }), 'pat-a')
  assert.equal(githubTokenFromEnv({ GITHUB_TOKEN: '   ', GH_TOKEN: '  pat-b  ' }), 'pat-b')
  assert.equal(githubTokenFromEnv({ GITHUB_TOKEN: '', GH_TOKEN: '' }), null)
  assert.equal(githubTokenFromEnv({}), null)
})

test('anonymous sends no Authorization key at all; a token adds the `token` scheme without touching the base', () => {
  const anonymous = githubApiHeaders(UPDATE_CHECK_HEADERS, null)

  assert.deepEqual(anonymous, UPDATE_CHECK_HEADERS)
  assert.equal('Authorization' in anonymous, false)

  const authed = githubApiHeaders(UPDATE_CHECK_HEADERS, 'pat-a')

  assert.equal(authed.Authorization, 'token pat-a')
  assert.equal(authed['User-Agent'], 'hermes-desktop-update-check')
  // The caller's base object is a constant; mutating it would leak the token
  // into every later request that builds from it.
  assert.equal('Authorization' in UPDATE_CHECK_HEADERS, false)
})
