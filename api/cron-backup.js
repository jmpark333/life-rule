'use strict';

const lib = require('./_lib');

// 매일 23:00 KST (14:00 UTC) — vercel.json 의 crons 가 호출한다.
module.exports = async function handler(req, res) {
  const secret = process.env.CRON_SECRET || '';
  const auth = req.headers.authorization || '';
  if (!secret || auth !== 'Bearer ' + secret) {
    return lib.send(res, 401, { ok: false, error: 'unauthorized' });
  }

  try {
    const result = await lib.snapshot('daily', { skipIfSame: true });
    return lib.send(res, 200, { ok: true, result: result });
  } catch (err) {
    return lib.send(res, 503, { ok: false, error: 'storage-unavailable' });
  }
};
