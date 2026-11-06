import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { equal, trusted } from '../api/webhook.mjs';

test('Dropbox requires the exact signed bytes and configured secret', () => {
  const body = Buffer.from('{"list_folder":{"accounts":["sample"]}}');
  const key = 'synthetic-secret';
  const signature = createHmac('sha256', key).update(body).digest('hex');
  assert.equal(trusted('dropbox', body, { 'x-dropbox-signature': signature }, { DROPBOX_APP_SECRET: key }), true);
  assert.equal(trusted('dropbox', Buffer.from('{}'), { 'x-dropbox-signature': signature }, { DROPBOX_APP_SECRET: key }), false);
  assert.equal(trusted('dropbox', body, { 'x-dropbox-signature': signature }, {}), false);
});

test('Notion signature uses its verification token and sha256 prefix', () => {
  const body = Buffer.from('{"type":"page.content_updated"}');
  const key = 'synthetic-verification-token';
  const signature = `sha256=${createHmac('sha256', key).update(body).digest('hex')}`;
  assert.equal(trusted('notion', body, { 'x-notion-signature': signature }, { NOTION_VERIFICATION_TOKEN: key }), true);
  assert.equal(trusted('notion', body, { 'x-notion-signature': signature.slice(7) }, { NOTION_VERIFICATION_TOKEN: key }), false);
});

test('Missing and malformed signatures fail closed', () => {
  assert.equal(equal(undefined, 'value'), false);
  assert.equal(equal('x', 'xx'), false);
  assert.equal(trusted('unknown', Buffer.from('{}'), {}, {}), false);
});
