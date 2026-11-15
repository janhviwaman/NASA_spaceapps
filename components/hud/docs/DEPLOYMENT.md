# Deployment

## 1. Publish on GitHub Pages (demo and offline app)

1. Create a new repository on GitHub, for example `mars-eva-hud`.
2. From this folder:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: Mars EVA HUD"
   git branch -M main
   git remote add origin https://github.com/<your-username>/mars-eva-hud.git
   git push -u origin main
   ```
3. In the repository go to **Settings > Pages**, and under **Build and deployment** set **Source** to **GitHub Actions**.
4. The workflow in `.github/workflows/pages.yml` runs on every push to `main`. When it finishes (see the **Actions** tab), the site is live at:
   `https://<your-username>.github.io/mars-eva-hud/`
5. Open that link on your phone in Safari or Chrome.

GitHub Pages serves over https, so motion sensors, camera and the offline cache all work. Add the page to the home screen for a full-screen app.

**Alternative without Actions:** under Settings > Pages choose **Deploy from a branch**, branch `main`, folder `/ (root)`. This also works because `index.html` is at the root.

### Limits of the Pages version

The Pages site cannot reach a server on your local Wi-Fi unless that server uses https with a certificate the phone trusts (an https page may not call plain `http://` or `ws://` addresses). Use it for demos, sim walk, and loading mission files by hand. For the live base link use the field setup below.

## Field setup: base server on local Wi-Fi

The simplest approach is to let the base server host the app too, so the page and the link share one trusted https address.

1. Install Node.js 18 or newer and [mkcert](https://github.com/FiloSottile/mkcert).
2. Create a certificate for the server's address (replace the IP with the server's Wi-Fi IP):
   ```bash
   mkcert -install
   mkcert -key-file key.pem -cert-file cert.pem 192.168.1.10 localhost
   ```
3. Start the server:
   ```bash
   cd server
   npm install
   KEY=../key.pem CERT=../cert.pem node server.js
   ```
4. Make the phone trust the certificate: copy mkcert's root CA (`mkcert -CAROOT` shows its folder, file `rootCA.pem`) to the phone, install it, then on iPhone enable it under **Settings > General > About > Certificate Trust Settings**.
5. On the phone, join the same Wi-Fi and open `https://192.168.1.10:8443`. The address field is filled in automatically. Tap **Menu**, then **Connect**. The mission downloads and is saved on the phone.
6. Type commands in the server terminal to push messages, for example `msg Return to hab` or `goto 3`.

Use your own mission by starting the server with `MISSION=/path/to/mission.json`.

## Other hosts

The app is a static site. Netlify, Cloudflare Pages and Vercel all work: publish the repository root (or the files `index.html`, `sw.js`, `manifest.webmanifest`, `icon.svg`).

## Updating

Push to `main`. After a deploy, the phone may keep the old cached version once. Close and reopen the page (or reload twice) to get the new one. When changing the app files, raise the `CACHE` version string in `sw.js` so phones drop the old cache.

## Split hosting: HUD and planner on separate hosts

The HUD (static site) and the Jezero mission planner (Python server) are deployed independently. The HUD calls the planner directly from the phone's browser.

```
phone  --https-->  HUD on GitHub Pages            (the app)
phone  --https-->  Planner on your server         (POST /mission/plan, GET /mission/meta)
phone  --wss--->   Base server (optional)         (live position, messages, commands)
```

1. **Planner:** deploy the patched planner from the `mission-planner-server` package, with a domain and https. Set `ALLOWED_ORIGIN` to your HUD address (for example `https://<user>.github.io`).
2. **HUD:** deploy to GitHub Pages as in section 1.
3. **Connect:** open the HUD, tap **Menu**, and in **Route planner** enter the planner address (and the access key if you set one). Pick a goal and tap **Plan route**.
4. The route loads into the HUD and is saved on the phone, so it still works with no signal. Plan before leaving base.

Use the base server only if you also need live messages or position updates from the base. Planning does not need it.

### Why the planner must use https

The HUD on GitHub Pages is an https page, and browsers block it from calling a plain `http://` server. The planner needs a real certificate. The planner package includes a Caddy setup that gets one automatically.
