/* SPDX-License-Identifier: MPL-2.0 */
(() => {
  "use strict";
  const P = globalThis.BWCProtocol;
  class ZenAdapter {
    constructor(api, options = {}) {
      this.api = api;
      this.now = options.now || Date.now;
      this.uuid = options.uuid || (() => crypto.randomUUID());
      this.pairings = new Map();
      this.sessions = new Map();
      this.lastEpoch = new Map();
      this.serial = Promise.resolve();
      this.connectionGeneration = 0;
    }
    pair(windowId) {
      P.positiveInt(windowId, "windowId");
      for (const [id, pair] of this.pairings) if (pair.windowId === windowId || pair.expiresAt <= this.now()) this.pairings.delete(id);
      const pairingNonce = this.uuid();
      this.pairings.set(pairingNonce, {windowId, expiresAt: this.now() + 30000});
      return {pairingNonce, windowId, expiresAt: this.now() + 30000};
    }
    revokeWindow(windowId) {
      for (const [id, session] of this.sessions) if (session.windowId === windowId) this.revoke(id);
      for (const [id, pair] of this.pairings) if (pair.windowId === windowId) this.pairings.delete(id);
    }
    revoke(id) {
      const s = this.sessions.get(id);
      if (s) { s.revoked = true; s.paused = true; s.observation = null; this.lastEpoch.set(id, Math.max(s.epoch, this.lastEpoch.get(id) ?? -1)); }
      this.sessions.delete(id);
    }
    disconnect() {
      this.connectionGeneration++;
      for (const id of this.sessions.keys()) this.revoke(id);
      this.pairings.clear();
      this.lastEpoch.clear(); // Tombstones belong to a single authenticated transport generation.
      this.serial = Promise.resolve();
    }
    windowMetadata(win) {
      if (!win || win.incognito || win.type !== "normal") P.fail("unsupported_window");
      P.positiveInt(win.id, "windowId");
      if (![win.left, win.top, win.width, win.height].every(Number.isFinite) || win.width <= 0 || win.height <= 0) P.fail("window_geometry_unavailable");
      return {id: win.id, left: win.left, top: win.top, width: win.width, height: win.height, type: win.type, incognito: false};
    }
    guard(request, session, mutate = false) {
      if (this.now() >= request.deadlineUnixMs) P.fail("deadline_expired");
      if (!session || session.revoked || this.sessions.get(request.sessionId) !== session || request.epoch !== session.epoch) P.fail("session_revoked_or_stale");
      if (mutate && session.paused) P.fail("paused");
    }
    dispatch(request) {
      P.validate(request, this.now());
      // Revocations bypass the operation queue. A browser API call already submitted may finish.
      if (request.op === "stop" || request.op === "pause") return this.control(request);
      const generation = this.connectionGeneration;
      const run = this.serial.then(() => {
        if (generation !== this.connectionGeneration) P.fail("connection_revoked");
        return this.execute(request);
      });
      this.serial = run.catch(() => {});
      return run;
    }
    control(request) {
      P.allowedKeys(request.args, []);
      const s = this.sessions.get(request.sessionId);
      if (!s) {
        if (request.op === "stop") { this.lastEpoch.set(request.sessionId, Math.max(request.epoch, this.lastEpoch.get(request.sessionId) ?? -1)); return {stopped: true, inFlightMayFinish: true}; }
        P.fail("session_not_bound");
      }
      this.guard(request, s);
      s.observation = null;
      if (request.op === "stop") this.revoke(request.sessionId); else s.paused = true;
      return {stopped: request.op === "stop", paused: true, inFlightMayFinish: true};
    }
    async tab(session, tabId) {
      P.positiveInt(tabId, "tabId");
      const tab = await this.api.tabs.get(tabId);
      if (tab.windowId !== session.windowId || tab.incognito) P.fail("tab_out_of_scope");
      if (P.sensitive.test(tab.title || "")) P.fail("protected_page");
      return tab;
    }
    async content(request, session, tabId, command) {
      let tab = await this.tab(session, tabId);
      P.webUrl(tab.url);
      if (!await this.api.permissions.contains({origins: [P.originPattern(tab.url)]})) P.fail("site_permission_required");
      this.guard(request, session, command.op !== "observe" && command.op !== "verify");
      await this.api.scripting.executeScript({target: {tabId, frameIds: [0]}, files: ["page-handler.js"]});
      this.guard(request, session, command.op !== "observe" && command.op !== "verify");
      tab = await this.tab(session, tabId);
      if (!await this.api.permissions.contains({origins: [P.originPattern(tab.url)]})) P.fail("site_permission_required");
      this.guard(request, session, command.op !== "observe" && command.op !== "verify");
      const results = await this.api.scripting.executeScript({target: {tabId, frameIds: [0]},
        func: arg => globalThis.__betterWinControlPage.command(arg),
        args: [{...command, deadlineUnixMs: request.deadlineUnixMs}]});
      this.guard(request, session);
      if (results.length !== 1 || results[0].error || !results[0].result) P.fail("page_response_unavailable");
      if (results[0].result.error) P.fail(results[0].result.error.code, results[0].result.error.message);
      return results[0].result;
    }
    capabilities() {
      return {adapter: "zen-companion", version: "0.2.0", backend: "webextension", scope: "paired_window_main_web_frames",
        operations: [...P.ACTIONS], inputSemantics: "browser_api_or_dom_semantic",
        trustedPointerKeyboard: false, hover: false, drag: false, keyChords: false, fileInput: false,
        browserChrome: false, nativeDialogs: false, physicalInputInjection: false,
        limitations: ["Page operations require a per-site grant.", "DOM click is not trusted pointer input.", "Subframes and cross-origin iframe control are not implemented.", "In-flight browser operations cannot be rolled back by Stop."]};
    }
    async execute(request) {
      P.validate(request, this.now());
      const args = request.args;
      if (request.op === "capabilities") { P.allowedKeys(args, []); return this.capabilities(); }
      if (request.op === "pair_candidates") {
        P.allowedKeys(args, []);
        const generation = this.connectionGeneration;
        // Only the authenticated controller's explicit request enumerates windows. Never read tabs or page data here.
        const windows = await this.api.windows.getAll({populate: false, windowTypes: ["normal"]});
        if (generation !== this.connectionGeneration || this.now() >= request.deadlineUnixMs || request.epoch <= (this.lastEpoch.get(request.sessionId) ?? -1)) P.fail("binding_revoked");
        const candidates = [], seen = new Set();
        for (const win of windows) {
          if (win.incognito || win.type !== "normal" || seen.has(win.id)) continue;
          const window = this.windowMetadata(win);
          seen.add(win.id);
          candidates.push({...this.pair(win.id), window});
        }
        return {candidates};
      }
      if (request.op === "bind") {
        P.allowedKeys(args, ["windowId", "pairingNonce"]);
        P.positiveInt(args.windowId, "windowId"); P.token(args.pairingNonce, "pairingNonce");
        const pair = this.pairings.get(args.pairingNonce);
        if (!pair || pair.expiresAt <= this.now() || pair.windowId !== args.windowId) P.fail("pairing_required");
        if (request.epoch <= (this.lastEpoch.get(request.sessionId) ?? -1)) P.fail("stale_epoch");
        if (this.sessions.has(request.sessionId) || [...this.sessions.values()].some(s => s.windowId === args.windowId)) P.fail("window_already_bound");
        const win = await this.api.windows.get(args.windowId);
        const window = this.windowMetadata(win);
        if (this.now() >= request.deadlineUnixMs || this.now() >= pair.expiresAt || !this.pairings.has(args.pairingNonce) || request.epoch <= (this.lastEpoch.get(request.sessionId) ?? -1)) P.fail("binding_revoked");
        this.pairings.delete(args.pairingNonce);
        this.sessions.set(request.sessionId, {windowId: args.windowId, epoch: request.epoch, paused: false, revoked: false, observation: null, lastAction: null});
        return {bound: true, windowId: args.windowId, epoch: request.epoch, window};
      }
      const s = this.sessions.get(request.sessionId); this.guard(request, s);
      if (request.op === "resume") { P.allowedKeys(args, []); s.paused = false; s.observation = null; return {paused: false, observationRequired: true}; }
      if (request.op === "observe") {
        P.allowedKeys(args, ["tabId", "includePage", "maxElements"]);
        const tabs = (await this.api.tabs.query({windowId: s.windowId})).filter(t => !t.incognito);
        this.guard(request, s);
        let tabId = args.tabId ?? tabs.find(t => t.active)?.id;
        const tab = await this.tab(s, tabId);
        const page = args.includePage === true ? await this.content(request, s, tabId, {op: "observe", maxElements: args.maxElements ?? 120}) : null;
        this.guard(request, s);
        const observationId = this.uuid();
        s.observation = {id: observationId, tabId, url: tab.url, page, expiresAt: this.now() + 15000,
          tabs: new Map(tabs.map(t => [t.id, {url: t.url, windowId: t.windowId}]))};
        return {observationId, expiresAt: s.observation.expiresAt, windowId: s.windowId, tabId, paused: s.paused,
          tabs: tabs.map(t => ({id: t.id, active: t.active, title: (t.title || "").slice(0,300), url: P.safeUrl(t.url), discarded: !!t.discarded})), page};
      }
      if (request.op === "verify") {
        P.allowedKeys(args, ["actionId"]);
        if (!s.lastAction || s.lastAction.actionId !== args.actionId) P.fail("unknown_action");
        const last = s.lastAction;
        if (last.pageAction) {
          const result = await this.content(request, s, last.tabId, {op: "verify", actionId: last.pageAction});
          return {...result, actionId:last.actionId, pageActionId:result.actionId};
        }
        const tab = await this.tab(s, last.tabId); this.guard(request, s);
        const effectVerified = last.action === "select_tab" ? tab.active === true : tab.url === last.url && tab.status === "complete";
        return {actionId: last.actionId, effectVerified, pending: !effectVerified, active: tab.active, url: P.safeUrl(tab.url), status: tab.status};
      }
      if (request.op !== "input") P.fail("unsupported_operation");
      P.allowedKeys(args, ["observationId", "action", "tabId", "elementId", "value", "url", "x", "y", "seconds"]);
      if (!P.ACTIONS.has(args.action)) P.fail("unsupported_input");
      const obs = s.observation;
      if (!obs || args.observationId !== obs.id || obs.expiresAt <= this.now()) P.fail("stale_observation");
      const tabId = args.tabId ?? obs.tabId;
      const tab = await this.tab(s, tabId);
      const original = obs.tabs.get(tabId);
      if (!original || original.url !== tab.url) P.fail("tab_changed");
      this.guard(request, s, true);
      const actionId = this.uuid();
      s.observation = null;
      if (args.action === "select_tab") {
        await this.api.tabs.update(tabId, {active: true});
        this.guard(request, s);
        s.lastAction = {actionId, tabId, action: args.action};
        return {actionId, submitted: true, effectVerified: false, mechanism: "tabs.update(active)", verifyRequired: true};
      }
      if (args.action === "navigate") {
        const url = P.webUrl(args.url).href;
        if (!await this.api.permissions.contains({origins: [P.originPattern(url)]})) P.fail("site_permission_required");
        this.guard(request, s, true);
        await this.api.tabs.update(tabId, {url}); this.guard(request, s);
        s.lastAction = {actionId, tabId, action: args.action, url};
        return {actionId, submitted: true, effectVerified: false, mechanism: "tabs.update(url)", verifyRequired: true};
      }
      if (tabId !== obs.tabId || !obs.page) P.fail("page_observation_required");
      const result = await this.content(request, s, tabId, {op: "input", observationId: obs.page.observationId,
        documentId: obs.page.documentId, action: args.action, elementId: args.elementId,
        value: args.value, x: args.x, y: args.y, seconds: args.seconds});
      s.lastAction = {actionId, tabId, action: args.action, pageAction: result.actionId};
      return {...result, actionId, pageActionId: result.actionId, verifyRequired: true};
    }
  }
  globalThis.BWCZenAdapter = ZenAdapter;
})();
