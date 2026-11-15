'use strict';

const $ = id => document.getElementById(id);
let code = (new URLSearchParams(location.search).get('code') || '').trim().toUpperCase();
let frame = null, mission = null, ws = null, routeBounds = null;
let pollTimer = null, missionRefreshTimer = 0, reconnectTimer = null, reconnectDelay = 2000;
let connecting = false, sessionValid = false;
const imu = { active:false, pitch:0, roll:0, heading:null, headingOffset:0, pitchZero:0, rollZero:0, mode:'both', lastPost:0, permission:'unknown' };
let imuPostTimer = null;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const fmt = (v, n = 0) => Number.isFinite(Number(v)) ? Number(v).toFixed(n) : '—';

function setConnection(label, state = '') {
  $('hudConn').textContent = label;
  $('hudConn').className = 'conn ' + state;
}
function setMessage(message, isError = false) {
  $('hudAlert').textContent = message;
  $('hudAlert').classList.toggle('message-error', isError);
}
async function fetchJSON(path) {
  const response = await fetch(path, { cache: 'no-store', headers: { Accept: 'application/json' } });
  let data = {};
  try { data = await response.json(); } catch (_) { /* non-JSON error response */ }
  if (!response.ok) throw new Error(data.detail || `Server returned ${response.status}`);
  return data;
}

function drawScene() {
  const canvas = $('scene');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const gradient = ctx.createLinearGradient(0, 0, 0, h);
  gradient.addColorStop(0, '#17242a'); gradient.addColorStop(.48, '#55443a'); gradient.addColorStop(1, '#9b7658');
  ctx.fillStyle = gradient; ctx.fillRect(0, 0, w, h);
  for (let layer = 0; layer < 5; layer++) {
    ctx.beginPath(); const base = h * (.48 + layer * .105); ctx.moveTo(0, h);
    for (let px = 0; px <= w; px += 18) {
      const y = base + Math.sin(px * .009 + layer * 1.8) * 34 + Math.sin(px * .025 + layer) * 17 + Math.cos(px * .004 + layer) * 28;
      ctx.lineTo(px, y);
    }
    ctx.lineTo(w, h); ctx.closePath(); ctx.fillStyle = ['#263238', '#344047', '#51443b', '#705544', '#987256'][layer];
    ctx.globalAlpha = .72; ctx.fill(); ctx.globalAlpha = 1;
  }
  if (mission?.waypoints_enu_m?.length) {
    const target = mission.waypoints_enu_m[mission.waypoints_enu_m.length - 1];
    ctx.save(); ctx.strokeStyle = '#75e6d5'; ctx.setLineDash([12, 10]); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(w / 2, h * .45); ctx.lineTo(w / 2 + Number(target[0] || 0) * .09, h * .45 - Number(target[1] || 0) * .09); ctx.stroke(); ctx.restore();
  }
  ctx.strokeStyle = 'rgba(120,230,220,.23)'; ctx.lineWidth = 1; ctx.beginPath();
  ctx.moveTo(w / 2, 0); ctx.lineTo(w / 2, h); ctx.moveTo(0, h * .47); ctx.lineTo(w, h * .47); ctx.stroke();
}
function setMission(nextMission) {
  const before = JSON.stringify(mission);
  mission = nextMission || null;
  if (JSON.stringify(mission) !== before) {
    if (mission) {
      $('missionName').textContent = mission.name || mission.goal_site_name || 'UPLOADED MISSION';
      $('missionTag').textContent = mission.plan_id || 'MISSION LOADED';
      $('hudDistance').textContent = `${fmt(mission.distance_m)} m`;
      $('hudEta').textContent = `${fmt(mission.eta_min, 1)} min`;
      $('routeStatus').textContent = (mission.status || 'PLANNED').toUpperCase();
    } else {
      $('missionName').textContent = 'AWAITING MISSION DATA';
      $('missionTag').textContent = 'NO MISSION';
      $('routeStatus').textContent = 'NO ROUTE';
    }
    drawScene(); drawRoute(); renderWaypoints();
  }
}
function projectRoute(point) {
  const bounds = routeBounds || { minX: -100, minY: -100, maxX: 100, maxY: 100 };
  const canvas = $('routeMap'), pad = 60;
  const sx = (canvas.width - pad * 2) / Math.max(1, bounds.maxX - bounds.minX);
  const sy = (canvas.height - pad * 2) / Math.max(1, bounds.maxY - bounds.minY);
  const scale = Math.min(sx, sy);
  return [pad + (point[0] - bounds.minX) * scale, pad + (bounds.maxY - point[1]) * scale];
}
function drawRoute() {
  const canvas = $('routeMap'), ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  ctx.fillStyle = '#0b151b'; ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = '#1a2b32'; ctx.lineWidth = 1;
  for (let x = 0; x <= w; x += 45) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
  for (let y = 0; y <= h; y += 45) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  const points = mission?.waypoints_enu_m || [];
  if (!points.length) {
    ctx.fillStyle = '#6e858b'; ctx.font = '20px monospace'; ctx.textAlign = 'center';
    ctx.fillText('WAITING FOR MISSION ROUTE', w / 2, h / 2); ctx.textAlign = 'left'; return;
  }
  const xs = points.map(p => Number(p[0])), ys = points.map(p => Number(p[1]));
  routeBounds = { minX: Math.min(0, ...xs) - 100, maxX: Math.max(0, ...xs) + 100, minY: Math.min(0, ...ys) - 100, maxY: Math.max(0, ...ys) + 100 };
  ctx.strokeStyle = 'rgba(117,230,213,.12)'; ctx.lineWidth = 18; ctx.beginPath();
  points.forEach((point, i) => { const [x, y] = projectRoute(point); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  ctx.strokeStyle = '#78dfd0'; ctx.lineWidth = 4; ctx.beginPath();
  points.forEach((point, i) => { const [x, y] = projectRoute(point); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  const stride = Math.max(1, Math.floor(points.length / 8));
  points.forEach((point, i) => {
    if (i === 0 || i === points.length - 1 || i % stride === 0) {
      const [x, y] = projectRoute(point); ctx.fillStyle = i === 0 ? '#ff8a61' : i === points.length - 1 ? '#e8ce82' : '#e8f4ef';
      ctx.beginPath(); ctx.arc(x, y, i === 0 || i === points.length - 1 ? 9 : 5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#d9e7e4'; ctx.font = '14px monospace'; ctx.fillText(i === 0 ? 'BASE' : i === points.length - 1 ? 'GOAL' : `W${i + 1}`, x + 12, y - 10);
    }
  });
  const position = frame?.pose?.position_enu_m;
  if (position) {
    const [x, y] = projectRoute(position); ctx.fillStyle = '#ff784f'; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, 10, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  }
}
function renderWaypoints() {
  const element = $('waypointList'), points = mission?.waypoints_enu_m || [];
  if (!points.length) {
    element.innerHTML = '<div class="empty-waypoints">Mission route will appear when the server sends a mission plan.</div>'; return;
  }
  const current = frame?.nav?.current_waypoint_index ?? 0;
  element.innerHTML = '';
  const stride = Math.max(1, Math.floor(points.length / 6));
  const indices = [...new Set([0, ...points.map((_, i) => i).filter(i => i > 0 && i < points.length - 1 && i % stride === 0), points.length - 1])];
  indices.forEach(i => {
    const point = points[i], row = document.createElement('div');
    row.className = `wp-row ${i < current ? 'complete' : i === current ? 'active' : ''}`;
    const name = i === 0 ? 'BASE / START' : i === points.length - 1 ? 'MISSION GOAL' : `WAYPOINT ${String(i).padStart(2, '0')}`;
    const marker = i < current ? '✓' : i === current ? '●' : '○';
    row.innerHTML = `<span class="wp-marker">${marker}</span><span class="wp-name">${name}<small>${fmt(point[0])} E · ${fmt(point[1])} N</small></span><span class="wp-state">${i < current ? 'PASSED' : i === current ? 'ACTIVE' : 'UPCOMING'}</span>`;
    element.append(row);
  });
}
function renderCompass(degrees) {
  $('hudHeading').textContent = `${String(Math.round(degrees || 0)).padStart(3, '0')}°`;
  $('compassTrack').style.setProperty('--heading', `${degrees || 0}deg`);
}
function norm360(value) { return ((value % 360) + 360) % 360; }
function signedAngle(value) { return ((value + 540) % 360) - 180; }
function renderImu() {
  const pitch = imu.pitch - imu.pitchZero, roll = imu.roll - imu.rollZero;
  const heading = imu.heading == null ? null : norm360(imu.heading + imu.headingOffset);
  $('imuPitch').textContent = `${fmt(pitch, 1)}°`;
  $('imuRoll').textContent = `${fmt(roll, 1)}°`;
  $('imuHeading').textContent = heading == null ? '—°' : `${Math.round(heading).toString().padStart(3,'0')}°`;
  const bearing = frame?.nav?.next_waypoint?.bearing_deg;
  const relative = heading == null || bearing == null ? null : signedAngle(bearing - heading);
  $('imuRelative').textContent = relative == null ? '—°' : `${relative > 0 ? '+' : ''}${relative.toFixed(1)}°`;
  $('imuStatus').textContent = imu.active ? 'SENSORS LIVE' : (imu.permission === 'denied' ? 'PERMISSION DENIED' : 'SENSOR OFF');
  $('imuStatus').className = imu.active ? 'connected' : 'muted';
  $('imuCalibrate').disabled = !imu.active; $('imuStop').disabled = !imu.active;
  $('imuEnable').disabled = imu.active;
  const scene = $('scene');
  if (imu.active && (imu.mode === 'both' || imu.mode === 'visor')) {
    scene.style.transform = `translate(${clamp((imu.roll-imu.rollZero)*-1.8,-70,70)}px, ${clamp((imu.pitch-imu.pitchZero)*1.5,-55,55)}px) scale(1.12)`;
    scene.style.transformOrigin = 'center center';
    $('scene').style.filter = `hue-rotate(${clamp((imu.roll-imu.rollZero)*.12,-8,8)}deg)`;
  } else { scene.style.transform = ''; scene.style.filter = ''; }
  if (imu.active && (imu.mode === 'both' || imu.mode === 'nav') && heading != null) renderCompass(heading);
  if (relative != null && (imu.mode === 'both' || imu.mode === 'nav')) {
    $('guidanceIcon').textContent = '➤';
    $('hudGuidance').textContent = Math.abs(relative) < 8 ? 'ON BEARING' : relative > 0 ? 'TURN RIGHT' : 'TURN LEFT';
    $('hudAlert').textContent = `Waypoint is ${Math.abs(relative).toFixed(1)}° ${relative > 0 ? 'right' : 'left'} of phone heading. Relative bearing is orientation guidance, not position tracking.`;
  }
}
function onOrientation(event) {
  if (!imu.active) return;
  if (Number.isFinite(event.beta)) imu.pitch = event.beta;
  if (Number.isFinite(event.gamma)) imu.roll = event.gamma;
  let h = Number.isFinite(event.webkitCompassHeading) ? event.webkitCompassHeading :
          (event.absolute && Number.isFinite(event.alpha) ? norm360(360 - event.alpha) : null);
  if (h != null) imu.heading = h;
  renderImu();
  const now = Date.now();
  if (code && now - imu.lastPost > 900) { imu.lastPost = now; postImuTelemetry(); }
}
async function postImuTelemetry() {
  if (!imu.active || !code) return;
  const heading = imu.heading == null ? null : norm360(imu.heading + imu.headingOffset);
  const payload = { pitch_deg: clamp(imu.pitch - imu.pitchZero, -180, 180), roll_deg: clamp(imu.roll - imu.rollZero, -180, 180), heading_deg: heading, mode: imu.mode, calibrated: true, source: 'deviceorientation' };
  try { await fetch(`/api/session/${encodeURIComponent(code)}/imu`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), keepalive:true}); }
  catch (error) { console.debug('IMU telemetry upload unavailable', error); }
}
async function enableImu() {
  if (!window.isSecureContext) {
    $('imuHelp').textContent = 'This page is not a secure context. Use a trusted HTTPS deployment or localhost on the phone itself; an ordinary http:// laptop LAN address will not grant iOS motion sensors. The rest of the HUD can still work.';
    return;
  }
  try {
    if (typeof DeviceOrientationEvent === 'undefined') throw new Error('Device orientation is not supported by this browser.');
    if (typeof DeviceOrientationEvent.requestPermission === 'function') {
      const permission = await DeviceOrientationEvent.requestPermission();
      if (permission !== 'granted') throw new Error('Motion & Orientation permission was not granted.');
    }
    if (typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function') {
      const motionPermission = await DeviceMotionEvent.requestPermission();
      if (motionPermission !== 'granted') throw new Error('Motion permission was not granted.');
    }
    imu.active = true; imu.permission = 'granted';
    window.addEventListener('deviceorientation', onOrientation, true);
    window.addEventListener('deviceorientationabsolute', onOrientation, true);
    $('imuHelp').textContent = 'Sensors enabled. Hold the phone in the intended viewing position, then calibrate. Heading availability and accuracy depend on device sensors and surroundings.';
    renderImu();
  } catch (error) {
    imu.active = false; imu.permission = 'denied';
    $('imuHelp').textContent = `${error.message} Check Settings → Safari → Motion & Orientation Access, then reload and tap Enable again.`;
    renderImu();
  }
}
function stopImu() {
  window.removeEventListener('deviceorientation', onOrientation, true);
  window.removeEventListener('deviceorientationabsolute', onOrientation, true);
  imu.active = false; renderImu();
}
function calibrateImu() {
  if (!imu.active) return;
  imu.pitchZero = imu.pitch; imu.rollZero = imu.roll;
  // Keep the absolute compass heading in the same north-referenced frame as mission bearings.
  $('imuHelp').textContent = "Pitch/roll zeroed. Heading remains north-referenced so it can be compared with the mission's absolute waypoint bearing. Point the top edge of the phone toward the direction you want to inspect; recalibrate pitch/roll after changing the mount.";
  renderImu(); postImuTelemetry();
}
$('imuEnable').addEventListener('click', enableImu);
$('imuStop').addEventListener('click', stopImu);
$('imuCalibrate').addEventListener('click', calibrateImu);
$('imuMode').addEventListener('change', event => { imu.mode = event.target.value; renderImu(); });

function update(data) {
  frame = data;
  if (Object.prototype.hasOwnProperty.call(data, 'mission')) setMission(data.mission);
  const pose = data.pose || {}, nav = data.nav || {}, env = data.env || {}, consumables = data.consumables || {}, alerts = data.alerts || [];
  $('dataSource').textContent = data.simulated ? 'SYNTHETIC SIMULATION STREAM' : 'SERVER TELEMETRY';
  $('missionClock').textContent = `T+${String(Math.floor((data.t_s || 0) / 60)).padStart(2, '0')}:${String(Math.floor((data.t_s || 0) % 60)).padStart(2, '0')}`;
  $('localClock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' LMST*';
  $('hudSpeed').textContent = fmt(nav.speed_mps ?? 0, 1); $('hudAltitude').textContent = fmt(pose.altitude_m ?? 0); renderCompass(nav.heading_deg ?? pose.heading_deg ?? 0);
  $('hudConfidence').textContent = String(data.confidence || '—').toUpperCase(); $('hudSigma').textContent = `σ ${fmt(pose.sigma_m, 1)} m`;
  $('hudEta').textContent = `${fmt(data.eta_remaining_min, 1)} min`; $('hudDust').textContent = fmt(env.tau, 2);
  $('dustState').textContent = env.dust_alert ? 'VISIBILITY DEGRADED' : 'ATMOSPHERIC'; $('hudDose').textContent = `${fmt(env.dose_mSv, 3)} mSv`;
  const power = Math.round((consumables.power_frac ?? 0) * 100), oxygen = Math.round((consumables.o2_frac ?? 0) * 100);
  $('hudPower').textContent = `${power}%`; $('hudO2').textContent = `${oxygen}%`;
  $('powerMeter').style.width = `${clamp(power, 0, 100)}%`; $('o2Meter').style.width = `${clamp(oxygen, 0, 100)}%`;
  $('hudReturn').textContent = `${Math.round((consumables.return_margin_frac ?? 0) * 100)}%`;
  $('hudDistance').textContent = mission ? `${fmt(mission.distance_m)} m` : '—';
  const index = nav.current_waypoint_index ?? 0, points = mission?.waypoints_enu_m || [];
  $('missionProgress').textContent = points.length ? `WAYPOINT ${Math.min(index + 1, points.length)} / ${points.length}` : 'WAYPOINT -- / --';
  const next = nav.next_waypoint || null;
  $('nextWaypoint').textContent = next?.label || (points.length ? 'ROUTE TARGET' : 'WAITING');
  $('nextDistance').textContent = next ? `${fmt(next.distance_m)} m · ${fmt(next.bearing_deg)}°` : '— m · —°';
  $('hudGuidance').textContent = `${String(data.ribbon?.style || 'standby').toUpperCase()} GUIDANCE`;
  $('guidanceIcon').textContent = data.ribbon?.style === 'bearing' ? '△' : '⌖';
  const alertText = alerts.map(alert => alert.text).join(' · ') || 'NO ACTIVE ALERTS · FOLLOW THE PLANNED TRAVERSE';
  $('hudAlert').textContent = alertText; $('hudAlert').classList.remove('message-error');
  $('sceneAlertText').textContent = alerts.length ? alerts[0].text.toUpperCase() : 'NO ACTIVE ALERTS';
  $('sceneAlert').classList.toggle('alert-active', alerts.length > 0);
  drawRoute(); renderWaypoints(); renderImu();
}
async function refreshMission() {
  const data = await fetchJSON(`/api/session/${encodeURIComponent(code)}/mission`);
  setMission(data.mission);
}
async function pollState() {
  try {
    const data = await fetchJSON(`/api/session/${encodeURIComponent(code)}/state`);
    sessionValid = true; update(data);
    if (!ws || ws.readyState !== WebSocket.OPEN) setConnection('LIVE · HTTP FALLBACK', 'connected');
  } catch (error) {
    setConnection('SERVER UNREACHABLE', 'offline'); setMessage(`Cannot reach this mission session: ${error.message}. Check the server URL and join code.`, true);
  }
}
function startPolling() {
  if (pollTimer) return;
  setConnection('USING HTTP FALLBACK', ''); pollState();
  pollTimer = window.setInterval(() => {
    pollState();
    missionRefreshTimer++;
    if (missionRefreshTimer % 5 === 0) refreshMission().catch(() => {});
  }, 2000);
}
function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = null;
}
function scheduleReconnect() {
  if (reconnectTimer || !code) return;
  reconnectTimer = window.setTimeout(() => { reconnectTimer = null; openSocket(); }, reconnectDelay);
  reconnectDelay = Math.min(15000, Math.round(reconnectDelay * 1.6));
}
function openSocket() {
  if (!code || connecting || (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))) return;
  if (!('WebSocket' in window)) { startPolling(); return; }
  connecting = true;
  try {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${scheme}//${location.host}/ws/${encodeURIComponent(code)}`);
    ws.onopen = () => { connecting = false; reconnectDelay = 2000; stopPolling(); setConnection('SERVER CONNECTED', 'connected'); };
    ws.onmessage = event => {
      try { const data = JSON.parse(event.data); sessionValid = true; update(data); setConnection('LIVE TELEMETRY', 'connected'); }
      catch (error) { console.error('Invalid HUD telemetry frame', error); }
    };
    ws.onerror = () => { connecting = false; setConnection('WS ERROR · HTTP FALLBACK', 'offline'); startPolling(); };
    ws.onclose = () => { connecting = false; setConnection('RECONNECTING…', 'offline'); startPolling(); scheduleReconnect(); };
  } catch (error) {
    connecting = false; setConnection('WS UNAVAILABLE · HTTP FALLBACK', 'offline'); startPolling(); scheduleReconnect();
  }
}
async function connect() {
  if (!code) {
    $('joinForm').hidden = false; setConnection('ENTER JOIN CODE', 'offline');
    setMessage('Enter the six-character code from the mission console to connect this phone.'); return;
  }
  $('joinForm').hidden = true; $('sessionCode').textContent = code; setConnection('CHECKING SESSION…');
  try {
    await refreshMission();
    await pollState();
    openSocket();
  } catch (error) {
    sessionValid = false; setConnection('SESSION NOT FOUND', 'offline');
    setMessage(`${error.message}. Create a new session on the mission console and open its latest HUD link.`, true);
    $('joinForm').hidden = false; $('joinCode').value = code;
  }
}
$('joinForm').addEventListener('submit', event => {
  event.preventDefault();
  const entered = $('joinCode').value.trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (entered.length !== 6) { setMessage('The mission code must contain 6 characters.', true); return; }
  code = entered;
  const url = new URL(location.href); url.searchParams.set('code', code); history.replaceState({}, '', url);
  connect();
});
$('recenterBtn').addEventListener('click', () => { drawRoute(); renderWaypoints(); });
$('fullscreenBtn').addEventListener('click', async () => {
  document.body.classList.toggle('hud-immersive');
  const enabled = document.body.classList.contains('hud-immersive');
  $('fullscreenBtn').textContent = enabled ? 'EXIT IMMERSIVE' : 'IMMERSIVE VIEW';
  if (enabled && document.documentElement.requestFullscreen) {
    try { await document.documentElement.requestFullscreen(); } catch (_) { /* CSS immersive mode still works on iOS */ }
  } else if (!enabled && document.fullscreenElement && document.exitFullscreen) {
    try { await document.exitFullscreen(); } catch (_) { /* browser may already have exited */ }
  }
});
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement) { document.body.classList.remove('hud-immersive'); $('fullscreenBtn').textContent = 'IMMERSIVE VIEW'; }
});
window.addEventListener('online', () => { if (code) { startPolling(); openSocket(); } });
window.addEventListener('beforeunload', () => { stopImu(); stopPolling(); if (reconnectTimer) clearTimeout(reconnectTimer); if (ws) ws.close(); });

if (!window.isSecureContext) $('imuHelp').textContent = 'HTTPS is required for iPhone sensor access. The HUD works over local HTTP, but Safari will normally block IMU readings there. Use a trusted HTTPS deployment for sensor testing.';
drawScene(); drawRoute(); renderImu(); connect();
