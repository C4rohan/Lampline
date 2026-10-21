# Lampline Phone Bridge

A Chrome and Edge extension for Lampline's **Evals** tab: it connects a Google
Voice call to Lampline's Test Caller, so a real phone call to your voice agent
is played and graded without speakers, microphones or audio cables. Windows or
Mac.

Install: `chrome://extensions` → **Developer mode** → **Load unpacked** → this
folder. How to use it: [docs/EVALS.md](../docs/EVALS.md).

| File | What it does |
|---|---|
| `manifest.json` | Runs only on `voice.google.com` and on Lampline pages at `localhost` / `127.0.0.1` |
| `lampline-relay.js` | On a Lampline page: tells the Evals page the extension is installed, and passes "start an eval" on |
| `background.js` | Opens Google Voice with the number filled in, remembers that one tab, and sends its call offer to Lampline with a one-call token |
| `gv-relay.js` | In that Google Voice tab: turns the bridge on and shows a small banner |
| `gv-page.js` | In that Google Voice tab: the Test Caller's voice becomes the microphone, and the call's audio goes to Lampline |

Google Voice tabs Lampline didn't open are never changed. The extension never
presses Call: you place each call yourself.
