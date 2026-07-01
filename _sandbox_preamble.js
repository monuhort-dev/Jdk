'use strict';
/**
 * BotHost process sandbox — Node.js edition.
 *
 * Loaded via:  node --require /path/to/_sandbox_preamble.js entry.js
 *
 * Blocked zone
 * ────────────
 *   Everything inside BOTHOST_APP_DIR  (e.g. /…/artifacts/bot-hosting/)
 *   …EXCEPT this server's own folder:  BOTHOST_SANDBOX_DIR
 *                                       (e.g. /…/uploads/<server_id>/)
 *
 * Bots CANNOT access:
 *   • app.py, templates/, static/        ← web source code
 *   • _sandbox_preamble.py / .js         ← sandbox scripts
 *   • uploads/<other_server_id>/         ← other users' files
 *
 * Bots CAN access:
 *   • uploads/<own_server_id>/           ← their own folder only
 *   • /nix/, /usr/, /lib/, /tmp/, etc.   ← system / runtime paths
 *
 * Environment variables (set by BotHost before launch):
 *   BOTHOST_SANDBOX_DIR  – this server's own upload dir (absolute)
 *   BOTHOST_APP_DIR      – root of the bot-hosting web app (absolute)
 */

const path = require('path');
const fs   = require('fs');
const url  = require('url');

const SEP     = path.sep;
const BOT_DIR = path.resolve(process.env.BOTHOST_SANDBOX_DIR);
const APP_DIR = path.resolve(process.env.BOTHOST_APP_DIR);

function resolvePath(p) {
  if (p === null || p === undefined) return null;
  if (typeof p === 'number') return null;   // file descriptor — skip
  let s;
  if (typeof p === 'string') {
    s = p;
  } else if (Buffer.isBuffer(p)) {
    s = p.toString();
  } else if (p instanceof URL || (p && typeof p === 'object' && p.href)) {
    try { s = url.fileURLToPath(p); } catch { return null; }
  } else {
    s = String(p);
  }
  try { return path.resolve(s); } catch { return null; }
}

function isBlocked(resolved) {
  if (!resolved) return false;
  // Only restrict paths inside the app directory
  const inApp = resolved === APP_DIR || resolved.startsWith(APP_DIR + SEP);
  if (!inApp) return false;
  // Inside the app dir: allow only this bot's own upload folder
  const inOwn = resolved === BOT_DIR || resolved.startsWith(BOT_DIR + SEP);
  return !inOwn;
}

function checkPath(p) {
  const resolved = resolvePath(p);
  if (resolved && isBlocked(resolved)) {
    const err = new Error(
      `[BotHost Sandbox] Access denied: '${p}' is outside your server's directory.`
    );
    Object.assign(err, { code: 'EACCES', syscall: 'open', path: String(p) });
    throw err;
  }
}

// ─── Patch fs (callback + sync) ──────────────────────────────────────────────

const PATH_METHODS = [
  'access', 'accessSync',
  'stat', 'statSync',
  'lstat', 'lstatSync',
  'readdir', 'readdirSync',
  'opendir', 'opendirSync',
  'open', 'openSync',
  'readFile', 'readFileSync',
  'createReadStream',
  'realpath', 'realpathSync',
  'writeFile', 'writeFileSync',
  'appendFile', 'appendFileSync',
  'createWriteStream',
  'rename', 'renameSync',
  'copyFile', 'copyFileSync',
  'unlink', 'unlinkSync',
  'rm', 'rmSync',
  'rmdir', 'rmdirSync',
  'mkdir', 'mkdirSync',
  'mkdtemp', 'mkdtempSync',
  'symlink', 'symlinkSync',
  'link', 'linkSync',
  'readlink', 'readlinkSync',
  'truncate', 'truncateSync',
  'utimes', 'utimesSync',
  'chmod', 'chmodSync',
  'chown', 'chownSync',
  'watch', 'watchFile',
];

for (const name of PATH_METHODS) {
  if (typeof fs[name] !== 'function') continue;
  const orig = fs[name];
  fs[name] = function sandboxedFs(firstArg, ...rest) {
    checkPath(firstArg);
    return orig.call(fs, firstArg, ...rest);
  };
  Object.defineProperty(fs[name], 'name', { value: name });
  for (const [k, v] of Object.entries(orig)) {
    try { fs[name][k] = v; } catch { /* ignore */ }
  }
}

// ─── Patch fs.promises ───────────────────────────────────────────────────────

if (fs.promises) {
  const PROMISE_METHODS = [
    'access', 'stat', 'lstat',
    'readdir', 'opendir',
    'open', 'readFile', 'realpath',
    'writeFile', 'appendFile',
    'rename', 'copyFile',
    'unlink', 'rm', 'rmdir',
    'mkdir', 'mkdtemp',
    'symlink', 'link', 'readlink',
    'truncate', 'utimes', 'chmod', 'chown',
  ];
  for (const name of PROMISE_METHODS) {
    if (typeof fs.promises[name] !== 'function') continue;
    const orig = fs.promises[name];
    fs.promises[name] = async function sandboxedFsPromise(firstArg, ...rest) {
      checkPath(firstArg);
      return orig.call(fs.promises, firstArg, ...rest);
    };
  }
}
