#!/usr/bin/env node
/* Mock base-station server.
 *  - serves the HUD app (so the page and the link share one origin)
 *  - GET /mission.json  -> mission file
 *  - WebSocket /ws      -> live messages (see docs/PROTOCOL.md)
 *  - type commands in this terminal:  msg <text> | goto <n> | recenter | o2 <n> | pos <x> <y> | reload
 * Env: PORT (8443), KEY + CERT (PEM paths, enables https/wss), MISSION (path to a mission json) */
const fs = require('fs'), path = require('path'), http = require('http'), https = require('https');
const { WebSocketServer } = require('ws');
const PORT = process.env.PORT || 8443, root = path.join(__dirname, '..');
const missionPath = process.env.MISSION || path.join(root, 'mission.example.json');
const FILES = { '/': 'index.html', '/index.html': 'index.html', '/sw.js': 'sw.js', '/manifest.webmanifest': 'manifest.webmanifest', '/icon.svg': 'icon.svg' };
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };
const loadMission = () => JSON.parse(fs.readFileSync(missionPath, 'utf8'));

const handler = (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  const url = req.url.split('?')[0];
  if (url === '/mission.json') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(loadMission())); }
  const f = FILES[url];
  if (!f) { res.writeHead(404); return res.end('Not found'); }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(f)] || 'text/plain' });
  res.end(fs.readFileSync(path.join(root, f)));
};

const secure = process.env.KEY && process.env.CERT;
const server = secure
  ? https.createServer({ key: fs.readFileSync(process.env.KEY), cert: fs.readFileSync(process.env.CERT) }, handler)
  : http.createServer(handler);
const wss = new WebSocketServer({ server, path: '/ws' });
const send = o => wss.clients.forEach(c => c.readyState === 1 && c.send(JSON.stringify(o)));

wss.on('connection', ws => {
  console.log('goggles connected');
  ws.send(JSON.stringify({ type: 'mission', ...loadMission() }));
  ws.on('message', m => console.log('from goggles:', String(m)));
  ws.on('close', () => console.log('goggles disconnected'));
});

let o2 = 98, bat = 100;
setInterval(() => { o2 = Math.max(0, o2 - 0.02); bat = Math.max(0, bat - 0.01); send({ type: 'telemetry', o2: Math.round(o2), battery: Math.round(bat), co2: 1 }); }, 2000);

process.stdin.setEncoding('utf8');
process.stdin.on('data', d => {
  const [cmd, ...a] = d.trim().split(' ');
  if (cmd === 'msg') send({ type: 'msg', text: a.join(' ') });
  else if (cmd === 'goto') send({ type: 'command', action: 'goto', index: +a[0] });
  else if (cmd === 'recenter') send({ type: 'command', action: 'recenter' });
  else if (cmd === 'o2') o2 = +a[0];
  else if (cmd === 'pos') send({ type: 'position', x: +a[0], y: +a[1] });
  else if (cmd === 'reload') send({ type: 'mission', ...loadMission() });
  else if (cmd) console.log('commands: msg <text> | goto <n> | recenter | o2 <n> | pos <x> <y> | reload');
});

server.listen(PORT, '0.0.0.0', () => console.log(`Base server on ${secure ? 'https' : 'http'}://0.0.0.0:${PORT}  (type "help" for commands)`));
