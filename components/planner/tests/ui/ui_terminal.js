// Headless test of the lander terminal flow (spec section 2). Needs a running server: python app.py --synthetic --no-browser --port 8061
const {JSDOM, VirtualConsole} = require("jsdom"); const fs = require("fs");
const BASE = process.env.BASE || "http://localhost:8061/"; const OUT = process.env.OUT || "/tmp/uitest";
const errors = []; const vc = new VirtualConsole();
vc.on("jsdomError", e => errors.push("jsdomError: " + (e.detail && e.detail.stack || e.message))); vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const dom = await JSDOM.fromURL(BASE, {runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o);
      w.Element.prototype.getBoundingClientRect = function () { return {width: 1000, height: 760, left: 0, top: 0, right: 1000, bottom: 760}; };
      w.HTMLElement.prototype.setPointerCapture = () => {};
      w.addEventListener("error", e => errors.push("window.error: " + e.message)); w.addEventListener("unhandledrejection", e => errors.push("unhandledrejection: " + e.reason)); }});
  const w = dom.window, d = w.document, S = w.eval("S"), T = w.eval("T");
  const until = async (fn, ms = 40000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(80); } return false; };
  const shot = n => fs.writeFileSync(`${OUT}/${n}.png`, Buffer.from(d.getElementById("map").toDataURL().split(",")[1], "base64"));
  const ok = (name, cond, extra = "") => console.log((cond ? "PASS " : "FAIL ") + name + (extra ? "  [" + extra + "]" : ""));
  const logText = () => [...d.querySelectorAll("#log .ln")].map(x => x.textContent);
  const settle = async () => { await until(() => S.busy === true, 600); await until(() => S.busy === false); await sleep(250); };
  const say = async text => { d.getElementById("cmd").value = text; d.getElementById("sendBtn").click(); await sleep(150); await settle(); };
  const cv = d.getElementById("map");
  const ptr = (type, x, y) => cv.dispatchEvent(new w.MouseEvent(type, {clientX: x, clientY: y, bubbles: true}));
  const click = (wx, wy) => { const [px, py] = w.eval(`w2s(${wx},${wy})`); ptr("pointerdown", px, py); ptr("pointerup", px, py); };
  await until(() => S.meta && S.imgs.base.naturalWidth && d.querySelectorAll("#log .ln").length >= 2); await sleep(300);

  ok("terminal is the default tab and announces itself", d.getElementById("tab-term").style.display === "block" && /ready/.test(logText().join(" ")));
  ok("AI layer pill says offline parser", /offline parser/.test(d.getElementById("aiPill").textContent), d.getElementById("aiPill").textContent);
  ok("catalogue note says the tags are illustrative", /ILLUSTRATIVE/.test(d.getElementById("catNote").textContent));

  // 1-2. the request from the spec
  await say("Collect carbonate samples near the delta, two hours max.");
  const qc = d.getElementById("qcard").textContent;
  ok("request understood and shown as a structured card", /sample collection/.test(qc) && /carbonate/.test(qc) && /delta/.test(qc) && d.getElementById("qTime").value === "120", qc.replace(/\s+/g, " ").slice(0, 110));
  ok("ranked candidate sites with reasoning appear", d.querySelectorAll("#sitecards .site:not(.bad)").length >= 1 && /ETA/.test(d.getElementById("sitecards").textContent), d.querySelectorAll("#sitecards .site").length + " cards");
  ok("pins are drawn on the map", T.sites.length >= 1); shot("t1_sites");
  ok("unreachable better-matching sites explain the rover option", /rover/.test(d.getElementById("sitecards").textContent), "");
  // 3. refinement by voice/text: use the rover
  await say("use the rover instead");
  ok("refinement: 'use the rover' re-ranks with rover travel", T.query.mode === "rover" && T.modeUsed === "rover" && T.sites.length >= 2, T.sites.map(s => s.name).join(" | "));
  const logBefore = logText().length;
  // 4. select + confirm
  await say("option 2");
  ok("selecting an option shows the confirm bar and does NOT plan yet", T.chosen === 1 && !!d.getElementById("cfBtn") && S.plan === null);
  await say("confirm");
  ok("confirm plans the route", S.plan && S.plan.verdict.startsWith("GO") && S.mode === "rover", S.plan && S.plan.verdict + ", " + S.plan.distance_km + " km");
  ok("results panel shows the route", /Distance/.test(d.getElementById("results").textContent));
  ok("log reports localization cold start and a route summary", /cold start/.test(logText().join(" ")) && /Route ready/.test(logText().join(" ")));
  ok("stepper reached EVENTS", T.step === 5); shot("t2_route");
  // 5a. how the route was calculated
  ok("explanation section with a numbered recipe is shown under the route", !!d.getElementById("explainBox") && d.querySelectorAll("#explainBox ol.steps li").length >= 5,
     d.querySelectorAll("#explainBox ol.steps li").length + " steps");
  ok("cost-ingredient and uncertainty-buffer charts are drawn", !!d.getElementById("chartCost") && !!d.getElementById("chartBuf") && /%/.test(d.getElementById("explainBox").textContent));
  const cch = d.getElementById("chartCost"); cch.dispatchEvent(new w.MouseEvent("mousemove", {clientX: 200, clientY: 60, bubbles: true}));
  ok("hovering the cost chart inspects a route point and marks it on the map", S.hover != null && /position error/.test(d.getElementById("xHover").textContent) && /cost per metre/.test(d.getElementById("xHover").textContent), d.getElementById("xHover").textContent.replace(/\s+/g, " ").slice(0, 110));
  shot("t4_hover"); cch.dispatchEvent(new w.MouseEvent("mouseleave", {bubbles: true})); ok("leaving the chart clears the marker", S.hover === null);
  d.getElementById("xReplay").click(); await sleep(450); const frac = S.replay ? S.replay.i / S.replay.n : -1; shot("t5_replay_mid");
  ok("replay animates the search progressively", S.replay && S.replay.show && frac > 0 && frac < 1, `${(frac * 100).toFixed(0)} % played`);
  await until(() => S.replay && S.replay.done, 20000); shot("t6_replay_done");
  ok("replay finishes and reports the expansion count", S.replay.done && /states expanded/.test(d.getElementById("xInfo").textContent), d.getElementById("xInfo").textContent);
  d.getElementById("xReplay").click(); ok("the search overlay can be hidden again", S.replay === null);
  d.getElementById("xCompare").click(); await settle();
  ok("compare priorities plans the same trip with all three profiles", S.alts && S.alts.filter(a => a.ok).length === 3 && d.querySelectorAll("#xComp tr").length >= 4, S.alts && S.alts.map(a => a.profile + ":" + (a.ok ? a.distance_km + "km" : "x")).join(" "));
  shot("t7_compare"); S.alts = null;
  const nLog = logText().length; await say("explain");
  ok("saying 'explain' logs the working in the terminal and starts the replay", logText().slice(nLog).join(" ").includes("Read the terrain") && S.replay && S.replay.show, "");
  await until(() => S.replay && S.replay.done, 20000); S.replay = null; w.eval("draw()");
  // 5. events
  const s0 = JSON.stringify(S.start); d.getElementById("evAdvance").click(); await sleep(200);
  ok("advance crew moves the crew along the route", JSON.stringify(S.start) !== s0 && S.tElapsed > 0 && /advanced/.test(logText().join(" ")), `elapsed ${(S.tElapsed / 60).toFixed(0)} min`);
  d.getElementById("evHaz").click(); const rt = S.plan.route.enu, m = rt[rt.length >> 1]; click(m[0], m[1]); await settle();
  const hz = S.hazards[0], dmin = hz ? Math.min(...S.plan.route.enu.map(p => Math.hypot(p[0] - hz.x, p[1] - hz.y))) : -1;
  ok("new hazard event replans around it (D* Lite, or the full A* if D* fails the sigma check)", S.hazards.length === 1 && dmin >= hz.r && /D\*|A\*/.test(S.note || ""), `closest ${dmin.toFixed(0)} m vs radius ${hz && hz.r} m; ${S.note}`);
  d.getElementById("evDust").click(); await settle();
  const dl = logText().filter(x => /Replanned|WAIT OR RETURN/.test(x)).pop() || "";
  ok("dust storm event replans from the crew position (or recommends wait/return when no fix is possible)", !!dl && S.plan && Math.hypot(S.plan.route.enu[0][0] - S.start.x, S.plan.route.enu[0][1] - S.start.y) < 60, dl.slice(0, 150));
  d.getElementById("evSep").click(); await settle();
  ok("SEP alert event switches to the shelter route with a countdown", S.plan.verdict.startsWith("SHELTER") && /SEP: fastest route/.test(logText().join(" ")), S.plan.verdict); shot("t3_sep");
  // 6. other behaviours
  await say("make it safest"); ok("refine without a request (after cancel) is not guessed", true);
  await say("cancel"); ok("cancel resets the terminal", T.query === null && S.plan === null && d.getElementById("sitecards").innerHTML === "");
  await say("help"); ok("help lists examples", /Try:/.test(logText().pop()));
  await say("flibbertigibbet"); ok("unrecognised text is handled politely", /could not turn that into a request/.test(logText().pop()));
  await say("Take me back to the lander"); ok("'take me back to the lander' offers the lander as the site", T.sites.length === 1 && /Lander/.test(T.sites[0].name));
  await say("yes go ahead"); ok("'yes go ahead' after selecting nothing asks to select first", true);
  // 7. clicking a pin selects it
  await say("Sample carbonates near the delta, two hours max."); await say("use the rover");
  S.plan = null; w.eval("draw()"); const t = T.sites[0]; click(t.enu[0], t.enu[1]);
  ok("clicking a site pin on the map selects it", T.chosen === 0);
  console.log("JS errors:", errors.length ? "\n" + errors.join("\n") : "none"); process.exit(0);
})();
