import heapq, math, re, random

# Synthetic Jezero-inspired demonstration coordinates in a local ENU-like frame.
# Not real surveyed coordinates or validated terrain data.
SITES = [
    {"id":"site_delta","name":"Delta Front","x":620,"y":420,"features":["delta","clay"],"science_score":0.91,"placeholder":True},
    {"id":"site_carb","name":"Carbonate Margin","x":760,"y":-260,"features":["carbonate","olivine"],"science_score":0.88,"placeholder":True},
    {"id":"site_seitah","name":"Séítah","x":-520,"y":360,"features":["olivine","bedrock"],"science_score":0.79,"placeholder":True},
    {"id":"site_maaz","name":"Máaz Unit","x":-250,"y":-610,"features":["bedrock","basalt"],"science_score":0.72,"placeholder":True},
    {"id":"site_rim","name":"Crater Rim Viewpoint","x":880,"y":720,"features":["rim","geology"],"science_score":0.68,"placeholder":True},
    {"id":"site_synth","name":"Synthetic Ridge","x":-850,"y":-250,"features":["ridge","geology"],"science_score":0.61,"placeholder":True},
]

def resolve_request(text, mode="walk", time_budget=120):
    t = text.lower()
    features = [f for f in ["carbonate","olivine","clay","delta","rim","bedrock"] if f in t]
    if "two hours" in t: time_budget = 120
    m = "rover" if "rover" in t else mode
    ranked = []
    for s in SITES:
        match = len(set(features) & set(s["features"]))
        dist = math.hypot(s["x"], s["y"])
        eta = dist / (1.0 if m == "walk" else 3.0) / 60
        score = s["science_score"] + 0.12*match - 0.00012*dist
        if eta <= time_budget:
            ranked.append({**s, "distance_m": round(dist), "eta_min": round(eta,1),
                           "rank_score": round(score,3),
                           "explanation": f"{s['name']} matches {match} requested feature(s); estimated one-way travel {round(eta)} min."})
    ranked.sort(key=lambda x: x["rank_score"], reverse=True)
    return {"intent":{"target_features":features,"time_budget_min":time_budget,"mode":m,"source":"rules"},
            "sites":ranked[:5], "notice":"All locations and scores are synthetic placeholders for a prototype."}

def terrain_cost(x, y, mode):
    # Deterministic synthetic terrain with ridge-like hazards.
    slope = abs(math.sin(x/230) * math.cos(y/310))*17
    rough = abs(math.sin((x+y)/170))*0.8
    crater_rim = math.hypot(x-430, y-100) 
    hazard = crater_rim < 115
    banned = slope > (22 if mode == "walk" else 17) or (mode == "rover" and rough > 0.72)
    return slope, rough, hazard, banned

def make_plan(site, mode="walk", quality="high"):
    # 40 m grid A* over a bounded synthetic area.
    step = 40
    start=(0,0); goal=(round(site["x"]/step), round(site["y"]/step))
    sigma = {"high":4.0,"low":110.0,"medium":22.0}.get(quality,4.0)
    base_buffer = 3 if mode=="walk" else 5
    buffer = base_buffer + 2*sigma
    def h(p): return math.hypot(goal[0]-p[0],goal[1]-p[1])
    q=[(h(start),0,start)]
    came={}; cost={start:0}; seen=set()
    while q:
        _,g,p=heapq.heappop(q)
        if p in seen: continue
        seen.add(p)
        if p==goal: break
        for dx,dy in [(1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)]:
            n=(p[0]+dx,p[1]+dy)
            x,y=n[0]*step,n[1]*step
            if abs(x)>1200 or abs(y)>1200: continue
            slope,rough,hazard,banned=terrain_cost(x,y,mode)
            if banned or (hazard and buffer>18): continue
            edge=math.hypot(dx,dy)*(1+0.018*slope+0.18*rough)
            if quality=="low":
                edge *= 1 + 0.22*(1-abs(math.sin(x/280)))
            ng=g+edge
            if ng<cost.get(n,1e18):
                cost[n]=ng; came[n]=p
                heapq.heappush(q,(ng+h(n),ng,n))
    if goal not in came and goal != start:
        # Fallback straight-ish route, clearly marked as constrained synthetic demo.
        path=[start,goal]
        status="fallback"
    else:
        path=[goal]
        while path[-1]!=start: path.append(came[path[-1]])
        path.reverse(); status="ok"
    waypoints=[[p[0]*step,p[1]*step,0] for p in path]
    distance=sum(math.hypot(b[0]-a[0],b[1]-a[1]) for a,b in zip(waypoints,waypoints[1:]))
    speed=1.0 if mode=="walk" else 3.0
    fix_stops=[]
    if quality=="low" and len(waypoints)>4:
        fix_stops=[{"at_waypoint":len(waypoints)//2,"method":"skyline","duration_s":60,
                    "reason":"Low simulated localization confidence"}]
    eta=distance/speed/60 + sum(x["duration_s"] for x in fix_stops)/60
    return {"plan_id":"demo-"+site["id"]+"-"+quality,"name":site["name"],"goal_site_name":site["name"],"goal_site_id":site["id"],
            "waypoints_enu_m":waypoints,"eta_min":round(eta,1),"distance_m":round(distance),
            "turn_back_point_enu_m":waypoints[max(0,len(waypoints)//2)][:2],
            "fix_stops":fix_stops,"predicted_sigma_m":[round(sigma,1),round(sigma+distance*0.015,1)],
            "risk_score":round(min(.95,.12+sigma/500),2),"energy_kj":round(distance*(3.2 if mode=="walk" else 1.4)),
            "return_margin_ok":True,"alerts":[],"status":status,"reason":None,
            "uncertainty_buffer_m":round(buffer,1),"mode":mode,"localization_quality":quality,
            "disclaimer":"Synthetic demonstration output. Not suitable for real navigation or mission operations."}

def make_frame(s):
    plan=s.get("plan") or {}
    quality=s.get("quality","high")
    if s["sep"]:
        confidence="low"; ribbon="bearing"; alerts=[{"kind":"sep","text":"SEP ALERT: shelter route required in this simulation."}]
    elif s["tau"]>1.2:
        confidence="low"; ribbon="bearing"; alerts=[{"kind":"dust","text":"Dust opacity high; celestial fixes unavailable."}]
    else:
        confidence=quality; ribbon="full" if quality=="high" else ("halo" if quality=="medium" else "bearing")
        alerts=[]

    # Simulated progress along the server-supplied mission polyline.
    points=plan.get("waypoints_enu_m") or [[0,0,0]]
    speed=0.8 if s.get("mode")=="walk" else 3.0
    remaining=max(0.0, s["t"]*speed)
    segments=[]; total=0.0
    for a,b in zip(points,points[1:]):
        length=math.hypot(b[0]-a[0],b[1]-a[1]); segments.append((a,b,length,total)); total+=length
    pos=[float(points[0][0]),float(points[0][1]),0.0]
    heading=0.0; current_idx=0
    for i,(a,b,length,cumulative) in enumerate(segments):
        if remaining <= cumulative+length or i==len(segments)-1:
            frac=0 if length==0 else max(0,min(1,(remaining-cumulative)/length))
            pos=[a[0]+(b[0]-a[0])*frac,a[1]+(b[1]-a[1])*frac,0.0]
            heading=(math.degrees(math.atan2(b[0]-a[0],b[1]-a[1]))+360)%360
            current_idx=i+1 if frac<1 else min(i+2,len(points)-1)
            break
    next_wp=None
    if points and current_idx < len(points):
        target=points[current_idx]
        dx,dy=target[0]-pos[0],target[1]-pos[1]
        next_wp={"label":f"WAYPOINT {current_idx+1:02d}","distance_m":round(math.hypot(dx,dy),1),
                 "bearing_deg":round((math.degrees(math.atan2(dx,dy))+360)%360,1)}
    mission=None
    if plan:
        mission={"plan_id":plan.get("plan_id"),"name":plan.get("name"),
                 "goal_site_id":plan.get("goal_site_id"),"goal_site_name":plan.get("goal_site_name"),
                 "waypoints_enu_m":points,"eta_min":plan.get("eta_min",0),
                 "distance_m":plan.get("distance_m",round(total)),"status":plan.get("status","planned"),
                 "mode":plan.get("mode",s.get("mode","walk"))}
    return {"type":"hud_frame","t_s":s["t"],"pose":{"position_enu_m":[round(v,1) for v in pos],
            "heading_deg":round(heading,1),"altitude_m":0.0,"sigma_m":{"high":4.0,"medium":22.0,"low":110.0}.get(confidence,110.0)},
            "nav":{"speed_mps":speed if s.get("running") else 0.0,"heading_deg":round(heading,1),
                   "current_waypoint_index":current_idx,"next_waypoint":next_wp},
            "mission":mission,"confidence":confidence,"ribbon":{"style":ribbon,"waypoints_next":points[:8]},
            "eta_remaining_min":max(0,round(plan.get("eta_min",0)-s["t"]/60,1)),
            "consumables":{"o2_frac":max(0,1-s["t"]/9000),"power_frac":max(0,1-s["t"]/12000),"return_margin_frac":0.31},
            "env":{"tau":round(s["tau"],2),"dust_alert":s["tau"]>1.0,"dose_mSv":round(s["t"]*0.0004,3)},
            "alerts":alerts,"simulated":True}
