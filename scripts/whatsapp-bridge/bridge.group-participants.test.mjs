/**
 * Unit tests for group participant JID normalisation used by
 * POST /group-participants (pc_60aaac38c871).
 *
 * The roster WhatsApp group sync posts raw staff numbers to the bridge;
 * the bridge must turn each into a bare @s.whatsapp.net JID before
 * calling Baileys' groupParticipantsUpdate.
 *
 * Pure helper test — no import of bridge.js (that starts a server).
 */

import { strict as assert } from 'node:assert';

import { normaliseParticipantJid } from './bridge_helpers.js';

// -- plain digit strings ---------------------------------------------------
{
  const jid = normaliseParticipantJid('61406117214');
  assert.equal(jid, '61406117214@s.whatsapp.net');
  console.log('  ✓ bare digits pass through with the country code intact');
}

// -- AU 10-digit local numbers get the 61 prefix ---------------------------
{
  const jid = normaliseParticipantJid('0406117214');
  assert.equal(jid, '61406117214@s.whatsapp.net');
  console.log('  ✓ AU 10-digit number with leading 0 becomes 61…');
}

// -- punctuation and full JIDs are stripped to digits ----------------------
{
  assert.equal(
    normaliseParticipantJid('+61 421 220 359'),
    '61421220359@s.whatsapp.net',
  );
  assert.equal(
    normaliseParticipantJid('61406117214@s.whatsapp.net'),
    '61406117214@s.whatsapp.net',
  );
  console.log('  ✓ formatted numbers and existing JIDs normalise to digits@…');
}

// -- non-AU 10-digit numbers keep their country code -----------------------
{
  const jid = normaliseParticipantJid('15550001111');
  assert.equal(jid, '15550001111@s.whatsapp.net');
  console.log('  ✓ 10-digit number without leading 0 is not given a 61 prefix');
}

// -- garbage inputs throw --------------------------------------------------
{
  assert.throws(() => normaliseParticipantJid(''), /no digits/);
  assert.throws(() => normaliseParticipantJid(null), /no digits/);
  assert.throws(() => normaliseParticipantJid('not-a-number'), /no digits/);
  console.log('  ✓ digit-less input throws with a clear message');
}
