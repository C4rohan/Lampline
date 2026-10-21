# Changelog

Releases are tagged on `prod` ([docs/BRANCHES.md](docs/BRANCHES.md)).

## Unreleased (on dev)


- Test a phone agent for free from your laptop: the new Test Caller template plays a caller with a persona and a goal through the laptop's speakers and mic, with your phone on speaker carrying the call ([docs/PHONE-TESTING.md](docs/PHONE-TESTING.md)). Start steps can wait for the other side to speak first.

### Security and reliability (review of October 2026)

- Saving an agent can no longer write a prompt file outside its folder: step and transfer ids are checked, and file names are always slugs.
- A remote save (`VPE_REMOTE_EDITING`) can't change where end steps transfer calls, what they send, or the agent's values those addresses use; and call context from the API trigger can't override the agent's own transfer or recipient addresses.
- Outbound calls never ring twice: a contact is counted before the call, a timed-out request (the phone may be ringing) answers 504 and keeps the 10-minute guard, and the API trigger takes an `Idempotency-Key`. `dry_run` must be a real true/false.
- The API key never goes in a URL: live events use a per-conversation `events_token`.
- "Only on this computer" fails closed: proxy headers or a public Host mean remote, and with a public address local-only changes also need the owner key.
- Carriers' redelivered webhooks still connect transfers and keep an outbound call's context; WhatsApp payloads are parsed defensively and each message is answered once.
- A live conversation can be watched only by the key that started it (or an admin); phone and message conversations follow the call-history rule.
- The public health check no longer shows setup details to callers without a key, and `/openapi.json` isn't served.

- `vk test --target dfcx|ces`: run the same test files against your live Dialogflow CX agent or CX Agent Studio app (text), or side by side with the engine (`--target engine,dfcx`).
- `vk test` can let a model play the caller: describe who is calling and what they want (`caller:` in a test file), and the engine's model talks to your agent until the goal is met.

## v0.2.0

Faster, more natural calls; real phone-system hand-offs; ready for a team.

### Calls that feel faster
- Moonshine: free, streaming speech to text on this computer (English). The text is ready about 0.2–0.3 s after the caller stops, against 0.66 s for the default Whisper.
- Parakeet speech to text on this computer (NVIDIA Parakeet TDT 0.6B, English or 25 European languages): Whisper's accuracy or better, in 0.25–0.45 s after the caller stops on a 2-CPU machine.
- Qwen3-TTS voices from your own GPU server (vLLM-Omni), streamed.
- Speech to speech (experimental, off by default): voice calls on one model that listens and talks, over the OpenAI Realtime protocol (OpenAI's gpt-realtime, or your own server). Steps, transfers and tools work as before; agents with scripted pages keep the normal chain.
- Turn taking settings: smart turn (the default, now adjustable) or a fixed pause.
- Response time on the Test call page: how long the caller waits for each reply, split into turn detection, listening, model and speaking; also as `turn_metrics` events.

### Phone systems
- Transfer to a person: an End call step can hand the caller to a phone number or SIP address on Twilio, Telnyx and jambonz, with what the agent collected as SIP headers (and User-to-User data for Avaya and Cisco). Dialogflow CX live agent transfers import and export.
- Outbound calls over SIP through jambonz, from schedules and the API trigger, with the same limits as Twilio and Telnyx.
- HD voice (16 kHz) for SIP calls through jambonz, for trunks that carry wideband audio.

### Teams and records
- Calls page: every finished call and chat with its transcript, collected details, steps, response times and how it ended; search, filter and delete. Kept on this computer for 30 days by default. Optional recordings (off by default) as stereo WAV.
- Users and roles: named keys (admin, builder, viewer) on a new Team page, stored as fingerprints; an HTTPS guide for Caddy and nginx (docs/HTTPS.md); `VPE_REMOTE_EDITING` to allow changes through the proxy.

### Fixed
- The built-in Whisper garbled phone calls: 8 kHz phone audio is now resampled to the 16 kHz Whisper expects.

### Project
- CI scans every commit for keys and tokens.
- Pull request template, issue templates, code of conduct and this changelog.
- Engine UI on React 19.3, Radix UI 1.8, React Flow 12.12 and Tailwind CSS 4.3; Dependabot now sends one weekly pull request for the UI's packages.

## v0.1.0

The first release.

- **The engine** on Pipecat: a drag-and-drop builder where LLM steps and scripted pages mix in one agent, text chat, browser calls, and phone calls.
- **Import from Dialogflow CX and CX Agent Studio**: flows, pages, forms, routes, playbooks and tools; and export back to both.
- **20 model providers**: Claude, OpenAI-compatible, Gemini, Vertex AI, Azure OpenAI, Bedrock, Groq, Mistral, DeepSeek, Grok, Together, Fireworks, OpenRouter, Cerebras, NVIDIA NIM, Qwen, SambaNova, Nebius, Novita and Ollama.
- **Phone and SIP**: Twilio, Telnyx, and any SIP trunk, PBX or SBC through jambonz.
- **Data and knowledge** of its own: record collections and local document search.
- **Tools and triggers**: HTTP, MCP and connector tools; schedules, Telegram, WhatsApp and email.
- **`vk`**: lint with a personal-data scan, conversation tests, starter templates and coding-agent skills.
- **One Docker container** for local and on-prem, non-root and key-protected.
- Coming soon: Google Cloud deployment, and Genesys, Avaya, Cisco and other contact centers ([docs/ROADMAP.md](docs/ROADMAP.md)).
