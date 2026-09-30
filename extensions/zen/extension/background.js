/* SPDX-License-Identifier: MPL-2.0 */
(() => {
  "use strict";
  const P = globalThis.BWCProtocol, adapter = new globalThis.BWCZenAdapter(browser);
  const retryDelays = [250, 500, 1000, 2000, 5000, 10000];
  let port = null, readinessEnabled = false, transportConnected = false;
  let retryTimer = null, retryAttempt = 0, transportEpoch = 0, initializePromise;
  let statusQueue = Promise.resolve();
  const status = (phase, text, badge = "") => {
    const data = {phase, status: text, readinessEnabled, updatedAt: Date.now()};
    statusQueue = statusQueue.catch(() => {}).then(async () => {
      await browser.storage.local.set(data);
      await browser.action.setBadgeText({text: badge});
      await browser.action.setTitle({title: text});
    });
    return statusQueue;
  };
  const readyStatus = () => status("ready", "BetterWinControl ready — waiting for your request");
  function revokeTransport() {
    transportConnected = false;
    transportEpoch++;
    adapter.disconnect();
  }
  function cancelRetry() {
    if (retryTimer !== null) clearTimeout(retryTimer);
    retryTimer = null;
  }
  function scheduleRetry() {
    if (!readinessEnabled || port || retryTimer !== null) return;
    const delay = retryDelays[Math.min(retryAttempt++, retryDelays.length - 1)];
    retryTimer = setTimeout(() => {retryTimer = null; connect();}, delay);
    status("reconnecting", "BetterWinControl reconnecting to its local helper", "…").catch(() => {});
  }
  function sendEvent(event) {
    if (!port || !transportConnected) return;
    try {port.postMessage({v: 1, type: "event", ...event});} catch { /* Disconnect listener schedules recovery. */ }
  }
  function connect() {
    if (!readinessEnabled || port) return;
    cancelRetry();
    let owned;
    try {owned = browser.runtime.connectNative(P.HOST_NAME);} catch {scheduleRetry(); return;}
    port = owned;
    owned.onDisconnect.addListener(() => {
      if (port !== owned) return;
      port = null;
      revokeTransport();
      scheduleRetry();
    });
    owned.onMessage.addListener(async request => {
      if (port !== owned || !readinessEnabled) return;
      if (request?.v === 1 && request.type === "transport") {
        if (request.state === "waiting") {
          revokeTransport();
          retryAttempt = 0;
          await readyStatus();
        } else if (request.state === "connected") {
          transportConnected = true;
          retryAttempt = 0;
          await readyStatus();
        }
        return;
      }
      if (!transportConnected) return;
      const epoch = transportEpoch;
      const current = () => port === owned && readinessEnabled && transportConnected && epoch === transportEpoch;
      try {
        const result = await adapter.dispatch(request);
        if (!current()) return;
        owned.postMessage({v: 1, type: "reply", id: request.id, sessionId: request.sessionId, epoch: request.epoch, result});
        if (request.op === "bind") await status("connected", "BetterWinControl connected to the requested window", "ON");
        if (request.op === "pause") await status("paused", "BetterWinControl control paused", "Ⅱ");
        if (request.op === "resume") await status("connected", "BetterWinControl connected to the requested window", "ON");
        if (request.op === "stop") await readyStatus();
      } catch(ex) {
        if (current()) owned.postMessage({v: 1, type: "reply", id: request?.id, error: {code: ex.code || "adapter_error", message: ex.code || "Operation failed; re-observe before retrying."}});
      }
    });
    try {owned.postMessage({v: 1, type: "hello", extensionId: P.EXTENSION_ID, version: browser.runtime.getManifest().version});}
    catch {if (port === owned) {port = null; revokeTransport(); try {owned.disconnect();} catch {} scheduleRetry();}}
  }
  function initialize() {
    if (!initializePromise) initializePromise = (async () => {
      const saved = await browser.storage.local.get("readinessEnabled");
      readinessEnabled = saved.readinessEnabled !== false;
      if (readinessEnabled) {await readyStatus(); connect();}
      else await status("disabled", "BetterWinControl readiness disabled", "OFF");
    })();
    return initializePromise;
  }
  async function setReadiness(enabled) {
    await initialize();
    readinessEnabled = enabled;
    if (!enabled) {
      cancelRetry();
      sendEvent({event: "user_stop"});
      const owned = port;
      port = null;
      revokeTransport();
      try {owned?.disconnect();} catch {}
      await status("disabled", "BetterWinControl readiness disabled", "OFF");
    } else {
      retryAttempt = 0;
      await browser.storage.local.set({readinessEnabled: true});
      if (!port) {await readyStatus(); connect();}
    }
    return {readinessEnabled, stopped: !enabled};
  }
  // Register synchronously: Firefox wakes this event page at browser startup.
  // An open native port keeps it alive; the persistent helper waits without polling pages.
  browser.runtime.onStartup.addListener(() => initialize());
  browser.action.onClicked.addListener(async tab => {
    try {
      // permissions.request must run directly in the user gesture, before any await.
      if (tab?.incognito) P.fail("unsupported_window");
      const grant = tab?.url && /^https?:/.test(tab.url) ? browser.permissions.request({origins: [P.originPattern(tab.url)]}) : Promise.resolve(false);
      await grant;
      await setReadiness(true);
    } catch(ex) {await status("error", "BetterWinControl: " + (ex.code || "Site access was not granted"), "!");}
  });
  browser.windows.onRemoved.addListener(windowId => {adapter.revokeWindow(windowId); sendEvent({event: "window_closed", windowId});});
  browser.permissions.onRemoved.addListener(() => {transportEpoch++; adapter.disconnect(); sendEvent({event: "permissions_revoked"}); if (readinessEnabled) readyStatus().catch(() => {});});
  browser.runtime.onMessage.addListener((message, sender) => {
    // Options may be open in a tab. Authorize our exact extension page, never a web content script.
    if (sender.id !== P.EXTENSION_ID || sender.url !== browser.runtime.getURL("options.html") || (sender.frameId !== undefined && sender.frameId !== 0)) return undefined;
    if (message?.type === "local_stop" || message?.type === "disable_readiness") return setReadiness(false);
    if (message?.type === "enable_readiness") return setReadiness(true);
    return undefined;
  });
  initialize().catch(() => {status("error", "BetterWinControl could not read readiness settings", "!").catch(() => {});});
})();
