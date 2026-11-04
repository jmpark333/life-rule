'use strict';

const lib = require('./_lib');

module.exports = async function handler(req, res) {
  if (req.method === 'GET') {
    try {
      return lib.send(res, 200, { ok: true, state: await lib.readState() });
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
    if (body.action === 'check') {
      const date = String(body.date || '');
      const ruleId = String(body.ruleId || '');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !ruleId) {
        return lib.send(res, 400, { ok: false, error: 'bad-request' });
      }
      const rules = await lib.readRules();
      const rule = rules.find(function (r) { return r.id === ruleId; });
      if (!rule || rule.archived) {
        return lib.send(res, 409, { ok: false, error: 'rule-not-found', state: await lib.readState() });
      }
      await lib.setCheck(date, ruleId, body.value === true);
    } else if (body.action === 'rules') {
      const rules = lib.normalizeRules(body.rules);
      if (!rules.length) {
        return lib.send(res, 400, { ok: false, error: 'no-rules', state: await lib.readState() });
      }
      await lib.writeRules(rules);
    } else if (body.action === 'import') {
      const next = lib.normalizeState(body.state);
      if (!next.rules.length) {
        return lib.send(res, 400, { ok: false, error: 'no-rules', state: await lib.readState() });
      }
      await lib.replaceAll(next);
    } else {
      return lib.send(res, 400, { ok: false, error: 'unknown-action' });
    }

    return lib.send(res, 200, { ok: true, state: await lib.readState() });
  } catch (err) {
    return lib.send(res, 503, { ok: false, error: 'storage-unavailable' });
  }
};
