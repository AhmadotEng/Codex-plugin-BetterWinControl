/* SPDX-License-Identifier: MPL-2.0 */
(() => {
  "use strict";
  const VERSION = 1;
  const EXTENSION_ID = "zen-companion@betterwincontrol.local";
  const HOST_NAME = "local.betterwincontrol.zen";
  const OPS = new Set(["capabilities", "pair_candidates", "bind", "observe", "input", "verify", "pause", "resume", "stop"]);
  const ACTIONS = new Set(["select_tab", "navigate", "click", "set_value", "scroll", "media_pause", "media_play", "media_seek"]);
  const sensitive = /\b(password|passcode|credential|sign[ -]?in|log[ -]?in|two[ -]?factor|authentication|verify your identity)\b/i;
  const fail = (code, message = code) => { throw Object.assign(new Error(message), {code}); };
  function object(value) { return value && typeof value === "object" && !Array.isArray(value); }
  function token(value, field) {
    if (typeof value !== "string" || !/^[a-zA-Z0-9_.:-]{1,128}$/.test(value)) fail("invalid_argument", field);
  }
  function validate(request, now = Date.now()) {
    if (!object(request) || request.v !== VERSION || !OPS.has(request.op)) fail("invalid_request");
    if (Object.keys(request).some(k => !["v", "id", "sessionId", "epoch", "deadlineUnixMs", "op", "args"].includes(k))) fail("unknown_argument");
    token(request.id, "id"); token(request.sessionId, "sessionId");
    if (!Number.isSafeInteger(request.epoch) || request.epoch < 0) fail("invalid_epoch");
    if (!Number.isSafeInteger(request.deadlineUnixMs) || request.deadlineUnixMs <= now || request.deadlineUnixMs > now + 60000) fail("expired_or_invalid_deadline");
    if (!object(request.args)) fail("invalid_args");
    return request;
  }
  function webUrl(value) {
    if (typeof value !== "string" || value.length > 8192) fail("invalid_url");
    let u; try { u = new URL(value); } catch { fail("invalid_url"); }
    if (!["http:", "https:"].includes(u.protocol) || u.username || u.password) fail("unsupported_url");
    return u;
  }
  function safeUrl(value) { try { const u = webUrl(value); return u.origin + u.pathname; } catch { return "[non-web page]"; } }
  function originPattern(value) { return webUrl(value).origin + "/*"; }
  function allowedKeys(args, keys) { if (Object.keys(args).some(k => !keys.includes(k))) fail("unknown_argument"); }
  function positiveInt(value, field) { if (!Number.isSafeInteger(value) || value < 0) fail("invalid_argument", field); }
  globalThis.BWCProtocol = {VERSION, EXTENSION_ID, HOST_NAME, OPS, ACTIONS, sensitive, fail, object, token,
    validate, webUrl, safeUrl, originPattern, allowedKeys, positiveInt};
})();
