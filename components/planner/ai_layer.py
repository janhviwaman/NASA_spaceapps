"""
ai_layer.py - the AI layer / target resolver (spec Section 6).

Role (spec): convert intent to a STRUCTURED QUERY. It does not plan and it does not touch safety limits.

    crew speech/text --> resolver --> validate_query() --> candidate sites (science catalogue)
                                                      --> coarse planner runs (MissionPlanner.rank_sites)
                                                      --> ranked sites with plain-language reasoning

Guardrails implemented here (spec 6):
  * structured output with schema validation: validate_query() accepts only the fields of the schema,
    clamps every value to its allowed range, and REPORTS anything else it dropped;
  * the language model cannot set slopes, dose limits, return margins or consumables: such keys are
    stripped and surfaced to the crew as a guardrail warning;
  * every numeric claim shown to the crew comes from the planner, never from the resolver;
  * the crew can always ignore a suggestion (nothing is planned until they select and confirm);
  * runs on the lander computer, offline: the default OfflineResolver is a deterministic parser with no
    model and no network. A local LLM (Ollama) or a cloud LLM (Anthropic API) can be plugged in behind
    the same schema; both fall back to the offline parser if they fail or return invalid output.
    NOTE: only the local model is consistent with the spec's "offline, on-device" requirement.

Science catalogue: see Catalogue. The tags are ILLUSTRATIVE (hand-assigned from published unit
descriptions), not derived from the CRISM / geologic-map layers the spec calls for.
"""
import json
import math
import os
import re
import urllib.request

TASKS = ("sample_collection", "survey", "goto", "return_to_shelter")
MODES = ("walk", "rover")
PROFILES = ("min_eta", "balanced", "safest")
TIME_MIN, TIME_MAX = 10, 600
DEFAULT_BUDGET_MIN = 180

# keys an AI layer must NEVER be able to set (spec 6 guardrails)
SAFETY_KEYS = {"max_slope", "max_slope_deg", "slope", "slope_limit", "dose", "dose_limit", "dose_budget", "return_margin",
               "reserve", "reserve_frac", "consumables", "o2", "oxygen", "power", "cooling", "sigma", "n_sigma",
               "buffer", "margin", "hazard", "hazards", "path", "route", "waypoints", "eta", "energy", "risk",
               "safety", "limits", "constraints"}

FEATURE_SYNONYMS = {
    "carbonate": ["carbonate", "carbonates", "carbonate-bearing", "magnesite", "fe-mg carbonate"],
    "olivine": ["olivine", "olivine-rich", "seitah", "séítah", "cumulate"],
    "clay": ["clay", "clays", "phyllosilicate", "phyllosilicates", "smectite", "hydrated silicate", "hydrated silicates"],
    "sedimentary": ["sediment", "sediments", "sedimentary", "mudstone", "sandstone", "layered rock", "layered rocks",
                    "delta rock", "delta rocks", "delta deposit", "delta deposits"],
    "igneous": ["igneous", "basalt", "basaltic", "lava", "volcanic", "maaz", "máaz"],
    "breccia": ["breccia", "megabreccia", "ancient crust", "old crust", "crust"],
    "regolith": ["regolith", "soil", "sand sample", "loose material", "dust sample"],
}
AREA_SYNONYMS = {
    "delta": ["delta", "fan", "upper fan", "western delta", "delta front", "delta top", "kodiak"],
    "floor": ["crater floor", "the floor", "floor", "seitah", "séítah", "maaz", "máaz"],
    "rim": ["crater rim", "rim", "margin", "margin unit", "inner margin"],
    "outside": ["outside the crater", "beyond the rim", "highlands", "outside"],
    "plain": ["plain", "smooth plain", "flat ground"],
}
TASK_WORDS = {
    "return_to_shelter": ["go home", "back to the lander", "return to the lander", "return to shelter", "take me home",
                          "take me back", "get me back", "back to shelter", "return to base", "retreat", "head back",
                          "go back to the lander", "to the shelter", "take me to the lander", "back to the ship"],
    "sample_collection": ["sample", "samples", "collect", "drill", "core", "gather", "pick up", "bring back", "cache"],
    "survey": ["survey", "image", "photograph", "inspect", "look at", "scout", "recon", "examine", "observe"],
    "goto": ["go to", "travel to", "walk to", "drive to", "head to", "navigate to", "take me to", "go near", "get to"],
}
NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
NUMBER_WORDS.update({"thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "seventy-five": 75, "ninety": 90,
                     "an": 1, "a": 1})
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}


# ------------------------------------------------------------------ small parsers
def _num(tok):
    tok = tok.lower().strip()
    try:
        return float(tok)
    except ValueError:
        return NUMBER_WORDS.get(tok)


def parse_duration_min(text):
    """'two hours max', '90 minutes', 'half an hour', 'an hour and a half', '1.5 hours' -> minutes (or None)."""
    t = text.lower()
    if re.search(r"\bhalf an? hour\b", t):
        return 30.0
    if re.search(r"\ban? hour and a half\b", t):
        return 90.0
    m = re.search(r"\b(\d+(?:\.\d+)?|[a-z]+(?:-[a-z]+)?)\s+and a half\s+hours?\b", t)
    if m and _num(m.group(1)) is not None:
        return (_num(m.group(1)) + 0.5) * 60.0
    m = re.search(r"\b(\d+(?:\.\d+)?|[a-z]+(?:-[a-z]+)?)\s*(?:hours?|hrs?|h)\b", t)
    if m and _num(m.group(1)) is not None:
        return _num(m.group(1)) * 60.0
    m = re.search(r"\b(\d+(?:\.\d+)?|[a-z]+(?:-[a-z]+)?)\s*(?:minutes?|mins?)\b", t)
    if m and _num(m.group(1)) is not None:
        return _num(m.group(1))
    return None


def _find_any(text, vocab):
    """Longest-synonym-first match; returns the set of canonical keys found."""
    t = " " + re.sub(r"[^\w\s\-éíáó]", " ", text.lower()) + " "
    found = []
    for key, words in vocab.items():
        for w in sorted(words, key=len, reverse=True):
            if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", t):
                found.append(key)
                break
    return found


# ------------------------------------------------------------------ schema validation (the guardrail)
def validate_query(raw, features=None, areas=None):
    """Return (clean_query, warnings). Only schema fields survive, every value is range-checked, and
    anything the AI tried to set outside the schema (especially safety limits) is dropped and reported."""
    warnings = []
    raw = dict(raw or {})
    clean = {"task": None, "target_features": [], "area_hint": None, "time_budget_min": None, "mode": None, "profile": None}
    dropped_safety = sorted(k for k in raw if str(k).lower() in SAFETY_KEYS)
    if dropped_safety:
        warnings.append("GUARDRAIL: the AI layer cannot set safety limits; ignored " + ", ".join(dropped_safety)
                        + ". Slopes, dose, return margin and consumables are fixed by the planner.")
    unknown = sorted(k for k in raw if k not in clean and str(k).lower() not in SAFETY_KEYS
                     and k not in ("intent", "select", "simulate", "confirm"))
    if unknown:
        warnings.append("ignored unknown field(s): " + ", ".join(map(str, unknown)))
    task = raw.get("task")
    if task in TASKS:
        clean["task"] = task
    elif task is not None:
        warnings.append(f"ignored unknown task '{task}'")
    feats = raw.get("target_features") or []
    if isinstance(feats, str):
        feats = [feats]
    ok_feats = []
    for f in feats:
        f = str(f).lower()
        if features is None or f in features:
            if f not in ok_feats:
                ok_feats.append(f)
        else:
            warnings.append(f"ignored unknown target '{f}'")
    clean["target_features"] = ok_feats
    ah = raw.get("area_hint")
    if ah:
        ah = str(ah).lower()
        if areas is None or ah in areas:
            clean["area_hint"] = ah
        else:
            warnings.append(f"ignored unknown area '{ah}'")
    tb = raw.get("time_budget_min")
    if tb is not None:
        try:
            tb = float(tb)
            if math.isfinite(tb):
                clipped = min(max(tb, TIME_MIN), TIME_MAX)
                if clipped != tb:
                    warnings.append(f"time budget {tb:g} min clamped to {clipped:g} min")
                clean["time_budget_min"] = int(round(clipped))
        except (TypeError, ValueError):
            warnings.append("ignored a time budget that is not a number")
    if raw.get("mode") in MODES:
        clean["mode"] = raw["mode"]
    elif raw.get("mode") is not None:
        warnings.append(f"ignored unknown mode '{raw['mode']}'")
    if raw.get("profile") in PROFILES:
        clean["profile"] = raw["profile"]
    elif raw.get("profile") is not None:
        warnings.append(f"ignored unknown profile '{raw['profile']}'")
    return clean, warnings


# ------------------------------------------------------------------ science catalogue
class Catalogue:
    """Candidate science sites with feature scores in [0, 1].

    ILLUSTRATIVE: the tags are hand-assigned from published descriptions of Jezero's units (Séítah olivine
    cumulate, Máaz lava plain, delta / Upper Fan sediments with carbonate and phyllosilicates, carbonate-bearing
    Margin Unit). They are NOT read from the CRISM mineral maps or the geologic map (Stack et al. 2020) the spec
    calls for; load those layers and replace this class's `sites` to make the science value real."""

    def __init__(self, sites, source, illustrative=True):
        self.sites, self.source, self.illustrative = sites, source, illustrative
        self.features = sorted({f for s in sites for f in s["features"]} | set(FEATURE_SYNONYMS))
        self.areas = sorted({s["area"] for s in sites})

    @classmethod
    def real(cls, frame):
        from plan import LANDMARKS
        spec = {
            "landing": ("Landing site / Máaz lava plain", "floor", "Máaz formation: igneous lava plain on the crater floor",
                        {"igneous": 0.9, "regolith": 0.5, "olivine": 0.2}),
            "crater_floor": ("Séítah (crater floor)", "floor", "Séítah formation: olivine cumulate, carbonate along grain boundaries",
                             {"olivine": 1.0, "igneous": 0.8, "carbonate": 0.35}),
            "delta_front": ("Delta front", "delta", "Base of the western delta: sedimentary rock, carbonate detected at the base",
                            {"sedimentary": 0.9, "carbonate": 0.55, "clay": 0.5}),
            "delta_top": ("Delta top", "delta", "Delta deposits: coarse sandstone and conglomerate, carbonate-bearing",
                          {"sedimentary": 0.9, "carbonate": 0.6, "clay": 0.4}),
            "upper_fan": ("Upper Fan", "delta", "Upper Fan sediments: carbonate and phyllosilicates (carbonate up to ~6 wt%)",
                          {"sedimentary": 0.9, "carbonate": 0.75, "clay": 0.7}),
            "rim_base": ("Rim base (Margin Unit)", "rim", "Margin Unit along the inner crater margin: carbonate and olivine (carbonate up to ~16 wt%)",
                         {"carbonate": 1.0, "olivine": 0.7, "igneous": 0.5}),
            "rim_top": ("Crater rim", "rim", "Rim bedrock: ancient crust and breccia",
                        {"breccia": 0.9, "igneous": 0.5}),
            "beyond_rim": ("Beyond the rim", "outside", "Terrain outside the crater: ancient crust",
                           {"breccia": 0.8, "regolith": 0.4}),
        }
        sites = []
        for k, (name, area, unit, feats) in spec.items():
            if k not in LANDMARKS:
                continue
            lat, lon, _ = LANDMARKS[k]
            e, n = frame.lonlat_to_enu(lon, lat)
            sites.append({"id": k, "name": name, "area": area, "unit": unit, "features": feats,
                          "enu": [round(float(e), 1), round(float(n), 1)]})
        return cls(sites, "Perseverance positions (NASA) with hand-assigned unit tags", True)

    @classmethod
    def synthetic(cls):
        sites = [
            {"id": "margin", "name": "Carbonate-bearing margin", "area": "rim", "unit": "synthetic margin unit",
             "features": {"carbonate": 0.95, "olivine": 0.5, "igneous": 0.4}, "enu": [2500, 1500]},
            {"id": "delta_front", "name": "Delta front (beyond the scarp)", "area": "delta", "unit": "synthetic delta deposits",
             "features": {"sedimentary": 0.9, "carbonate": 0.6, "clay": 0.55}, "enu": [-3200, 2000]},
            {"id": "floor_olivine", "name": "Olivine-rich floor unit", "area": "floor", "unit": "synthetic floor cumulate",
             "features": {"olivine": 1.0, "igneous": 0.8, "carbonate": 0.3}, "enu": [800, -1800]},
            {"id": "upper_fan", "name": "Upper fan clays", "area": "delta", "unit": "synthetic upper fan",
             "features": {"clay": 0.9, "sedimentary": 0.8, "carbonate": 0.4}, "enu": [-1500, 3200]},
            {"id": "plain", "name": "Smooth regolith plain", "area": "plain", "unit": "synthetic sand plain",
             "features": {"regolith": 0.9}, "enu": [0, -4500]},
            {"id": "far_rim", "name": "Far rim breccia", "area": "rim", "unit": "synthetic rim bedrock",
             "features": {"breccia": 0.9, "igneous": 0.5}, "enu": [4200, 3800]},
        ]
        return cls(sites, "synthetic test terrain (invented)", True)

    def match(self, query, limit=6):
        """Candidate sites for a validated query, with a science score and a one-line reason."""
        feats = query.get("target_features") or []
        area = query.get("area_hint")
        out = []
        for s in self.sites:
            if feats:
                sci = sum(s["features"].get(f, 0.0) for f in feats) / len(feats)
                hit = [f for f in feats if s["features"].get(f, 0.0) > 0]
            else:
                sci, hit = 0.5, []
            if feats and sci <= 0:
                continue                                   # none of the requested features: not a candidate
            if not feats and area and s["area"] != area:
                continue
            if area and s["area"] != area:
                sci *= 0.55                                # still offered, but ranked below the named area
            why = s["unit"]
            if hit:
                why += "; matches " + ", ".join(f"{f} ({s['features'][f]:.2f})" for f in hit)
            out.append({"site": s, "science_score": round(min(sci, 1.0), 3), "why": why})
        out.sort(key=lambda r: -r["science_score"])
        return out[:limit]


# ------------------------------------------------------------------ offline resolver (no model, no network)
class OfflineResolver:
    name = "offline"

    def parse(self, text, ctx, catalogue):
        t = (text or "").strip()
        low = t.lower()
        ctx = ctx or {}
        out = {"intent": "unknown", "query": {}, "select": None, "simulate": None, "confirm": False, "unknown_targets": []}
        if not t:
            return out
        if re.search(r"\b(cancel|abort|never ?mind|forget it|start over|reset|clear)\b", low):
            out["intent"] = "cancel"
            return out
        sel = self._select(low)
        conf = bool(re.search(r"\b(confirm|yes|go ahead|proceed|do it|affirmative|approve|execute|engage|lets go|let's go)\b", low))
        if sel is not None and ctx.get("n_sites"):
            out.update(intent="select", select=sel, confirm=conf)
            return out
        if conf and not re.search(r"\b(sample|collect|survey|go to|take me)\b", low):
            out.update(intent="confirm")
            return out
        if re.search(r"\b(explain|how did you (calculate|work|plan|get|decide|choose|compute)|how was (that|this|it) (calculated|planned|computed|worked)|"
                     r"why (this|that) route|show (me )?(your )?(working|work|reasoning|math|calculation)|how does (it|this) work|walk me through)\b", low):
            out["intent"] = "explain"
            return out
        if re.search(r"\b(dust ?storm|sand ?storm|dust)\b", low):
            out.update(intent="simulate", simulate="dust")
            return out
        if re.search(r"\b(solar particle|sep|radiation|solar flare|flare)\b", low):
            out.update(intent="simulate", simulate="sep")
            return out
        if re.search(r"\b(hazard|boulder|rockfall|obstacle|crevasse|blocked)\b", low):
            out.update(intent="simulate", simulate="hazard")
            return out
        task = None
        for k in ("return_to_shelter", "sample_collection", "survey", "goto"):
            if any(re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", low) for w in TASK_WORDS[k]):
                task = k
                break
        feats = _find_any(low, FEATURE_SYNONYMS)
        # an area word that is really part of a feature phrase ("delta rocks") still names the area
        area = None
        found_areas = _find_any(low, AREA_SYNONYMS)
        if found_areas:
            area = found_areas[0]
        # a mineral / rock word we have no site list for ("collect sulfate samples") must not silently become "anything"
        known = {w for ws in list(FEATURE_SYNONYMS.values()) + list(AREA_SYNONYMS.values()) for x in ws for w in x.split()}
        stop = {"some", "these", "those", "more", "several", "core", "drill", "collect", "gather", "grab", "sample", "take",
                "fresh", "good", "any", "the", "two", "three", "four", "five", "soil", "loose", "dust", "rock", "bring", "pick"}
        unknown = [m.group(1) for m in re.finditer(r"\b([a-zé\-]{4,})\s+(?:samples?|rocks?|minerals?|material|deposits?)\b", low)
                   if m.group(1) not in known and m.group(1) not in stop]
        out["unknown_targets"] = unknown
        dur = parse_duration_min(low)
        mode = "rover" if re.search(r"\b(rover|vehicle|drive|driving|by car|buggy)\b", low) else \
            "walk" if re.search(r"\b(walk|on foot|foot|hike|eva|walking)\b", low) else None
        profile = "safest" if re.search(r"\b(safest|safe|careful|cautious)\b", low) else \
            "min_eta" if re.search(r"\b(fastest|quickest|asap|as fast|shortest time|minimum time)\b", low) else \
            "balanced" if re.search(r"\bbalanced\b", low) else None
        q = {"task": task, "target_features": feats, "area_hint": area, "time_budget_min": dur, "mode": mode, "profile": profile}
        if task == "return_to_shelter":
            q.update(target_features=[], area_hint=None)
        has_content = bool(task or feats or area)
        if has_content:
            if q["task"] is None:
                q["task"] = "sample_collection" if feats else "goto"
            out.update(intent="new_request", query=q)
        elif (dur is not None or mode or profile) and ctx.get("query"):
            out.update(intent="refine", query={k: v for k, v in q.items() if v not in (None, [], "")})
        elif re.search(r"\b(help|what can you do|how do i|commands?|examples?)\b", low) or low.strip() == "?":
            out["intent"] = "help"
        return out

    @staticmethod
    def _select(low):
        m = re.search(r"(?:option|number|no\.?|site|choice|candidate|target|#)\s*(\d)\b", low)
        if m:
            return int(m.group(1))
        m = re.search(r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\b(?:\s+(?:one|option|site|choice))?", low)
        if m and re.search(r"\b(take|pick|choose|select|go with|use|want|one|option|site|choice|that)\b", low):
            return ORDINALS[m.group(1)]
        m = re.fullmatch(r"\s*(?:number\s+)?(\d)\s*", low)
        if m:
            return int(m.group(1))
        return None


# ------------------------------------------------------------------ optional LLM resolvers (behind the same guardrails)
SYSTEM_PROMPT = """You are the target resolver of a Mars lander terminal. Convert the crew's request into ONE JSON object and nothing else.
Allowed keys: intent (new_request|refine|select|confirm|cancel|simulate|explain|help|unknown), task (sample_collection|survey|goto|return_to_shelter),
target_features (subset of: {features}), area_hint (one of: {areas} or null), time_budget_min (number of minutes or null),
mode (walk|rover|null), profile (min_eta|balanced|safest|null), select (1-based option number or null), simulate (dust|sep|hazard|null).
You do NOT plan routes and you cannot set safety limits (slopes, dose, margins, consumables). Output JSON only."""


class LLMResolver:
    """Local (Ollama) or cloud (Anthropic API) resolver. It only EXTRACTS the structured request; the reply text,
    the site ranking and every number shown to the crew are produced by deterministic code and the planner."""

    def __init__(self, kind, url=None, model=None, api_key=None, timeout=20):
        self.kind = kind
        self.name = kind
        self.timeout = timeout
        if kind == "ollama":
            self.url = (url or os.environ.get("JEZERO_LLM_URL") or "http://localhost:11434").rstrip("/") + "/api/chat"
            self.model = model or os.environ.get("JEZERO_LLM_MODEL") or "llama3.2"
        elif kind == "anthropic":
            self.url = url or os.environ.get("JEZERO_LLM_URL") or "https://api.anthropic.com/v1/messages"
            self.model = model or os.environ.get("JEZERO_LLM_MODEL") or "claude-haiku-5-5"
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not self.api_key:
                raise ValueError("ANTHROPIC_API_KEY is not set")
        else:
            raise ValueError(f"unknown LLM kind '{kind}'")

    def parse(self, text, ctx, catalogue):
        sysmsg = SYSTEM_PROMPT.format(features=", ".join(catalogue.features), areas=", ".join(catalogue.areas))
        ctxmsg = json.dumps({"current_query": (ctx or {}).get("query"), "options_shown": (ctx or {}).get("n_sites", 0)})
        user = f"Context: {ctxmsg}\nCrew said: {text}"
        if self.kind == "ollama":
            body = {"model": self.model, "stream": False, "format": "json",
                    "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}]}
            req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                content = json.load(r)["message"]["content"]
        else:
            body = {"model": self.model, "max_tokens": 400, "system": sysmsg, "messages": [{"role": "user", "content": user}]}
            req = urllib.request.Request(self.url, json.dumps(body).encode(), {
                "Content-Type": "application/json", "x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                content = json.load(r)["content"][0]["text"]
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ValueError("no JSON object in the model reply")
        raw = json.loads(m.group(0))
        intent = raw.get("intent")
        if intent not in ("new_request", "refine", "select", "confirm", "cancel", "simulate", "explain", "help", "unknown"):
            raise ValueError(f"invalid intent '{intent}'")
        q = {k: raw[k] for k in raw if k not in ("intent", "select", "simulate")}
        sel = raw.get("select")
        return {"intent": intent, "query": q, "select": int(sel) if isinstance(sel, (int, float)) else None,
                "simulate": raw.get("simulate") if raw.get("simulate") in ("dust", "sep", "hazard") else None,
                "confirm": bool(raw.get("confirm"))}


def resolver_from_env():
    kind = (os.environ.get("JEZERO_LLM") or "").strip().lower()
    if kind in ("ollama", "anthropic"):
        try:
            return LLMResolver(kind)
        except ValueError as e:
            print(f"[ai] LLM resolver '{kind}' not available ({e}); using the offline parser")
    return OfflineResolver()


# ------------------------------------------------------------------ the AI layer
class AILayer:
    def __init__(self, catalogue, resolver=None):
        self.cat = catalogue
        self.resolver = resolver or resolver_from_env()
        self.offline = OfflineResolver()

    def status(self):
        return {"resolver": self.resolver.name, "offline": self.resolver.name == "offline",
                "catalogue": {"source": self.cat.source, "illustrative": self.cat.illustrative,
                              "n_sites": len(self.cat.sites), "areas": self.cat.areas, "features": self.cat.features}}

    def interpret(self, text, ctx=None):
        ctx = ctx or {}
        notes, used = [], self.resolver.name
        try:
            raw = self.resolver.parse(text, ctx, self.cat)
        except Exception as e:                         # noqa: BLE001 - any LLM failure falls back to the offline parser
            raw = self.offline.parse(text, ctx, self.cat)
            used = "offline (fallback)"
            notes.append(f"The {self.resolver.name} resolver failed ({type(e).__name__}); used the offline parser instead.")
        clean, warns = validate_query(raw.get("query"), set(self.cat.features), set(self.cat.areas))
        intent = raw.get("intent", "unknown")
        prior = ctx.get("query")
        query = clean
        if intent == "refine" and prior:
            query = dict(prior)
            for k, v in clean.items():
                if v not in (None, [], ""):
                    query[k] = v
        elif intent == "refine":
            intent = "unknown"
        if intent == "new_request" and clean["task"] is None:
            intent = "unknown"
        unk = raw.get("unknown_targets") or []
        if intent == "new_request" and unk and not clean["target_features"] and not clean["area_hint"] and clean["task"] != "return_to_shelter":
            intent = "unknown"
            notes.append(f"No science site in the catalogue has '{', '.join(unk)}'. Known targets: {', '.join(self.cat.features)}.")
        elif unk:
            notes.append(f"Ignored '{', '.join(unk)}': not in the science catalogue.")
        res = {"intent": intent, "query": query if intent in ("new_request", "refine") else prior,
               "select": raw.get("select"), "simulate": raw.get("simulate"), "confirm": bool(raw.get("confirm")),
               "resolver": used, "warnings": warns, "notes": notes}
        res["reply"] = self._reply(res, ctx)
        return res

    # -------- deterministic reply text (never produced by a language model)
    def describe(self, q):
        task = {"sample_collection": "collect samples", "survey": "survey", "goto": "travel",
                "return_to_shelter": "return to the lander"}.get(q.get("task"), "do something")
        bits = [task]
        if q.get("target_features"):
            bits.append("of " + " and ".join(q["target_features"]))
        if q.get("area_hint"):
            bits.append("near the " + q["area_hint"])
        s = " ".join(bits)
        s += f"; time budget {q['time_budget_min']} min" if q.get("time_budget_min") else f"; no time limit given (assuming {DEFAULT_BUDGET_MIN} min)"
        s += {"walk": "; on foot", "rover": "; by rover"}.get(q.get("mode"), "; travel mode not given (on foot, rover if nothing is reachable)")
        if q.get("profile"):
            s += f"; optimise for {q['profile'].replace('_', ' ')}"
        return s

    def _reply(self, r, ctx):
        i = r["intent"]
        if i == "new_request":
            return "Understood: " + self.describe(r["query"]) + ". Searching candidate sites."
        if i == "refine":
            return "Updated: " + self.describe(r["query"]) + ". Re-ranking the sites."
        if i == "select":
            return f"Option {r['select']} selected." + (" Confirmed by the crew." if r["confirm"] else " Say 'confirm' to plan the route.")
        if i == "confirm":
            return "Confirmed. Planning the route."
        if i == "cancel":
            return "Cancelled. Nothing was planned."
        if i == "simulate":
            return {"dust": "Simulating a dust storm.", "sep": "Simulating a solar particle event.",
                    "hazard": "Click the map where the hazard is."}[r["simulate"]]
        if i == "explain":
            return ("Showing how the route was calculated: the cost of each step, the position error against the safety buffer, "
                    "and a replay of the search." if ctx.get("has_route") else "There is no route to explain yet. Ask for one first.")
        if i == "help":
            return ("I turn what you want into a structured request, then the planner does everything that affects safety. Try: "
                    "'Sample carbonates near the delta, two hours max', 'Collect olivine from the crater floor, 90 minutes', "
                    "'Survey the rim by rover', 'Take me back to the lander'. Then say 'option 2' and 'confirm', and 'explain' to see how the route was calculated.")
        return ("I could not turn that into a request. Say what you want to do, e.g. 'Sample carbonates near the delta, two hours max'. "
                "Say 'help' for examples.")
