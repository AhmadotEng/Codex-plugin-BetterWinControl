"use strict";
async function refresh() {
  const data = await browser.storage.local.get(["status", "readinessEnabled"]);
  document.querySelector("#status").textContent = data.status || "Starting local connection…";
  document.querySelector("#enable").hidden = data.readinessEnabled !== false;
  document.querySelector("#stop").hidden = data.readinessEnabled === false;
}
browser.storage.onChanged.addListener((changes, area) => {if (area === "local" && (changes.status || changes.readinessEnabled)) refresh();});
document.querySelector("#stop").addEventListener("click", async () => {await browser.runtime.sendMessage({type: "disable_readiness"}); await refresh();});
document.querySelector("#enable").addEventListener("click", async () => {await browser.runtime.sendMessage({type: "enable_readiness"}); await refresh();});
refresh();
