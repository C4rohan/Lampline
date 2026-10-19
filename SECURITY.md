# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's
**Security → Report a vulnerability** form on this repository rather than in a
public issue. You'll get an acknowledgement within a few days.

Vulnerabilities in Pipecat or jambonz themselves belong upstream, with those
projects.

## What Lampline does by default

- No telemetry: nothing is sent anywhere except the model, speech and phone services you set up.
- The Docker container publishes its port on 127.0.0.1 only and needs an API key for every request; the native engine listens on 127.0.0.1 and needs a key as soon as it's reachable from elsewhere (a tunnel or `VPE_PUBLIC_URL`).
- Keys are stored in a `chmod 600` settings file or `.env`, never in agent folders or the repo, and are never sent back to the browser.
- Phone webhooks are checked: Twilio's signature, Telnyx's ed25519 signature, jambonz's `Jambonz-Signature` HMAC; every media stream needs a one-time token from its webhook.

## The engine's connections and triggers

- Keys for Telegram, WhatsApp, email and Google live in the settings file (mode 600) or `.env`, never in agent folders, and never appear in events, errors or the browser.
- Outbound calls and messages are off until you turn them on; then quiet hours, a daily limit per agent and a 10-minute gap per person apply, and every attempt is logged with the recipient masked.
- The model never chooses who a message goes to; a conversation can send at most 5 messages.
- WhatsApp webhooks must carry a valid signature from your Meta app secret; Google sign-in uses PKCE and a one-time state; Telegram needs no inbound address at all.
- MCP servers are reached over HTTP with the same address rules as tools; an agent folder never starts a program on your computer.
- Imported exports are only read: Python tools and callbacks in them are never run.
- Records and knowledge live in one SQLite file (mode 600, folder 700) outside agent folders, so they never reach git; their pages and API only answer on this computer. Uploaded documents are only read for text: macros, scripts and links in them are never run or followed.
- A lookup returns only the fields you list, and the model never picks the collection, field or knowledge base.
- Rules on scripted pages are parsed into a small allow-listed expression tree (comparisons, and/or/not, literals, variable names) and evaluated without `eval`; anything else is refused when the agent is checked.

## What you still own

Lampline is software, not a compliance certification. Before handling PHI/PII
you need, at minimum: BAAs with every vendor in the call path (model, speech and
phone providers), SSO and an IP allowlist in front of the app, TLS on every
connection (including jambonz to the engine), log redaction, access reviews, and
your organization's own risk assessment. Use synthetic data until then.
