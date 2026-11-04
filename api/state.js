'use strict';

const lib = require('./_lib');

module.exports = async function handler(req, res) {
  if (req.method === 'GET') {
    try {
      const state = await lib.readState();
      return lib.send(res, 200, { ok: true, state: state });
    } catch (err) {
      return lib.send(res, 503, { ok: false, error: 'storage-unavailable' });
    }
  }

  if (req.method !== 'POST') {
    res.setHeader('Allow', 'GET, POST');
    return lib.send(res, 405, { ok: false, error: 'method-not-allowed' });
  }

  if (!lib.tokenValid(lib.bearer(req))) {
    return lib.send(res, 401, { ok: false, error: 'unauthorized' });
  }

  const body = lib.readBody(req);
  if (!body || typeof body.action !== 'string') {
    return lib.send(res, 400, { ok: false, error: 'bad-request' });
  }

  let state;
  try {
    state = await lib.readState();
  } catch (err) {
    return lib.send(res, 503, { ok: false, error: 'storage-unavailable' });
  }

  if (body.action === 'check') {
    const date = String(body.date || '');
    const ruleId = String(body.ruleId || '');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !ruleId) {
      return lib.send(res, 400, { ok: false, error: 'bad-request' });
    }
    const rule = state.rules.find(function (r) { return r.id === ruleId; });
    if (!rule || rule.archived) {
      return lib.send(res, 409, { ok: false, error: 'rule-not-found', state: state });
    }
    const day = state.entries[date] || {};
    if (body.value === true) day[ruleId] = true;
    else delete day[ruleId];
    if (Object.keys(day).length) state.entries[date] = day;
    else delete state.entries[date];
  } else if (body.action === 'rules') {
    const next = lib.normalizeState({ rules: body.rules, entries: state.entries });
    if (!next.rules.length) {
      return lib.send(res, 400, { ok: false, error: 'no-rules', state: state });
    }
    state.rules = next.rules;
  } else if (body.action === 'import') {
    const next = lib.normalizeState(body.state);
    if (!next.rules.length) {
      return lib.send(res, 400, { ok: false, error: 'no-rules', state: state });
    }
    state = next;
  } else {
    return lib.send(res, 400, { ok: false, error: 'unknown-action' });
  }

  try {
    await lib.writeState(state);
  } catch (err) {
    return lib.send(res, 503, { ok: false, error: 'storage-unavailable' });
  }

  return lib.send(res, 200, { ok: true, state: state });
};
