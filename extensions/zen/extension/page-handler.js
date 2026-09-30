/* SPDX-License-Identifier: MPL-2.0. Runs in this extension's isolated content world. */
(() => {
  "use strict";
  if (globalThis.__betterWinControlPage?.version === "0.1.0") return;
  const documentId = crypto.randomUUID();
  const sensitive = /password|passcode|credential|token|secret|one.?time|credit.?card|card.?number|cvv|cvc|social.?security|otp/i;
  const protectedPage = /\b(sign[ -]?in|log[ -]?in|authentication|verify your identity)\b/i;
  let observation = null, lastAction = null;
  const error = code => { throw Object.assign(new Error(code), {code}); };
  const clampText = (text, n = 300) => String(text || "").replace(/\s+/g, " ").trim().slice(0,n);
  const visible = el => {
    const r = el.getBoundingClientRect(), style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none" &&
      r.bottom >= 0 && r.right >= 0 && r.top <= innerHeight && r.left <= innerWidth;
  };
  function protect(el) {
    if (protectedPage.test(document.title) || [...document.querySelectorAll('input[type="password"]')].some(visible)) error("protected_page");
    if (!el || !el.isConnected || el.ownerDocument !== document) error("stale_element");
    if (el.closest('[inert],[aria-hidden="true"]')) error("protected_or_hidden_element");
    if (sensitive.test([el.type, el.name, el.id, el.autocomplete, el.getAttribute("aria-label")].join(" ")) ||
      /username|current-password|new-password|one-time-code|cc-/.test(el.autocomplete || "")) error("protected_element");
  }
  function label(el) {
    return clampText(el.getAttribute("aria-label") || el.labels?.[0]?.innerText ||
      el.getAttribute("title") || el.getAttribute("alt") || (el.tagName === "INPUT" ? el.placeholder : el.innerText));
  }
  function rectangle(el) { const r = el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; }
  function fingerprint(el) {
    const media = el instanceof HTMLMediaElement;
    return JSON.stringify([el.tagName, el.getAttribute("type"), el.getAttribute("role"), label(el), rectangle(el),
      !media && "value" in el ? el.value : null, !!el.disabled, !!el.readOnly]);
  }
  function actions(el) {
    if (el instanceof HTMLMediaElement) return ["media_pause", "media_play", "media_seek"];
    const result = [];
    if (el.matches('a[href],button,input[type="button"],input[type="submit"],[role="button"]')) result.push("click");
    if (el.matches('input:not([type]),input[type="text"],input[type="search"],input[type="email"],input[type="url"],input[type="tel"],textarea') && !el.readOnly && !el.disabled) result.push("set_value");
    if (el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth) result.push("scroll");
    return result;
  }
  function observe(args) {
    protect(document.documentElement);
    const max = args.maxElements ?? 120;
    if (!Number.isSafeInteger(max) || max < 1 || max > 200) error("invalid_max_elements");
    const elements = [{id:"viewport",role:"viewport",name:clampText(document.title),actions:["scroll"],bounds:{x:0,y:0,width:innerWidth,height:innerHeight}}];
    const bindings = new Map();
    const candidates = document.querySelectorAll('a[href],button,input,textarea,select,video,audio,[role],h1,h2,h3,p');
    let scanned = 0;
    for (const el of candidates) {
      if (++scanned > 2000 || elements.length >= max) break;
      if (!visible(el)) continue;
      try { protect(el); } catch { continue; }
      const id = "p" + elements.length;
      const permitted = actions(el);
      const state = el instanceof HTMLMediaElement ? {paused:el.paused,currentTime:el.currentTime,duration:Number.isFinite(el.duration)?el.duration:null,muted:el.muted} :
        permitted.includes("set_value") ? {value:clampText(el.value,2000),readOnly:!!el.readOnly} : {};
      elements.push({id,role:el.getAttribute("role") || el.tagName.toLowerCase(),name:label(el),actions:permitted,bounds:rectangle(el),disabled:!!el.disabled,state});
      bindings.set(id, {el,fingerprint:fingerprint(el),actions:permitted});
    }
    observation = {id:crypto.randomUUID(),bindings,expiresAt:Date.now()+15000,scrollX,scrollY};
    return {documentId,observationId:observation.id,expiresAt:observation.expiresAt,title:clampText(document.title),
      viewport:{width:innerWidth,height:innerHeight,devicePixelRatio,scrollX,scrollY},elements,
      truncated:scanned < candidates.length,scope:"visible_main_frame",inputSemantics:"DOM semantic operations, not trusted device events"};
  }
  function requireObservation(args) {
    if (!observation || args.documentId !== documentId || args.observationId !== observation.id || Date.now() >= observation.expiresAt) error("stale_page_observation");
    protect(document.documentElement);
    if (observation.scrollX !== scrollX || observation.scrollY !== scrollY) error("page_scrolled_since_observation");
    if (args.elementId === "viewport") return {el:document.scrollingElement,actions:["scroll"],viewport:true};
    const binding = observation.bindings.get(args.elementId);
    if (!binding) error("unknown_page_element");
    protect(binding.el);
    if (binding.fingerprint !== fingerprint(binding.el) || !visible(binding.el)) error("stale_element");
    return binding;
  }
  async function input(args) {
    const binding = requireObservation(args), el = binding.el;
    if (!binding.actions.includes(args.action)) error("unsupported_element_action");
    if (el.disabled) error("disabled_element");
    const id = crypto.randomUUID();
    const data = {actionId:id,action:args.action,el,documentId};
    if (args.action === "set_value") {
      if (typeof args.value !== "string" || args.value.length > 10000 || args.value.includes("\0")) error("invalid_value");
      data.expected = args.value;
    } else if (args.action === "media_seek") {
      if (!Number.isFinite(args.seconds) || args.seconds < 0 || !Number.isFinite(el.duration) || args.seconds > el.duration) error("invalid_media_time");
      data.expected = args.seconds;
    } else if (args.action === "scroll") {
      if (!Number.isFinite(args.x ?? 0) || !Number.isFinite(args.y ?? 0) || Math.abs(args.x ?? 0) > 4000 || Math.abs(args.y ?? 0) > 4000) error("invalid_scroll");
      data.before = [el.scrollLeft,el.scrollTop];
    } else if (args.action === "click" && el.matches("a[href]")) {
      const u = new URL(el.href);
      if (!["https:","http:"].includes(u.protocol) || u.username || u.password || el.target && el.target !== "_self") error("unsupported_link_target");
    }
    if (Date.now() >= args.deadlineUnixMs) error("deadline_expired");
    observation = null; // Reusing the element token is forbidden even if a provider throws.
    lastAction = data;
    switch (args.action) {
      case "set_value": {
        const prototype = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype,"value").set.call(el,args.value);
        el.dispatchEvent(new InputEvent("input",{bubbles:true,inputType:"insertReplacementText",data:args.value}));
        el.dispatchEvent(new Event("change",{bubbles:true})); break;
      }
      case "click": el.click(); break;
      case "scroll": el.scrollBy({left:args.x ?? 0,top:args.y ?? 0,behavior:"instant"}); break;
      case "media_pause": el.pause(); break;
      case "media_play": await el.play(); break;
      case "media_seek": el.currentTime=args.seconds; break;
      default: error("unsupported_input");
    }
    return {actionId:id,submitted:true,effectVerified:false,mechanism:"DOM."+args.action,trustedInput:false};
  }
  function verify(args) {
    if (!lastAction || lastAction.actionId !== args.actionId) error("unknown_page_action");
    const a=lastAction, el=a.el; protect(el);
    let effectVerified=null, state={};
    switch(a.action) {
      case "set_value": effectVerified=el.value===a.expected; state={valueMatches:effectVerified}; break;
      case "media_pause": effectVerified=el.paused; state={paused:el.paused}; break;
      case "media_play": effectVerified=!el.paused; state={paused:el.paused}; break;
      case "media_seek": effectVerified=Math.abs(el.currentTime-a.expected)<1; state={currentTime:el.currentTime}; break;
      case "scroll": effectVerified=el.scrollLeft!==a.before[0]||el.scrollTop!==a.before[1]; state={scrollX:el.scrollLeft,scrollY:el.scrollTop}; break;
      case "click": state={note:"Generic click effects require a fresh observation; successful dispatch is insufficient."}; break;
    }
    return {actionId:a.actionId,effectVerified,state,documentId};
  }
  globalThis.__betterWinControlPage = {version:"0.1.0",async command(args) {
    try {
      if (!args || !Number.isSafeInteger(args.deadlineUnixMs) || Date.now() >= args.deadlineUnixMs || args.deadlineUnixMs > Date.now()+60000) error("deadline_expired");
      if (args.op === "observe") return observe(args);
      if (args.op === "input") return await input(args);
      if (args.op === "verify") return verify(args);
      error("unsupported_operation");
    } catch(ex) { return {error:{code:ex.code || "page_operation_failed",message:ex.code || "Page operation failed; observe before retrying."}}; }
  }};
})();
