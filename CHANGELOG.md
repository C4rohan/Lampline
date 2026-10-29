# Changelog

Releases are tagged on `prod` ([docs/BRANCHES.md](docs/BRANCHES.md)).

## Unreleased (on dev)

### Use it from Claude Code, Cursor or Codex
- **Claude Code plugin:** `/plugin marketplace add C4rohan/lampline`, then `/plugin install lampline@lampline`, installs Lampline's coding-agent skills. The marketplace points at the skills in `cli/src/vk/skills/`, so there's one copy of each.
- **Two new skills:** **voice-agent-fixer** makes failing tests pass by changing only the prompts or routes behind them, keeping a change only if more tests pass and none breaks; **dialogflow-tester** writes tests for an existing Dialogflow CX or CX Agent Studio agent and runs them against it live, or side by side with its Lampline copy. Both ship with `vk skills install` and the plugin.
- **`AGENTS.md`** at the repo root, for agents that read rules files: how to build, lint and test an agent folder, and the rules (synthetic data, no secrets, approval before anything goes live).
- **Releases by hand:** Actions → release → Run workflow tags prod with a version, for when a release pull request wasn't titled `Release vX.Y.Z`.

### Improve an agent
- **Improve an agent** on the Evals page: your test cases run as text chats against a copy of the agent, your model rewrites the prompts of the steps where checks failed, and a change is kept only if more checks pass and no passing case breaks. Review each prompt before and after, then apply (the previous version stays in history). Tools are simulated. Inspired by Google Research's Harness².

### Shipping
- `scripts/ship.sh` does each step: `ship.sh "message"` opens a pull request into dev, `ship.sh qa` and `ship.sh prod v0.4.0` open the promotions, `ship.sh status` shows what's waiting.
- New workflows: `sync` carries qa and prod back into dev (and a hotfix into qa) so promotions don't conflict; `ui-build` rebuilds a stale committed UI on pull requests, Dependabot's included; `release` tags and publishes when a `Release vX.Y.Z` pull request merges into prod.

### Getting started
- **No API keys:** `scripts/docker-up.sh --free` runs everything on your computer: a model in Ollama (Qwen3 4B), Moonshine listening and Kokoro speaking, downloaded on the first start with progress shown. `scripts/engine-up.sh --free` does the same natively with your own Ollama. `VPE_AUTO_DOWNLOAD=on` fetches the chosen local speech models at start.
- The README opens with a 30-second demo (import a Dialogflow CX agent, test it, grade a real phone call) and a section on free phone evals.

### Watch real calls
- **Call checks:** grade real calls automatically. Pick an agent on the Calls page, write what a good call achieves and a few checks, and each call to it is graded by your model as it ends, with a reason per check. Filter by **Failed checks**, or grade any kept call by hand. Off until you turn it on per agent; a grader that can't be reached marks the call "Not graded", never failed.
- **Response times:** each call keeps every turn's response time and shows p50 and p95 (and the slowest); the Calls page shows the last 7 days across all turns.
- **Cost per call (estimate):** speech to text, model tokens, characters spoken and phone minutes, times your rates (Integrations → Call history) or a list price Lampline knows. Local services cost nothing; parts without a rate are named.
- The call history file upgrades in place.

### A tougher Test Caller
- **Make the caller tougher** on the Evals page: background noise (busy room, street, car) at three levels, another voice for an accent, a speaking style (hesitant, terse, rambling, English as a second language), cutting in on the agent, and long pauses. Planned from a seed the run keeps, so a replay plays the same way ([docs/EVALS.md](docs/EVALS.md#make-the-caller-tougher)).
- Every eval times the agent from the caller's side, phone line included: p50, p95 and the slowest response.
- **Replay:** run a failed eval again, replay exactly what its caller said, or turn a real call on the Calls page into an eval with **Replay as a test**.
- The eval history file upgrades in place (schema 3).

### Speech to speech
- **Gemini Live** next to OpenAI Realtime (Integrations → Speech to speech → Gemini Live; `VPE_REALTIME_PROVIDER=gemini`, `VPE_GEMINI_LIVE_MODEL`, `VPE_GEMINI_LIVE_VOICE`), with your Gemini API key. Gemini Live can't change its instructions mid-call, so Lampline connects with the first step's instructions and every step's tools, and hands each new step's instructions to the model as a note from the call system (in the route's result when the model moved the call). Experimental: test an agent before real calls.

## v0.3.0

Test real phone calls, graded: an Evals tab, test scripts that turn into test cases, and Google agents side by side.

### Test real calls
- **Evals** tab: real phone calls to a voice agent, played by a Test Caller and graded against a goal and pass criteria. Through Google Voice in Chrome or Edge, the new **Lampline Phone Bridge** extension connects the call's audio to the Test Caller inside the browser (no speakers or cables, Windows or Mac); or with a phone on speaker, choosing the microphone and speaker. Runs are kept with their transcripts ([docs/EVALS.md](docs/EVALS.md)).
- Import test scripts from Hammer, Cyara, a spreadsheet or notes (text, CSV, XML or JSON): your model turns each test into a test case (what the caller says, and what has to happen), and you run any case as an eval. Keypad steps are said as digits ([docs/EVALS.md](docs/EVALS.md#import-test-scripts)).
- Test a phone agent for free from your laptop: the new Test Caller template plays a caller with a persona and a goal through the laptop's speakers and mic, with your phone on speaker carrying the call ([docs/PHONE-TESTING.md](docs/PHONE-TESTING.md)). Start steps can wait for the other side to speak first.

### Test Google agents
- `vk test --target dfcx|ces`: run the same test files against your live Dialogflow CX agent or CX Agent Studio app (text), or side by side with the engine (`--target engine,dfcx`).
- `vk test` can let a model play the caller: describe who is calling and what they want (`caller:` in a test file), and the engine's model talks to your agent until the goal is met.

### Security and reliability
- Saving an agent can no longer write a prompt file outside its folder: step and transfer ids are checked, and file names are always slugs.
- A remote save (`VPE_REMOTE_EDITING`) can't change where end steps transfer calls, what they send, or the agent's values those addresses use; and call context from the API trigger can't override the agent's own transfer or recipient addresses.
- Outbound calls never ring twice: a contact is counted before the call, a timed-out request (the phone may be ringing) answers 504 and keeps the 10-minute guard, and the API trigger takes an `Idempotency-Key`. `dry_run` must be a real true/false.
- The API key never goes in a URL: live events use a per-conversation `events_token`.
- "Only on this computer" fails closed: proxy headers or a public Host mean remote, and with a public address local-only changes also need the owner key.
- Carriers' redelivered webhooks still connect transfers and keep an outbound call's context; WhatsApp payloads are parsed defensively and each message is answered once.
- A live conversation can be watched only by the key that started it (or an admin); phone and message conversations follow the call-history rule.
- The public health check no longer shows setup details to callers without a key, and `/openapi.json` isn't served.

### Project
- The committed web UI build has fixed file names, so two branches that both rebuild it conflict instead of quietly keeping both builds.

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
