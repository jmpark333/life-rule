'use strict';

const lib = require('./_lib');

module.exports = async function handler(req, res) {
  if (req.method === 'GET') {
    try {
      return lib.send(res, 200, { ok: true, items: await lib.listBackups() });
    } catch (err) {
      return lib.send(res, 503, { ok: false, error: 'storage-unavailable' });
    }
  }

  if (req.method !== 'POST') {
    res.setHeader('Allow', 'GET, POST');
    return lib.send(res, 405, { ok: false, error: 'method-not-allowed' });
  }

  const body = lib.readBody(req);
  if (!body || typeof body.action !== 'string') {
    return lib.send(res, 400, { ok: false, error: 'bad-request' });
  }

  try {
    if (body.action === 'create') {
      const result = await lib.snapshot('manual');
      return lib.send(res, 200, { ok: true, result: result, items: await lib.listBackups() });
    }

    if (body.action === 'restore') {
      const id = String(body.id || '');
      if (!/^\d+-[a-z-]+$/.test(id)) {
        return lib.send(res, 400, { ok: false, error: 'bad-backup-id' });
      }
      const state = await lib.restoreBackup(id);
      return lib.send(res, 200, { ok: true, state: state });
    }

    return lib.send(res, 400, { ok: false, error: 'unknown-action' });
  } catch (err) {
    const notFound = String(err && err.message) === 'backup-not-found';
    return lib.send(res, notFound ? 404 : 503, { ok: false, error: notFound ? 'backup-not-found' : 'storage-unavailable' });
  }
};
