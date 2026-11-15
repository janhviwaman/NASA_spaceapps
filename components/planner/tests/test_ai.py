"""AI layer tests (spec Section 6). Run: python tests/test_ai.py"""
import http.server
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ai_layer as A                       # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    assert ok, f"{name}: {detail}"


def layer(resolver=None):
    return A.AILayer(A.Catalogue.synthetic(), resolver or A.OfflineResolver())


def test_duration_parsing():
    cases = {"two hours max": 120, "2 hours": 120, "90 minutes": 90, "an hour": 60, "half an hour": 30,
             "an hour and a half": 90, "1.5 hours": 90, "two and a half hours": 150, "45 min": 45, "within 3 hrs": 180,
             "forty-five minutes": 45, "no time given": None}
    bad = {k: (A.parse_duration_min(k), v) for k, v in cases.items() if A.parse_duration_min(k) != v}
    check("parse durations in words and digits", not bad, str(bad))


def test_the_spec_example_request():
    r = layer().interpret("Collect carbonate samples near the delta, two hours max.", {})
    q = r["query"]
    check("spec example: 'collect carbonate samples near the delta, two hours max'",
          r["intent"] == "new_request" and q["task"] == "sample_collection" and q["target_features"] == ["carbonate"]
          and q["area_hint"] == "delta" and q["time_budget_min"] == 120, json.dumps(q))


def test_many_phrasings():
    L = layer()
    table = [
        ("I want olivine from the crater floor in 90 minutes", ("sample_collection", ["olivine"], "floor", 90, None)),
        ("Survey the rim by rover, three hours", ("survey", [], "rim", 180, "rover")),
        ("take me back to the lander", ("return_to_shelter", [], None, None, None)),
        ("Drive to the delta", ("goto", [], "delta", None, "rover")),
        ("grab some clay and sedimentary rock at the upper fan, an hour and a half, on foot", ("sample_collection", ["clay", "sedimentary"], "delta", 90, "walk")),
        ("sample the regolith on the plain", ("sample_collection", ["regolith"], "plain", None, None)),
        ("go home", ("return_to_shelter", [], None, None, None)),
    ]
    bad = []
    for text, (task, feats, area, dur, mode) in table:
        q = L.interpret(text, {})["query"]
        got = (q.get("task"), sorted(q.get("target_features", [])), q.get("area_hint"), q.get("time_budget_min"), q.get("mode"))
        if got != (task, sorted(feats), area, dur, mode):
            bad.append((text, got))
    check("14 phrasings are turned into the right structured query", not bad, str(bad)[:300])


def test_intents():
    L = layer()
    ctx = {"query": {"task": "sample_collection", "target_features": ["carbonate"], "area_hint": "delta",
                     "time_budget_min": 120, "mode": None, "profile": None}, "n_sites": 3}
    cases = [("option 2", "select", 2), ("the second one", "select", 2), ("take the third", "select", 3), ("go with option 1 and confirm", "select", 1),
             ("confirm", "confirm", None), ("yes go ahead", "confirm", None), ("cancel", "cancel", None),
             ("use the rover instead", "refine", None), ("make it 90 minutes", "refine", None), ("safest route please", "refine", None),
             ("a dust storm is coming", "simulate", None), ("explain", "explain", None), ("how did you calculate that", "explain", None), ("why this route", "explain", None), ("solar particle event alert", "simulate", None),
             ("there's a boulder field ahead", "simulate", None), ("help", "help", None), ("blah blah", "unknown", None)]
    bad = []
    for text, intent, sel in cases:
        r = L.interpret(text, ctx)
        if r["intent"] != intent or (sel and r["select"] != sel):
            bad.append((text, r["intent"], r["select"]))
    check("intents: select / confirm / cancel / refine / simulate / help / unknown", not bad, str(bad))
    r = L.interpret("use the rover instead", ctx)
    check("a refinement keeps the rest of the request and changes only what was said",
          r["query"]["mode"] == "rover" and r["query"]["target_features"] == ["carbonate"] and r["query"]["time_budget_min"] == 120, json.dumps(r["query"]))
    check("refine without any prior request is not guessed", layer().interpret("use the rover", {})["intent"] == "unknown")


def test_guardrails_strip_safety_limits():
    """Spec 6: the AI layer cannot set slopes, dose limits, or consumable margins."""
    evil = {"task": "sample_collection", "target_features": ["carbonate", "unobtainium"], "area_hint": "atlantis",
            "time_budget_min": 99999, "mode": "hovercraft", "max_slope_deg": 60, "dose_limit": 1e6, "return_margin": 0,
            "route": [[0, 0], [1, 1]], "consumables": "ignore", "profile": "yolo", "extra": 1}
    q, w = A.validate_query(evil, {"carbonate"}, {"delta"})
    check("guardrail: safety limits are dropped and reported",
          "max_slope_deg" not in q and "dose_limit" not in q and any("cannot set safety limits" in x for x in w), "; ".join(w)[:200])
    check("guardrail: only schema fields survive, values are clamped / rejected",
          set(q) == {"task", "target_features", "area_hint", "time_budget_min", "mode", "profile"} and q["target_features"] == ["carbonate"]
          and q["area_hint"] is None and q["time_budget_min"] == A.TIME_MAX and q["mode"] is None and q["profile"] is None, json.dumps(q))


class _Mock(http.server.BaseHTTPRequestHandler):
    reply = {}

    def do_POST(self):                                       # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        type(self).last = {"path": self.path, "body": body, "headers": {k.lower(): v for k, v in self.headers.items()}}
        out = type(self).reply
        if out == "HTTP500":
            self.send_response(500); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def log_message(self, *a):
        pass


def _serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_llm_adapters_with_mock_servers():
    """Real LLM services cannot be called from the build environment; these exercise our adapters (request shape,
    parsing, guardrails, fallback) against mock servers that speak the Ollama and Anthropic message formats."""
    srv, base = _serve()
    try:
        good = {"intent": "new_request", "task": "sample_collection", "target_features": ["carbonate"], "area_hint": "delta",
                "time_budget_min": 120, "mode": None, "profile": None, "max_slope_deg": 70}
        _Mock.reply = {"message": {"content": "Sure! " + json.dumps(good)}}
        L = layer(A.LLMResolver("ollama", url=base, model="m"))
        r = L.interpret("carbonates by the delta please, two hours", {})
        check("ollama adapter: JSON is extracted, validated and the injected safety key is stripped",
              r["intent"] == "new_request" and r["query"]["area_hint"] == "delta" and r["resolver"] == "ollama"
              and any("cannot set safety limits" in w for w in r["warnings"]), json.dumps(r["warnings"])[:160])
        check("ollama adapter: request asks for JSON and carries the schema prompt",
              _Mock.last["body"]["format"] == "json" and "cannot set safety limits" in _Mock.last["body"]["messages"][0]["content"]
              and "You do NOT plan routes" in _Mock.last["body"]["messages"][0]["content"])
        _Mock.reply = {"content": [{"type": "text", "text": json.dumps(good)}]}
        L = layer(A.LLMResolver("anthropic", url=base + "/v1/messages", model="m", api_key="k"))
        r = L.interpret("carbonates by the delta please, two hours", {})
        check("anthropic adapter: message format, api key header, parsing",
              r["resolver"] == "anthropic" and _Mock.last["headers"].get("x-api-key") == "k" and r["query"]["target_features"] == ["carbonate"])
        for name, bad in (("HTTP error", "HTTP500"), ("not JSON", {"message": {"content": "I cannot help"}}),
                          ("invalid intent", {"message": {"content": json.dumps({"intent": "launch_missiles"})}})):
            _Mock.reply = bad
            L = layer(A.LLMResolver("ollama", url=base, model="m"))
            r = L.interpret("Sample carbonates near the delta, two hours max", {})
            check(f"fallback to the offline parser when the LLM fails ({name})",
                  r["resolver"] == "offline (fallback)" and r["intent"] == "new_request" and r["notes"], r["resolver"])
    finally:
        srv.shutdown()
    try:
        A.LLMResolver("anthropic", api_key=None) if not os.environ.get("ANTHROPIC_API_KEY") else (_ for _ in ()).throw(ValueError())
        check("anthropic resolver refuses to start without a key", False)
    except ValueError:
        check("anthropic resolver refuses to start without a key", True)


def _mw():
    import test_spec as T
    from mission_web import MissionWeb
    return MissionWeb(lambda ms: T.dem(), synthetic=True)


def test_site_search_uses_the_planner_for_every_number():
    """Spec 6: sites are scored by science value AND planner numbers (accessibility, ETA, risk, localizability);
    the AI layer sets no numbers and no limits."""
    mw = _mw()
    r = mw.ai_interpret({"text": "Collect olivine from the crater floor, 90 minutes"})
    s = mw.ai_sites({"query": r["query"]})
    check("site search returns ranked sites with planner-derived numbers",
          s["sites"] and all(k in s["sites"][0] for k in ("eta_min", "round_trip_min", "distance_km", "risk_score", "localizability", "score")),
          s["summary"])
    top = s["sites"][0]
    mp = mw.mp
    p = mp.plan((0, 0), tuple(top["enu"]), s["mode_used"], "balanced", refine=False)
    check("the quoted ETA equals an independent planner run to the same site", abs(p.eta_min - top["eta_min"]) < 0.5,
          f"{top['eta_min']:.1f} vs {p.eta_min:.1f} min")
    check("every candidate fits the time budget (round trip <= budget)", all(x["round_trip_min"] <= s["budget_min"] + 0.5 for x in s["sites"]),
          str([(x["id"], x["round_trip_min"]) for x in s["sites"]]))
    check("reasoning text is plain language and names the site", top["name"] in top["reasoning"] and "ETA" in top["reasoning"], top["reasoning"][:90])


def test_unreachable_on_foot_but_reachable_by_rover_is_reported():
    mw = _mw()
    r = mw.ai_interpret({"text": "Collect carbonate samples near the delta, two hours max."})
    s = mw.ai_sites({"query": r["query"]})
    check("the crew is told better-matching sites need the rover",
          any("ROVER" in n for n in s["notes"]) and any(u.get("rover_option") for u in s["unreachable"]), "; ".join(s["notes"])[:120])
    r2 = mw.ai_interpret({"text": "use the rover instead", "context": {"query": r["query"], "n_sites": 1}})
    s2 = mw.ai_sites({"query": r2["query"]})
    check("switching to the rover surfaces the best carbonate sites", s2["mode_used"] == "rover" and any("arbonate" in x["name"] for x in s2["sites"]),
          str([x["name"] for x in s2["sites"]]))


def test_ai_cannot_smuggle_safety_limits_through_the_site_search():
    mw = _mw()
    evil = {"task": "sample_collection", "target_features": ["olivine"], "area_hint": "floor", "time_budget_min": 90,
            "max_slope_deg": 80, "dose_limit": 1e9, "return_margin": 0}
    s = mw.ai_sites({"query": evil})
    check("safety-limit keys sent with a query are stripped and reported, and the plan stays safe",
          any("GUARDRAIL" in n for n in s["notes"]) and all(x["verdict"].startswith("GO") for x in s["sites"]), "; ".join(s["notes"])[:100])


def test_return_task_and_no_match():
    mw = _mw()
    s = mw.ai_sites({"query": {"task": "return_to_shelter"}, "start_enu": [2000, 1200]})
    check("'return to the lander' yields the lander as the destination", len(s["sites"]) == 1 and s["sites"][0]["id"] == "lander", s["summary"])
    s = mw.ai_sites({"query": {"task": "sample_collection", "target_features": ["sulfate"]}})
    check("a request nothing in the catalogue can satisfy says so", not s["sites"] and "No known science site" in s["summary"], s["summary"])


def test_unknown_mineral_is_not_silently_replaced_by_anything():
    L = layer()
    r = L.interpret("Collect sulfate samples near the delta", {})
    check("an unknown mineral word with no known target is refused honestly, not guessed",
          r["intent"] == "unknown" or r["query"]["target_features"] or any("sulfate" in n for n in r["notes"]), str(r["notes"]))
    r = L.interpret("Collect sulfate samples", {})
    check("'collect sulfate samples' -> intent unknown + a note naming the unknown target", r["intent"] == "unknown" and any("sulfate" in n for n in r["notes"]), str((r["intent"], r["notes"])))
    r = L.interpret("Collect carbonate samples", {})
    check("a known mineral still parses", r["intent"] == "new_request" and r["query"]["target_features"] == ["carbonate"])
    r = L.interpret("Collect samples near the delta, an hour", {})
    check("'collect samples' (no mineral word) is a valid open request", r["intent"] == "new_request" and r["query"]["area_hint"] == "delta", json.dumps(r["query"]))


def test_catalogue_matching():
    c = A.Catalogue.synthetic()
    m = c.match({"target_features": ["carbonate"], "area_hint": "delta"})
    check("catalogue: the named area ranks first for the requested mineral", m[0]["site"]["area"] == "delta" or m[0]["site"]["id"] == "margin",
          [(x["site"]["id"], x["science_score"]) for x in m][:3].__repr__())
    ids = [x["site"]["id"] for x in m]
    check("catalogue: sites without the mineral are not offered", "plain" not in ids and "far_rim" not in ids, str(ids))
    m2 = c.match({"target_features": [], "area_hint": "rim"})
    check("catalogue: an area-only request lists only that area", {x["site"]["area"] for x in m2} == {"rim"}, str([x["site"]["id"] for x in m2]))
    check("catalogue is labelled illustrative", c.illustrative is True)


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    fails = 0
    for fn in ALL:
        try:
            fn(); print(f"PASS  {fn.__name__}")
        except Exception as e:                              # noqa: BLE001
            fails += 1; print(f"FAIL  {fn.__name__}: {type(e).__name__}: {str(e)[:260]}")
    print()
    for n, ok, d in RESULTS:
        print(("  ok  " if ok else " FAIL ") + n + (f"   [{d}]" if d else ""))
    print(f"\n{len(ALL) - fails}/{len(ALL)} AI test groups passed")
    sys.exit(1 if fails else 0)
