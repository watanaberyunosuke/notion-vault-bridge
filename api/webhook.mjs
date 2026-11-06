import { waitUntil } from '@vercel/functions';
import { createHmac, timingSafeEqual } from 'node:crypto';

export const config = { api: { bodyParser: false } };

export function equal(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string') return false;
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function trusted(provider, body, headers, env) {
  if (provider === 'dropbox' && env.DROPBOX_APP_SECRET) {
    const signature = createHmac('sha256', env.DROPBOX_APP_SECRET).update(body).digest('hex');
    return equal(headers['x-dropbox-signature'], signature);
  }
  if (provider === 'notion' && env.NOTION_VERIFICATION_TOKEN) {
    const signature = createHmac('sha256', env.NOTION_VERIFICATION_TOKEN).update(body).digest('hex');
    return equal(headers['x-notion-signature'], `sha256=${signature}`);
  }
  return false;
}

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  const url = new URL(req.url, 'https://localhost');
  const provider = url.searchParams.get('provider');
  if (!['dropbox', 'notion'].includes(provider)) {
    return res.status(400).json({ error: 'Unknown provider' });
  }
  if (req.method === 'GET' && provider === 'dropbox') {
    const challenge = url.searchParams.get('challenge');
    if (!challenge || challenge.length > 1000) return res.status(400).end();
    res.setHeader('Content-Type', 'text/plain');
    return res.status(200).send(challenge);
  }
  if (req.method !== 'POST') return res.status(405).end();
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > 65536) return res.status(413).end();
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks);
  let body;
  try { body = JSON.parse(raw.toString('utf8')); } catch { return res.status(400).end(); }
  const origin = process.env.BRIDGE_ORIGIN;
  if (!origin || !origin.startsWith('https://') || !process.env.CRON_SECRET) {
    return res.status(503).json({ error: 'Setup required' });
  }
  const call = async (payload, timeout = 6000) => {
    const response = await fetch(`${origin}/api/sync`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${process.env.CRON_SECRET}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeout),
    });
    if (!response.ok) throw new Error('Internal sync request failed');
  };
  try {
    if (provider === 'notion' && body.verification_token) {
      if (!process.env.NOTION_SETUP_KEY || !equal(url.searchParams.get('setup'), process.env.NOTION_SETUP_KEY)) {
        return res.status(401).end();
      }
      // Candidate only: the administrator verifies it in Notion and sets the signing key.
      await call({ action: 'verification', token: body.verification_token });
      return res.status(200).json({ received: true });
    }
    if (!trusted(provider, raw, req.headers, process.env)) return res.status(401).end();
    if (provider === 'dropbox' && !body.list_folder?.accounts?.includes(process.env.DROPBOX_ACCOUNT_ID)) {
      return res.status(200).json({ ignored: true });
    }
    // Commit queue state before acknowledging the provider; execution is bounded by Vercel.
    await call({ action: 'enqueue' });
    waitUntil(call({ action: 'run' }, 240000).catch(() => {
      console.error('Queued sync did not complete; daily reconciliation or manual retry will resume it.');
    }));
    return res.status(200).json({ queued: true });
  } catch {
    return res.status(503).json({ error: 'Queue unavailable; retry notification' });
  }
}
