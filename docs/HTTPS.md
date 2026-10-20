# 🔒 HTTPS with your own address

The quick tunnel (`scripts/engine-up.sh --tunnel`) is fine for trying phone
calls, but its address changes every time. For a fixed address that a team
can use, put the engine behind a reverse proxy that handles HTTPS. The engine
keeps listening on this computer only, and the proxy is the one door in.

```
caller / teammate ──HTTPS──▶ Caddy or nginx (443) ──HTTP──▶ engine on 127.0.0.1:7860
```

## 1. Start the engine with a key and its public address

In `.env`:

```bash
VPE_API_KEY=<a long random string>          # the owner key; openssl rand -hex 24
VPE_PUBLIC_URL=https://voice.example.com      # the address people and carriers use
VPE_REMOTE_EDITING=on                         # optional: let keys save agents, data and knowledge through it
```

Leave `VPE_HOST` at its default (`127.0.0.1`) so the engine can only be
reached through the proxy. The engine refuses any other `Host` header, so
`VPE_PUBLIC_URL` must match the name in your certificate.

## 2. Caddy (simplest: it gets and renews the certificate)

`/etc/caddy/Caddyfile`:

```caddy
voice.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:7860
}
```

`sudo systemctl reload caddy`. Caddy passes websockets (browser calls and
phone audio streams) through on its own.

## 3. Or nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name voice.example.com;
    ssl_certificate     /etc/letsencrypt/live/voice.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.example.com/privkey.pem;

    client_max_body_size 25m;                 # agent imports and knowledge uploads

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;     # websockets: browser calls, phone audio
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;                   # a call can last an hour
        proxy_buffering off;                        # live events (server-sent events)
    }
}
```

## 4. Give each person their own key

Open `https://voice.example.com`, sign in with the owner key, and go to
**Team**. Make a key per person with a role:

| Role | Can |
| --- | --- |
| admin | everything: settings, integrations, keys, deleting call history |
| builder | agents, data, knowledge, tests and triggers; not settings or keys |
| viewer | read everything and run test calls and chats; change nothing |

Each key is shown once; only a fingerprint is stored. Revoke a key on the
same page and it stops working at once.

## 5. Point your phone numbers at it

Webhooks become fixed: `https://voice.example.com/v1/twilio/voice?agent=<folder>`,
`…/v1/telnyx/voice?agent=<folder>` and `…/v1/jambonz/call?agent=<folder>`.
The Phone numbers page shows them.

## Good to know

- **Changes through the proxy are opt-in.** By default, anything that saves
  (agents, data, knowledge) only works from the machine itself. Set
  `VPE_REMOTE_EDITING=on` to let keys make those changes through the proxy,
  as their role allows.
- **Settings stay local.** Settings, secrets, model downloads, sign-ins, MCP
  and connector tests, and running triggers never work through a proxy or
  tunnel, whatever the key or setting. Make those changes on the machine
  itself, or over `ssh -L 7860:127.0.0.1:7860`.
- **Docker.** `docker-up.sh` publishes the engine on `127.0.0.1:7860` with a
  key (`VPE_DOCKER_API_KEY`); point the proxy at that, and set
  `VPE_DOCKER_PUBLIC_URL` (and `VPE_REMOTE_EDITING` if you want it) in `.env`.
- **Data.** Call history and recordings stay on this machine. Use synthetic
  test data unless the machine is set up to protect real callers' details.
