const {JSDOM, VirtualConsole} = require("jsdom"); const fs = require("fs");
const BASE = process.env.BASE || "http://localhost:8061/"; const errors = []; const vc = new VirtualConsole();
vc.on("jsdomError", e => errors.push("jsdomError: " + (e.detail && e.detail.stack || e.message))); vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const dom = await JSDOM.fromURL(BASE, {runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o);
      w.Element.prototype.getBoundingClientRect = function () { return {width: 1000, height: 760, left: 0, top: 0, right: 1000, bottom: 760}; };
      w.HTMLElement.prototype.setPointerCapture = () => {};
      w.addEventListener("error", e => errors.push("window.error: " + e.message)); w.addEventListener("unhandledrejection", e => errors.push("unhandledrejection: " + e.reason)); }});
  const w = dom.window, d = w.document, S = w.eval("S");
  const until = async (fn, ms = 30000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(80); } return false; };
  const shot = n => fs.writeFileSync(`/tmp/uitest/${n}.png`, Buffer.from(d.getElementById("map").toDataURL().split(",")[1], "base64"));
  const cv = d.getElementById("map");
  const ptr = (type, x, y) => cv.dispatchEvent(new w.MouseEvent(type, {clientX: x, clientY: y, bubbles: true}));
  const click = (wx, wy) => { const [px, py] = w.eval(`w2s(${wx},${wy})`); ptr("pointerdown", px, py); ptr("pointerup", px, py); };
  const settle = async () => { await until(() => S.busy === true, 800); await until(() => S.busy === false); await sleep(200); };
  const ok = (name, cond, extra = "") => console.log((cond ? "PASS " : "FAIL ") + name + (extra ? "  [" + extra + "]" : ""));
  await until(() => S.meta && S.imgs.base.naturalWidth); await sleep(200);

  // 1. click-to-plan
  d.querySelector('#placeSeg [data-p="goal"]').click();
  click(2000, 1000); await settle();
  ok("click on map sets goal and auto-plans", S.goal && S.goal.x === 2000 && S.plan && S.plan.verdict.startsWith("GO"), S.plan && S.plan.verdict + ", " + S.plan.distance_km + " km");
  // 2. outside the map is rejected with a message
  const before = JSON.stringify(S.goal); click(99999, 0);
  ok("click outside the terrain is refused", JSON.stringify(S.goal) === before && d.getElementById("msg").style.display === "block", d.getElementById("msg").textContent);
  // 3. zoom keeps the point under the cursor
  const k0 = S.view.k; const [wx0, wy0] = w.eval("s2w(500,380)"); cv.dispatchEvent(new w.WheelEvent("wheel", {clientX: 500, clientY: 380, deltaY: -300, bubbles: true, cancelable: true}));
  const [wx1, wy1] = w.eval("s2w(500,380)"); ok("wheel zoom in, cursor-anchored", S.view.k > k0 && Math.abs(wx1 - wx0) < 1 && Math.abs(wy1 - wy0) < 1, `k ${k0.toFixed(3)} -> ${S.view.k.toFixed(3)}`);
  // 4. pan by dragging
  const cx0 = S.view.cx; ptr("pointerdown", 400, 300); ptr("pointermove", 500, 300); ptr("pointerup", 500, 300); ok("drag pans the map", Math.abs(S.view.cx - cx0) > 1, `cx ${cx0.toFixed(0)} -> ${S.view.cx.toFixed(0)}`);
  // 5. layers
  for (const l of ["nogo_walk", "nogo_rover", "slope", "localizability"]) { const c = d.querySelector(`[data-layer="${l}"]`); c.checked = true; c.dispatchEvent(new w.Event("change")); }
  ok("overlay layers load", await until(() => ["nogo_walk", "nogo_rover", "slope", "localizability"].every(l => S.imgs[l] && S.imgs[l].complete && S.imgs[l].naturalWidth)));
  // 6. colour modes + halo + waypoints
  for (const m of ["difficulty", "margin", "sigma"]) { d.getElementById("colorBy").value = m; d.getElementById("colorBy").dispatchEvent(new w.Event("change")); }
  d.getElementById("showWp").checked = true; d.getElementById("showWp").dispatchEvent(new w.Event("change")); ok("colour modes / waypoints draw without error", errors.length === 0, errors[0] || "");
  // 7. hazard + D* replan
  d.querySelector('#placeSeg [data-p="hazard"]').click();
  const rt = S.plan.route.enu, mid = rt[rt.length >> 1]; w.eval("fitRoute()"); click(mid[0], mid[1]);
  ok("hazard added", S.hazards.length === 1 && d.getElementById("replanBtn").disabled === false);
  const etaBefore = S.plan.eta_min; d.getElementById("replanBtn").click(); await settle();
  const haz = S.hazards[0], minD = Math.min(...S.plan.route.enu.map(p => Math.hypot(p[0] - haz.x, p[1] - haz.y)));
  ok("D* Lite replan routes around the hazard", minD >= haz.r, `closest approach ${minD.toFixed(0)} m vs radius ${haz.r} m; ETA ${etaBefore.toFixed(1)} -> ${S.plan.eta_min.toFixed(1)} min; ${S.note}`);
  shot("inter_replan");
  // 8. plain plan also respects the hazard
  d.getElementById("planBtn").click(); await settle(); const minD2 = Math.min(...S.plan.route.enu.map(p => Math.hypot(p[0] - haz.x, p[1] - haz.y)));
  ok("'Plan' respects placed hazards", minD2 >= haz.r, `closest ${minD2.toFixed(0)} m`);
  // 9. conditions change the answer
  S.hazards = []; w.eval("updateTexts()"); d.getElementById("planBtn").click(); await settle(); const eta0 = S.plan.eta_min;
  d.getElementById("dustOn").checked = true; d.getElementById("dustTau").value = 5; d.getElementById("dustPeak").value = 30; d.getElementById("dustOn").dispatchEvent(new w.Event("input"));
  ok("changing inputs marks the result stale", d.getElementById("staleNote").style.display === "block");
  d.getElementById("planBtn").click(); await settle(); ok("dust storm slows the route", S.plan.eta_min > eta0 * 1.05, `${eta0.toFixed(0)} -> ${S.plan.eta_min.toFixed(0)} min`);
  // 10. SEP now
  d.getElementById("sepNow").click(); await settle(); ok("SEP alert -> shelter route", S.plan.verdict.startsWith("SHELTER"), S.plan.verdict + ", " + (S.plan.sep.countdown_s / 60).toFixed(0) + " min before onset");
  // 11. server-side error is shown, not swallowed
  S.goal = {x: 90000, y: 0}; d.getElementById("planBtn").click(); await settle();
  ok("server error shown to the user", d.getElementById("msg").className === "err" && /outside the terrain/.test(d.getElementById("msg").textContent), d.getElementById("msg").textContent.slice(0, 90));
  // 11b. far point: refused with a clear reason; with the return rule off it plans a plain path
  d.getElementById("dustOn").checked = false; d.getElementById("dustOn").dispatchEvent(new w.Event("input"));
  d.getElementById("clearBtn").click(); S.start = {x: 0, y: 0}; S.goal = {x: 5600, y: 5600}; S.mode = "walk"; w.eval("setSeg('modeSeg','m','walk')");
  d.getElementById("planBtn").click(); await settle();
  ok("far point refused with an explanation and a tip", d.getElementById("msg").className === "err" && /(too far from the shelter|round trip)/.test(d.getElementById("msg").textContent) && /untick/.test(d.getElementById("msg").textContent), d.getElementById("msg").textContent.slice(0, 80));
  d.getElementById("retEnf").checked = false; d.getElementById("retEnf").dispatchEvent(new w.Event("input"));
  d.getElementById("planBtn").click(); await settle();
  ok("return rule off: plain path planned, margin shown as not checked", S.plan && /NOT checked/.test(S.plan.verdict) && /not checked/.test(d.getElementById("results").textContent), S.plan && S.plan.verdict + ", " + S.plan.distance_km + " km");
  d.getElementById("retEnf").checked = true; d.getElementById("retEnf").dispatchEvent(new w.Event("input"));
  // 12. clear
  d.getElementById("clearBtn").click(); ok("clear resets", S.plan === null && S.goal === null);
  // 13. landmark dropdown
  const sel = d.getElementById("landmarks"); sel.value = "1"; sel.dispatchEvent(new w.Event("change")); await settle(); ok("landmark picker plans a route", !!S.plan, S.plan && S.plan.verdict);
  console.log("JS errors:", errors.length ? "\n" + errors.join("\n") : "none"); process.exit(0);
})();
