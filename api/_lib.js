'use strict';

const crypto = require('node:crypto');

const RULES_KEY = 'life-rule:rules';
const CHECK_KEY = 'life-rule:check';
const LEGACY_KEY = 'life-rule:state';
const MAX_STREAK_DAYS = 3660;
const PALETTE = ['#4f46e5', '#f59e0b', '#0d9488', '#e11d48', '#7c3aed', '#0284c7'];
const ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

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

function newRuleId(index) {
  return 'rule-' + index + '-' + crypto.randomBytes(3).toString('hex');
}

function normalizeRules(raw) {
  const list = Array.isArray(raw) ? raw : [];
  const out = [];
  const seen = new Set();
  list.forEach(function (rule, index) {
    if (!rule || typeof rule !== 'object') return;
    if (typeof rule.name !== 'string' || !rule.name.trim()) return;
    // 체크 해시 필드는 `date|ruleId`이므로 id에 구분자나 경로 문자를 허용하지 않는다.
    const id = typeof rule.id === 'string' && ID_PATTERN.test(rule.id) ? rule.id : newRuleId(index);
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
    let parsed = null;
    try {
      parsed = normalizeRules(JSON.parse(reply.result));
    } catch (err) {
      parsed = null;
    }
    if (parsed && parsed.length) return parsed;
    // 저장된 규칙이 비었거나 깨졌으면 시드를 실제로 다시 저장한다.
    const repaired = seedRules();
    await writeRules(repaired);
    return repaired;
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
  send: send,
  readBody: readBody
};
