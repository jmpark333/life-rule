'use strict';

const crypto = require('node:crypto');

const RULES_KEY = 'life-rule:rules';
const CHECK_KEY = 'life-rule:check';
const LEGACY_KEY = 'life-rule:state';
const TOKEN_TTL_MS = 12 * 60 * 60 * 1000;
const MAX_FAILS = 10;
const FAIL_WINDOW_SEC = 600;
const MAX_STREAK_DAYS = 3660;
const PALETTE = ['#4f46e5', '#f59e0b', '#0d9488', '#e11d48', '#7c3aed', '#0284c7'];

const DEFAULT_RULES = [
  { id: 'rule-sleep', name: '저녁 10시 30분 취침', color: '#4f46e5', archived: false },
  { id: 'rule-wake', name: '아침 6시 기상', color: '#f59e0b', archived: false },
  { id: 'rule-leave', name: '7시 전 집 나서기', color: '#0d9488', archived: false }
];

/* ---------- Redis (Upstash REST) ---------- */

async function command(path, body) {
  const url = process.env.KV_REST_API_URL;
  const token = process.env.KV_REST_API_TOKEN;
  if (!url || !token) throw new Error('redis-not-configured');

  const options = { headers: { Authorization: 'Bearer ' + token } };
  if (body !== undefined) {
    options.method = 'POST';
    options.body = body;
  }

  const response = await fetch(url + path, options);
  if (!response.ok) throw new Error('redis-' + response.status);
  return response.json();
}

async function pipeline(commands) {
  const url = process.env.KV_REST_API_URL;
  const token = process.env.KV_REST_API_TOKEN;
  if (!url || !token) throw new Error('redis-not-configured');
  if (!commands.length) return [];

  const response = await fetch(url + '/pipeline', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify(commands)
  });
  if (!response.ok) throw new Error('redis-' + response.status);
  return response.json();
}

function field(date, ruleId) {
  return date + '|' + ruleId;
}

/* ---------- state helpers ---------- */

function pad2(n) { return String(n).length < 2 ? '0' + n : String(n); }

function dateKey(date) {
  return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate());
}

function addDays(date, delta) {
  const next = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  next.setDate(next.getDate() + delta);
  return next;
}

function isChecked(entries, key, ruleId) {
  const day = entries[key];
  return !!(day && day[ruleId] === true);
}

function computeStreak(entries, ruleId, day) {
  let cursor = isChecked(entries, dateKey(day), ruleId) ? day : addDays(day, -1);
  let streak = 0;
  while (streak < MAX_STREAK_DAYS && isChecked(entries, dateKey(cursor), ruleId)) {
    streak += 1;
    cursor = addDays(cursor, -1);
  }
  return streak;
}

function normalizeRules(raw) {
  const list = Array.isArray(raw) ? raw : [];
  const out = [];
  const seen = new Set();
  list.forEach(function (rule, index) {
    if (!rule || typeof rule !== 'object') return;
    if (typeof rule.name !== 'string' || !rule.name.trim()) return;
    const id = typeof rule.id === 'string' && rule.id ? rule.id : 'rule-' + index + '-' + crypto.randomBytes(3).toString('hex');
    if (seen.has(id)) return;
    seen.add(id);
    out.push({
      id: id,
      name: rule.name.trim().slice(0, 60),
      color: typeof rule.color === 'string' && /^#[0-9a-fA-F]{6}$/.test(rule.color) ? rule.color : PALETTE[out.length % PALETTE.length],
      archived: rule.archived === true
    });
  });
  return out;
}

function normalizeEntries(raw, rules) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  const known = new Set(rules.map(function (rule) { return rule.id; }));
  Object.keys(raw).forEach(function (date) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
    const day = raw[date];
    if (!day || typeof day !== 'object') return;
    const clean = {};
    Object.keys(day).forEach(function (ruleId) {
      if (day[ruleId] === true && known.has(ruleId)) clean[ruleId] = true;
    });
    if (Object.keys(clean).length) out[date] = clean;
  });
  return out;
}

function normalizeState(raw) {
  if (!raw || typeof raw !== 'object') return { version: 1, rules: [], entries: {} };
  const rules = normalizeRules(raw.rules);
  return { version: 1, rules: rules, entries: normalizeEntries(raw.entries, rules) };
}

function seedRules() {
  return normalizeRules(DEFAULT_RULES);
}

/* ---------- reads / writes ---------- */

async function readRules() {
  const reply = await command('/get/' + encodeURIComponent(RULES_KEY));
  if (reply && reply.result != null) {
    try {
      return normalizeRules(JSON.parse(reply.result));
    } catch (err) {
      return seedRules();
    }
  }

  const legacy = await command('/get/' + encodeURIComponent(LEGACY_KEY));
  if (legacy && legacy.result != null) {
    let migrated;
    try {
      migrated = normalizeState(JSON.parse(legacy.result));
    } catch (err) {
      migrated = { version: 1, rules: seedRules(), entries: {} };
    }
    if (!migrated.rules.length) migrated.rules = seedRules();
    await writeRules(migrated.rules);
    await replaceEntries(migrated.entries);
    await command('/del/' + encodeURIComponent(LEGACY_KEY));
    return migrated.rules;
  }

  const seeded = seedRules();
  await writeRules(seeded);
  return seeded;
}

async function readEntries() {
  const reply = await command('/hgetall/' + encodeURIComponent(CHECK_KEY));
  const flat = Array.isArray(reply && reply.result) ? reply.result : [];
  const entries = {};
  for (let i = 0; i + 1 < flat.length; i += 2) {
    const parts = String(flat[i]).split('|');
    if (parts.length !== 2) continue;
    const date = parts[0];
    const ruleId = parts[1];
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !ruleId) continue;
    if (!entries[date]) entries[date] = {};
    entries[date][ruleId] = true;
  }
  return entries;
}

async function readState() {
  const rules = await readRules();
  const entries = await readEntries();
  return { version: 1, rules: rules, entries: entries };
}

async function writeRules(rules) {
  await command('/set/' + encodeURIComponent(RULES_KEY), JSON.stringify(rules));
}

async function setCheck(date, ruleId, value) {
  const key = encodeURIComponent(CHECK_KEY);
  const name = field(date, ruleId);
  if (value) await command('/hset/' + key + '/' + encodeURIComponent(name) + '/1');
  else await command('/hdel/' + key + '/' + encodeURIComponent(name));
}

async function replaceEntries(entries) {
  const commands = [['DEL', CHECK_KEY]];
  Object.keys(entries).forEach(function (date) {
    Object.keys(entries[date]).forEach(function (ruleId) {
      if (entries[date][ruleId] === true) commands.push(['HSET', CHECK_KEY, field(date, ruleId), '1']);
    });
  });
  await pipeline(commands);
}

async function replaceAll(state) {
  const norm = normalizeState(state);
  if (!norm.rules.length) norm.rules = seedRules();
  const commands = [['DEL', CHECK_KEY], ['SET', RULES_KEY, JSON.stringify(norm.rules)]];
  Object.keys(norm.entries).forEach(function (date) {
    Object.keys(norm.entries[date]).forEach(function (ruleId) {
      commands.push(['HSET', CHECK_KEY, field(date, ruleId), '1']);
    });
  });
  await pipeline(commands);
}

/* ---------- auth ---------- */

function sameSecret(input, expected) {
  const a = crypto.createHash('sha256').update(String(input)).digest();
  const b = crypto.createHash('sha256').update(String(expected)).digest();
  return crypto.timingSafeEqual(a, b);
}

function passwordMatches(candidate) {
  const expected = process.env.CHECK_PASSWORD;
  if (!expected || typeof candidate !== 'string' || !candidate) return false;
  return sameSecret(candidate, expected);
}

function authSecret() {
  return process.env.AUTH_SECRET || '';
}

function sign(value) {
  return crypto.createHmac('sha256', authSecret()).update(String(value)).digest('base64url');
}

function issueToken() {
  const exp = Date.now() + TOKEN_TTL_MS;
  return exp + '.' + sign(exp);
}

function tokenValid(token) {
  if (!authSecret()) return false;
  const parts = String(token || '').split('.');
  if (parts.length !== 2) return false;
  const exp = Number(parts[0]);
  if (!Number.isFinite(exp) || exp < Date.now()) return false;
  const expected = sign(exp);
  const given = parts[1];
  if (given.length !== expected.length) return false;
  return crypto.timingSafeEqual(Buffer.from(given), Buffer.from(expected));
}

function bearer(req) {
  const header = req.headers && (req.headers.authorization || req.headers.Authorization);
  if (!header || !/^Bearer\s+/i.test(header)) return '';
  return header.replace(/^Bearer\s+/i, '').trim();
}

/* ---------- rate limit ---------- */

function clientIp(req) {
  const forwarded = req.headers && req.headers['x-forwarded-for'];
  const first = String(forwarded || '').split(',')[0].trim();
  return first || 'unknown';
}

async function tooManyFails(ip) {
  try {
    const reply = await command('/get/' + encodeURIComponent('life-rule:fail:' + ip));
    return Number((reply && reply.result) || 0) >= MAX_FAILS;
  } catch (err) {
    return false;
  }
}

async function recordFail(ip) {
  try {
    const key = 'life-rule:fail:' + ip;
    const reply = await command('/incr/' + encodeURIComponent(key));
    if (Number(reply && reply.result) === 1) {
      await command('/expire/' + encodeURIComponent(key) + '/' + FAIL_WINDOW_SEC);
    }
  } catch (err) { /* best effort */ }
}

async function clearFails(ip) {
  try {
    await command('/del/' + encodeURIComponent('life-rule:fail:' + ip));
  } catch (err) { /* best effort */ }
}

/* ---------- http helpers ---------- */

function send(res, status, payload) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(payload));
}

function readBody(req) {
  if (req.body && typeof req.body === 'object') return req.body;
  if (typeof req.body === 'string' && req.body) {
    try { return JSON.parse(req.body); } catch (err) { return null; }
  }
  return null;
}

module.exports = {
  readState: readState,
  readRules: readRules,
  readEntries: readEntries,
  writeRules: writeRules,
  setCheck: setCheck,
  replaceAll: replaceAll,
  normalizeState: normalizeState,
  normalizeRules: normalizeRules,
  computeStreak: computeStreak,
  dateKey: dateKey,
  passwordMatches: passwordMatches,
  issueToken: issueToken,
  tokenValid: tokenValid,
  bearer: bearer,
  clientIp: clientIp,
  tooManyFails: tooManyFails,
  recordFail: recordFail,
  clearFails: clearFails,
  send: send,
  readBody: readBody
};
