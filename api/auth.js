'use strict';

const lib = require('./_lib');

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return lib.send(res, 405, { ok: false, error: 'method-not-allowed' });
  }

  const body = lib.readBody(req);
  const password = body && typeof body.password === 'string' ? body.password : '';
  const ip = lib.clientIp(req);

  if (await lib.tooManyFails(ip)) {
    return lib.send(res, 429, { ok: false, error: 'too-many-attempts' });
  }

  if (!lib.passwordMatches(password)) {
    await lib.recordFail(ip);
    return lib.send(res, 401, { ok: false, error: 'wrong-password' });
  }

  await lib.clearFails(ip);

  if (!process.env.AUTH_SECRET) {
    return lib.send(res, 503, { ok: false, error: 'auth-not-configured' });
  }

  return lib.send(res, 200, { ok: true, token: lib.issueToken() });
};
