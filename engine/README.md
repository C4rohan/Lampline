# vpengine: the Lampline voice engine

`vpengine` runs the agent folders `vk` writes (`agent.yaml` + `prompts/*.md`)
as live conversations. It's built on [Pipecat](https://github.com/pipecat-ai/pipecat)
(BSD-2-Clause, by Daily) for audio, speech and model plumbing. The flow engine,
API, web app and test runner are this project's own code.

```bash
scripts/engine-up.sh               # from the repo root; http://localhost:7860
```

The web app has an overview, your voice
agents (steps and routes of each folder), test calls (chat or microphone, with
the steps lighting up as the call moves), Integrations and Phone numbers.

The first run creates `.venv-engine`, installs the engine, and puts the
front-desk template in `agents/` if that folder is empty.

What applies without a restart:
- **Integrations and phone settings**: the next call uses them.
- **Agent folders** (prompts, steps, tests): read again for every call.
- **The engine's own code**: `engine-up.sh` runs with auto-reload, so the
  engine restarts by itself a second after its code changes (a call in
  progress is dropped). `--no-reload` turns that off.

Only new Python packages need `engine-up.sh` to be run again, which it then
installs by itself.

## What it does today

| | |
|---|---|
| Steps and routes | Each step's prompt (plus the persona, unless the step turns it off) becomes the system instruction; each outgoing route becomes a tool whose description is the route's condition. When the model calls one, the engine moves to the next step. |
| Greeting | Spoken word for word from the start step's `greeting`, with `{{variables}}` filled. No greeting: the model speaks first. A Start transfer marked `after_greeting: true` hands the caller to its step (usually the root agent) as soon as the greeting is spoken, with no model call. |
| Transition lines | A route's `transition_speech` is spoken as the call moves. |
| Extraction | Leaving a step with extraction turned on runs one structured-output call and stores the values (shown live on the call page). |
| End steps | The model says one last thing, then the call ends. |
| Text chat | `/v1/chat/sessions`, used by the call page's Chat mode and by `vk test`. |
| Browser calls | WebRTC from the Test call page. Listening: Deepgram or Whisper on your computer. Speaking: Deepgram, Fish Audio, or Kokoro on your computer. |
| Phone calls | A Twilio or Telnyx number, or any SIP trunk, PBX or SBC through [jambonz](#sip-through-jambonz), rings the agent over a media stream. Twilio webhooks are checked against your auth token, Telnyx's against your account's ed25519 public key and jambonz's against your webhook secret; the media stream needs a one-time token from that webhook; the call hangs up when the agent reaches an end step (Telnyx: when an API key is set). |

| Tools | HTTP requests a step can make during a call (look up open times, book one). The model fills the parameters, the engine makes the request with your secrets, and the answer goes back to the model. See [Tools](#tools). |

Not yet: contact-center connections (Genesys Cloud, Avaya, Cisco, Amazon
Connect, NICE CXone, Five9, Twilio Flex: coming soon). Transfers to a person
already reach their phone systems over SIP ([Transfer to a person](#transfer-to-a-person)).

## Scripted pages and flows

Steps are LLM steps by default: the model reads the instructions and decides
what to say. A step can instead be a **scripted page**, as in Dialogflow CX:
it says exact words, asks a form's questions one at a time, checks each answer
with a built-in parser, and moves on by rules, not by the model. Both kinds mix
freely in one agent, and **flows** group steps (a "Booking" flow, a "Billing"
flow) the way Dialogflow CX flows group pages.

```yaml
- id: intake
  type: agentNode
  data:
    name: Intake
    flow: Booking
    mode: scripted               # llm (default) or scripted
    say: Let's get you booked.   # exact words on entry; {{variables}} are filled
    interpret: assisted          # assisted: the model helps read an answer the parsers can't; exact: parsers only
    max_attempts: 3              # misunderstood answers before the "no match" transfer
    no_match_say: Sorry, I didn't catch that.
    done_say: Thanks, that's everything.
    form:
      - {name: caller_name, type: text, ask: "What's your name?"}
      - {name: party, type: integer, ask: "How many people?", min: 1, max: 8,
         reprompt: "Sorry, I need a number from 1 to 8."}
      - name: plan
        type: choice
        ask: Is your plan Gold, Silver or Bronze?
        choices: [Gold, Silver, {value: Bronze, synonyms: [basic]}]
        confirm: So that's the {{plan}} plan, right?
      - {name: member_id, type: digits, ask: "Key in your member ID.", keypad: true, min_length: 4, max_length: 8}
```

Field types: `text` (optionally `pattern`, `min_length`, `max_length`),
`number`, `integer` (`min`, `max`), `yes_no`, `choice`, `date` ("next
Tuesday" becomes a YYYY-MM-DD date), `time`, `digits`, `phone`, `email`. A field already
known (from context variables or an earlier step) isn't asked again;
`required: false` lets the caller skip it. With `keypad: true` the caller can
type the answer on the phone keypad: `#` submits, `*` starts over, and it
submits by itself at `max_length` digits or after a short pause.

Transfers from a step come in four kinds:

| Transfer | When it fires |
|---|---|
| `when: plan == "Gold" and party >= 4` | a rule, checked once the form is complete (on an LLM step, after each reply) |
| `otherwise: true` | after the form is complete, when no rule matched |
| `on: no_match` | after `max_attempts` answers that couldn't be understood |
| a plain `condition` | the model decides, as on LLM steps; on a scripted page this lets a caller leave mid-form ("can I talk to a person?") |

Rules use a small, safe expression language: `== != < <= > >=`, `and or not`,
`in [...]`, numbers, `"text"`, `true false null`, and variable names, including
`current_date`, `current_weekday`, `current_hour` and `current_time` (24-hour,
so `current_time >= "17:00"` works). Rules never run code. A rule that sends
the call round in a loop stops after 12 hops.

Scripted pages make no model calls for anything the parsers can read, so they
are fast, cheap and say exactly what you wrote: good for collecting an ID or a
date, reading a disclosure, or a menu. Use LLM steps where callers need to be
understood loosely.

A scripted page can also run tools itself, without the model, the way
Dialogflow CX pages call webhooks:

```yaml
    on_entry: [todays_specials]     # before it speaks: its `say` can use what they save
    on_done: [find_visit]           # once the form is complete, before its rules
```

Their arguments come from the tool's `with` values and from variables of the
same name, and what they `save` is there for the page's lines and rules
(`visit_status == "confirmed"`).

## Data and knowledge

The engine keeps two kinds of data of its own, in one SQLite file in
`~/.local/share/vpengine` (or `VPE_DATA_DIR`), never in an agent folder:

- **Data**: collections of records (JSON documents), like `appointments` or
  `callbacks`. Manage them on the **Data** page: create a collection (with an
  optional key field, like `confirmation`), import CSV or JSON, add and edit
  records.
- **Knowledge**: knowledge bases of documents (.md, .txt, .html, .pdf, .docx,
  .csv, .json, up to 20 MB each), split into passages. Search ranks passages by
  keywords (SQLite FTS5, BM25) and by meaning, then merges the two. Meaning
  search uses all-MiniLM-L6-v2 run on this computer with onnxruntime (a 90 MB
  download from the Knowledge page), or any OpenAI-compatible embeddings server
  such as Ollama (Integrations → Knowledge search). Without either, keyword
  search still works.

Agents use them through three tools (builder: **New tool**):

| `connector` | What it does | `with` |
|---|---|---|
| `data.lookup` | find records whose field equals a value; case, spaces and punctuation don't matter (“hc 4271” finds HC-4271) | `collection`, `field`, optional `value` (else the model gives it), `fields` (only these go back), `where`, `limit` |
| `data.save` | add a record, or update the one with the same key | `collection`, `values` (fixed or `{{variables}}`), optional `ask` (names the model fills in) |
| `knowledge.search` | the best passages for a question, with their document | `base`, optional `results` (default 3) |

```yaml
- name: find_visit
  connector: data.lookup
  description: Find the caller's appointment.
  with: {collection: appointments, field: confirmation, value: "{{confirmation}}", fields: [name, day, status]}
  save: {visit_status: records.0.status, visit_day: records.0.day}
- name: search_faq
  connector: knowledge.search
  description: Questions about hours, parking or insurance.
  with: {base: clinic-faq}
```

Lookups and searches read the real data even in test chats (they only read,
and only on this computer); saves follow the “Mock tool answers” switch. When
nothing matches, a search tells the model to say it doesn't know rather than
guess. The Data and Knowledge pages, and their API, only work from this
computer, never through the tunnel. Imports and new records are checked for
things that look like real personal details (the same scan as `vk lint`) and
flagged. Use synthetic data unless the engine runs where real patient data is
allowed.

## Tools

Defined once per agent in `agent.yaml` (or in the builder: **Tools → New
tool**), then ticked on the steps that may use them:

```yaml
tools:
- name: find_open_slots
  description: Look up open appointment times on a day. Call it once you know the day.
  method: GET                     # GET and DELETE send parameters as the query; others as a JSON body
  url: https://scheduling.example.com/v1/clinics/{{clinic_id}}/slots
  headers: {Authorization: "Bearer {{secrets.SCHEDULING_KEY}}"}
  parameters:
  - {name: day, type: string, description: the day the caller wants, required: true}
  body: {slot: "{{time}}"}        # optional JSON template (POST/PUT/PATCH)
  say_while_running: One moment while I check.
  save: {first_open_time: slots.0.time}    # keep parts of the answer as variables
  timeout_seconds: 8              # 1 to 30
  mock: {slots: [{time: "9:30 AM"}]}       # used by tests and while url is empty
nodes:
- id: find
  type: agentNode
  data: {name: Find a time, tools: [find_open_slots], ...}
```

- `{{name}}` is a parameter the model filled in, else a session variable.
  `{{secrets.NAME}}` is a **tool secret**: saved on the Integrations page (or
  `VPE_SECRET_NAME` in `.env`), stored only in your settings file, never in the
  agent folder or the browser.
- The address (scheme and host) can only use secrets, never values a caller
  could influence; parameters in the path are URL-encoded. Redirects aren't
  followed, answers are capped at 1 MB and trimmed to 4,000 characters for the
  model, and events show only the method, host and path.
- A failed request (timeout, 4xx/5xx, unreachable) goes back to the model as an
  error so it can apologize and offer another way; the call carries on.
- **Mock tool answers** on the Test call page, and `vk test`, use each
  tool's `mock` instead of calling out. In the builder, **Try it** runs a tool
  once with the mock or against the real service.

### MCP servers

A tool can be an [MCP](https://modelcontextprotocol.io) server instead of one
request: every tool it lists becomes a tool the model can call (builder:
**New tool → MCP server**, then **Refresh**).

```yaml
tools:
- name: clinic
  description: The clinic's scheduling server.
  mcp:
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer {{secrets.CLINIC_MCP}}"}
    tools:                         # filled by Refresh; delete any you don't want offered
    - {name: find_slots, description: Open slots for a day, input_schema: {type: object, properties: {day: {type: string}}, required: [day]}}
  mock: {find_slots: {slots: ["9:30 AM"]}}
```

Streamable HTTP only (the transport CX Agent Studio uses too), with the
initialize handshake or the newer handshake-free protocol. The same address
rules, time limits and 1 MB cap as HTTP tools apply. Programs on this computer
(stdio servers) are never started from an agent folder.

### Connector actions

Ready-made actions that use the connections on the Integrations page, so the
agent folder holds no keys (builder: **New tool**, then the action):

| `connector` | What it does | Needs |
|---|---|---|
| `email.send` | send an email | Email (a mail server, or Gmail) |
| `whatsapp.send` | send a WhatsApp message | WhatsApp |
| `telegram.send` | send a Telegram message | Telegram |
| `calendar.free_times` | open times on a day, within opening hours | Google |
| `calendar.book` | book an event (checks it's free and within opening hours first) | Google |

```yaml
- name: send_confirmation
  connector: email.send
  description: Once the caller confirms, email them the booking details.
  with: {to: "{{caller_email}}", subject: "Your appointment"}   # fixed values; the model fills the rest
  mock: {sent: true}
```

The model never chooses who a message goes to: set `to` (or `chat_id`) in
`with`, or leave it out in a WhatsApp, Telegram or email conversation to reply
to whoever wrote in. A conversation can send at most 5 messages. Steps with
tools are told today's date and time (in the time zone from Integrations).

## Triggers and channels

What starts an agent besides a phone call, and what happens after a
conversation (builder: **Triggers → New trigger**). They live in `agent.yaml`
under `triggers:` (local to the folder, like tools) and run while the engine is
running:

| Trigger | |
|---|---|
| Telegram messages | people chat with the agent through your bot; the engine checks for messages itself, so no public address is needed |
| WhatsApp messages | Meta's Cloud API calls `{VPE_PUBLIC_URL}/v1/whatsapp/webhook` (signed with your app secret) |
| Emails | unread mail in your inbox is answered in the same thread (a mail server's IMAP, or Gmail), checked every minute |
| After a conversation | send a note by email, WhatsApp, Telegram or webhook, with `{{summary}}` (written by your model), `{{details}}` (what was collected), `{{transcript}}` and any variable; filter by `when` and by channel (browser tests are skipped unless you tick them) |
| On a schedule | cron in your time zone; call or message a list of people (or a list fetched from a URL), like tomorrow's reminders |
| API trigger | The API Trigger node: `POST /v1/public/agent/<path>` with `phone_number` (a call) or `channel` + `to` (a message), plus `initial_context`; `"dry_run": true` checks without sending |
| Webhook after the call | A Webhook node: posts `payload_template` (`{{workflow_run_id}}`, `{{gathered_context.x}}`, …) to your system |

Each person who writes in gets their own conversation, which ends after 30
minutes without a reply (after-call triggers then run). Messages count as
replies, so an outbound message's answer continues the same conversation.

**Outbound calls and messages are off** until you turn them on in
Integrations → Automation. Then they're kept out of quiet hours (default
21:00 to 08:00), to a daily limit per agent (default 50), and to one contact
per person per agent every 10 minutes. A contact is counted as it starts and
given back only when the provider certainly refused it, so a call whose answer
timed out (it may be ringing) can't be placed twice.
Every attempt is logged (recipients masked) under Automation → Recent
activity. Outbound calls need Twilio (account SID, auth token and a number),
Telnyx (API key, TeXML application and a number) or jambonz (API key, account
and application SIDs and a number), and the public address.
WhatsApp only allows approved templates more than 24 hours after a person last
wrote, so reminders outside that window need a template on Meta's side.


## Builder

Voice agents → **Open in builder** (or **New agent**) opens a canvas in the
style of CX Agent Studio:

- **Start** greets the caller and hands over to the **root agent**, which
  transfers the caller to **sub-agents**; **End call** steps say goodbye and
  hang up. **Global instructions** (persona, tone, rules) are added to every
  step that includes them.
- Drag a sub-agent, end call or global instructions from the left onto the
  canvas. Drag from the dot under a card onto another card to add a transfer.
  Click anything to edit its name, instructions, greeting, details to collect,
  transfer label and condition in the right panel.
- **Scripted page** (left) adds a page with exact wording, form fields and
  rules; any step can switch between LLM and scripted at the top of its panel.
  A transfer's panel picks its kind: the model decides, a rule, otherwise, or
  no match. Rules are dashed on the canvas.
- **Flows** (left) lists the step groups. Click one to highlight it; new steps
  join the highlighted flow. Each flow has its own colour on the cards.
- **Tools** (left panel) are HTTP requests the agent can make: edit the URL,
  headers, parameters, mock answer and what to save, **Try it**, then tick the
  tool on the steps that may use it.
- Every edit is checked in the background with the same rules as `vk lint`;
  problems show on the cards, the transfers and in the panel.
- Undo and redo (⌘/Ctrl Z, ⇧⌘/Ctrl Z), save (⌘/Ctrl S), tidy layout, zoom and
  pan (scroll, or pinch / ⌘ + scroll).
- Saving writes the agent's folder (`agent.yaml` + `prompts/*.md`), so changes
  show up as readable git diffs. The previous version is kept in
  `~/.cache/vpengine/history/<agent>/` (last 30 saves). Saving and creating
  agents only work from this computer, never through a tunnel.

**New agent** offers three ways to start:

- **Describe it**: say in a sentence or two what callers need. Your model
  drafts a root agent, sub-agents with when-to-transfer conditions, details to
  collect and tools with example answers; it opens in the builder to review and
  test. (Also on the Overview page.)
- **Template or blank**.
- **Import**: drop a Dialogflow CX or CX Agent Studio export (.zip). See
  [`vk import`](../cli/README.md#import) for what carries over.

## Integrations

The Integrations page shows every service as a tile (model, listening,
speaking, phone numbers and SIP, contact centers (coming soon), messaging and email, Google Calendar and Gmail, MCP
servers, automation, tool secrets, workspace) with its status: in use,
connected, or not set up. Click one to set it up, switch to it, or test it.
Tiles show monogram icons; to show a company's own logo, put its file in
`~/.config/vpengine/logos/` named after the tile (`anthropic`, `openai`,
`google`, `groq`, `bedrock`, … `deepgram`, `fish`, `whisper`, `kokoro`, `twilio`, `telnyx`, `jambonz`, `telegram`,
`whatsapp`, `email`, `google`, `mcp`; `.svg`, `.png` or
`.webp`). Logos aren't bundled with this project.

Set the model, listening, speaking, Twilio and the workspace name on the
Integrations page. It saves to `~/.config/vpengine/settings.json` (mode 600;
`VPE_SETTINGS_FILE` to move it), never to the repo or an agent folder. Saved
keys are never sent back to the browser, only "saved (…last 4)". Values in
`.env` apply until you save one on the page; clear a saved value to fall back.
Settings can only be changed from this computer, not through a tunnel.

| Model | Settings | |
|---|---|---|
| Claude (Anthropic) | `ANTHROPIC_API_KEY` | default `claude-haiku-5-5`; prompt caching on, thinking off |
| OpenAI-compatible | `OPENAI_API_KEY`, `VPE_LLM_BASE_URL` | OpenAI, a gateway, or any compatible server (a local one needs only the URL) |
| Gemini | `GEMINI_API_KEY` | Google AI Studio |
| Gemini on Vertex AI | `GOOGLE_CLOUD_PROJECT`, `VPE_VERTEX_LOCATION`, `VPE_VERTEX_CREDENTIALS` | a pasted service account key, or this computer's Google credentials |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | the model is your deployment's name |
| Amazon Bedrock | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | or this computer's AWS credentials |
| Groq, Mistral, DeepSeek, Grok (xAI), Together AI, Fireworks AI, OpenRouter, Cerebras, NVIDIA NIM, Qwen, SambaNova, Nebius, Novita AI | `GROQ_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, `DASHSCOPE_API_KEY`, `SAMBANOVA_API_KEY`, `NEBIUS_API_KEY`, `NOVITA_API_KEY` | each through Pipecat's own service for it |
| Ollama | `OLLAMA_BASE_URL` | open models on this computer or your own server, free |

Pick one with `VPE_LLM` (the names are in `.env.example`) or on the Integrations
page; each starts on a sensible default model, which you can change. Switching
provider on the page starts the new one on its own default. **Test connection**
asks the provider for its model list (Vertex AI and Bedrock: checks that the
settings are accepted). Tool calling and transfers work with every provider;
how well an agent follows its steps still depends on the model.

| Listening | |
|---|---|
| Deepgram | `DEEPGRAM_API_KEY`; new accounts get free credit |
| Fish Audio | `FISH_API_KEY` (the same key as speaking); each caller utterance goes to Fish's speech-to-text (`transcribe-1`, $0.36 per audio hour on Fish's price list) |
| Whisper on this computer (free) | runs inside the engine (faster-whisper, int8 on the CPU); no Docker, no key |
| Moonshine on this computer (free, streaming) | runs inside the engine and transcribes while the caller talks, so the text is ready moments after they stop; English; Linux or an Apple-silicon Mac on macOS 15 ([details](#moonshine-streaming-speech-to-text)) |
| Whisper server (advanced) | any OpenAI-style transcription server, e.g. Speaches in Docker |

| Speaking | |
|---|---|
| Deepgram | same key as listening |
| Fish Audio | `FISH_API_KEY`; pick from your own voices or popular English ones on the Integrations page, or paste a voice ID. The `s2.1-pro-free` model costs nothing during Fish's promotion (through Nov 30, 2026), and free-tier requests may be used to improve their models, so test data only |
| Kokoro on this computer (free) | runs inside the engine (Kokoro-82M on ONNX Runtime); no Docker, no key |
| Kokoro server (advanced) | a Kokoro-FastAPI server, e.g. in Docker |

| Messaging and apps | |
|---|---|
| Telegram | a bot token from @BotFather |
| WhatsApp | Meta Cloud API: phone number ID, access token, app secret (checks webhook signatures), a verify token you choose |
| Email | a mail server (address, app password, SMTP and IMAP hosts), or Gmail through Google |
| Google Calendar and Gmail | your own OAuth client (type "Desktop app") in Google Cloud, then **Connect Google account** (open the app at `http://localhost` for that step). Scopes: calendar events and free/busy, Gmail send and modify. The refresh token is saved in your settings file like any key. Opening hours, open days and the calendar used are set here too |

### Fish Audio in detail

One API key covers listening and speaking. Everything below is on the
Integrations page (or the environment variable in brackets).

| Speaking model (`VPE_FISH_MODEL`) | Price per million characters (Fish's list, October 2026) |
|---|---|
| `s2.1-pro-free` (default) | free during Fish's promotion, through Nov 30, 2026; free-tier requests may be used to improve their models |
| `s2.1-pro` | $15, the current model |
| `s2-pro` | $15, the previous model |
| `drama-3` | in Fish's API, not on its price list yet |
| `s1` | $15; deprecated, retires Dec 31, 2026 (then billed as `s2.1-pro`) |

| Voice setting | Values |
|---|---|
| Voice (`VPE_FISH_VOICE`) | your own voices or popular English ones from the picker, any voice ID, or empty for Fish's default |
| Connection (`VPE_FISH_CONNECTION`) | `auto` / `http` (default): Fish's HTTP endpoint, where Fish documents the free model. `live`: the WebSocket stream, a little faster, but it answered "402 no API credit" for an account without credit even with the free model |
| Latency (`VPE_FISH_LATENCY`) | `balanced` (default, best for calls), `normal` (best quality), `low` (fastest) |
| Read numbers and dates as words (`VPE_FISH_NORMALIZE`) | `on` (default) or `off` |
| Speed (`VPE_FISH_SPEED`) | 0.5 to 2.0 (default 1.0) |
| Volume (`VPE_FISH_VOLUME`) | -20 to 20 dB (default 0) |
| Temperature (`VPE_FISH_TEMPERATURE`) | 0 to 1 (default 0.7): higher is more expressive |
| Top P (`VPE_FISH_TOP_P`) | 0 to 1 (default 0.7) |

| Listening model (`VPE_FISH_STT_MODEL`) | Price per audio hour |
|---|---|
| `transcribe-1` (default) | $0.36; short recordings, best for a caller's turns |
| `transcribe-1-pro` | $0.36; long, multi-speaker audio (sent with speaker labels and sound cues off) |

Caller language (`VPE_FISH_STT_LANGUAGE`): a two-letter code (default `en`), or
empty to let Fish detect it. Fish's batch-only options (chunk sizes,
repetition penalty, MP3/Opus bitrates, multi-speaker voices) don't apply to the
live stream calls use, so they aren't offered.

The built-in options need their models once: press **Download models** on the
Integrations page (about 485 MB with the default Whisper `base.en`; `small.en`
is more accurate, `tiny.en` faster). They download into `~/.cache/vpengine`
(`VPE_MODEL_CACHE` to move it) from Hugging Face and the kokoro-onnx GitHub
release, then load once when the engine starts and are shared by every call.

The server options show whether the servers are running, with a **Start local
speech** button: the engine runs `docker compose` on
`local/docker-compose.speech.yml` (a fixed command; nothing from the browser
reaches it, and it can't be triggered through a tunnel). `scripts/speech-up.sh`
does the same from a terminal (`--down` stops it). Both need Docker running.

Tests: `VPE_TEST_MODELS=1 pytest engine/tests/test_builtin_speech.py` downloads
the real models, has Kokoro say a sentence and Whisper hear it back, and runs a
whole call on built-in speech.

## Docker

`scripts/docker-up.sh` (or `docker compose up -d --build`) runs the engine in
one container, built from the `Dockerfile` at the top of the repo:

- **Address:** published on `127.0.0.1:7860` only (`VPE_DOCKER_PORT` to
  change). Every request needs the API key, `VPE_DOCKER_API_KEY` in `.env`,
  which the script creates. Inside the container the port mapping hides where a
  request comes from, so there the key is what proves a request comes from this
  computer; requests through a tunnel never count.
- **Folders:** `./agents` → `/agents`, `./.vpe-data` → `/data` (settings and
  keys, records, knowledge, downloaded models). The container runs as your user
  (`VPE_UID`/`VPE_GID`, saved in `.env`), not root, with every Linux capability
  dropped.
- **Ollama on this computer:** use `http://host.docker.internal:11434/v1` as the
  model's or the embeddings' address.
- **Phone numbers:** they need a public https address. Create a Cloudflare named
  tunnel whose hostname points at `http://engine:7860`, put
  `CLOUDFLARE_TUNNEL_TOKEN` and `VPE_DOCKER_PUBLIC_URL=https://…` in `.env`,
  then `scripts/docker-up.sh --tunnel`.
- **What's inside:** Python 3.12 slim, the engine and `vk` (so
  `docker compose exec engine vk lint /agents/front-desk` works), and the
  built-in Whisper and Kokoro. Starting local Whisper or Kokoro servers from
  Integrations needs Docker on the host, so use the built-in ones.
- **Licences:** the image includes phonemizer and espeak-ng (GPL-3.0) through
  the Kokoro extra. Building and running it yourself is fine; if you publish the
  image, follow their terms (see NOTICE).

## Settings from the environment

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Claude, through Anthropic's own API (prompt caching on, thinking off) |
| `VPE_LLM` | `anthropic` | the model provider: see [Integrations](#integrations) for all of them and their keys |
| `VPE_LLM_MODEL` | the provider's default | |
| `VPE_LLM_BASE_URL`, `OPENAI_API_KEY` | | for `VPE_LLM=openai` (a local server needs only the URL) |
| `VPE_LLM_TEMPERATURE` | provider default | |
| `VPE_STT` / `VPE_TTS` | `deepgram` / `deepgram` | `whisper`, `moonshine`, `parakeet`, `whisper-server`; `fish`, `kokoro`, `kokoro-server`, `qwen3-tts` |
| `VPE_LOCAL_WHISPER_MODEL` | `base.en` | built-in Whisper: `tiny.en`, `base.en`, `small.en`, `distil-small.en` |
| `VPE_MOONSHINE_MODEL` | `small-streaming` | `tiny-streaming` for the fastest replies and the lightest CPU load |
| `VPE_VOICE_MODE` | `chain` | `speech-to-speech`: one model listens and talks (experimental) |
| `VPE_REALTIME_URL`, `VPE_REALTIME_MODEL`, `VPE_REALTIME_VOICE`, `VPE_REALTIME_API_KEY`, `VPE_REALTIME_TURNS` | `wss://api.openai.com/v1/realtime`, `gpt-realtime`, `marin`, the OpenAI key, `semantic` | the speech-to-speech server |
| `VPE_PARAKEET_MODEL` | `parakeet-tdt-0.6b-v2` | `parakeet-tdt-0.6b-v3` for 25 European languages |
| `VPE_JAMBONZ_SAMPLE_RATE` | `8000` | `16000` for HD voice on SIP calls through jambonz |
| `VPE_TURN_DETECTION` / `VPE_TURN_PAUSE` | `smart` / `3` (`0.8` with `pause`) | how a call decides the caller has finished: [Turn taking](#turn-taking-and-response-time) |
| `VPE_MODEL_CACHE` | `~/.cache/vpengine` | where built-in models download |
| `DEEPGRAM_API_KEY` | | voice calls; text chat works without it |
| `VPE_STT_MODEL` / `VPE_TTS_VOICE` | `nova-3-general` / `aura-2-helena-en` | Deepgram |
| `VPE_WHISPER_URL` / `VPE_WHISPER_MODEL` | `http://localhost:8001/v1` / `Systran/faster-whisper-small` | Whisper server |
| `FISH_API_KEY`, `VPE_FISH_VOICE`, `VPE_FISH_MODEL` | model `s2.1-pro-free` | voice ID from fish.audio; empty uses their default |
| `VPE_KOKORO_URL` / `VPE_KOKORO_VOICE` | `http://localhost:8880/v1` / `af_heart` | the voice is used by built-in Kokoro too |
| `VPE_QWEN3_TTS_URL`, `VPE_QWEN3_TTS_VOICE`, `VPE_QWEN3_TTS_LANGUAGE`, `VPE_QWEN3_TTS_MODEL` | `http://localhost:8091/v1`, `vivian`, `Auto`, `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` | a vLLM-Omni server |
| `VPE_API_KEY` | | require `X-API-Key` on `/v1/*` (Twilio's routes use their own checks); required to listen beyond 127.0.0.1 or with `VPE_PUBLIC_URL` |
| `VPE_PUBLIC_URL` | | the https address Twilio, Telnyx or jambonz reach the engine at (`--tunnel` sets it) |
| `TWILIO_AUTH_TOKEN` | | checks Twilio's webhook signatures and hangs up finished calls |
| `TELNYX_PUBLIC_KEY` / `TELNYX_API_KEY` | | checks Telnyx's webhook signatures / hangs up finished calls |
| `JAMBONZ_WEBHOOK_SECRET` | | checks jambonz's webhook signatures (SIP calls) |
| `VPE_FISH_STT_MODEL` | `transcribe-1` | or `transcribe-1-pro` |
| `VPE_MAX_SESSIONS` | 20 | chats and calls at once |
| `VPE_ICE_SERVERS` | none | comma-separated STUN/TURN URLs, only needed when the browser isn't on the same machine or network |
| `BRAND_NAME` | Lampline | the workspace name |
| `VPE_SETTINGS_FILE` | `~/.config/vpengine/settings.json` | where Integrations saves |
| `VPE_REMOTE_EDITING` | off | `on` lets keys save agents, data and knowledge through an HTTPS proxy ([docs/HTTPS.md](../docs/HTTPS.md)) |
| `VPE_CALL_HISTORY` | `on` | keep finished calls on the Calls page |
| `VPE_CALL_HISTORY_DAYS` | 30 | then they're deleted, recordings included |
| `VPE_CALL_RECORDINGS` | `off` | `on` saves voice calls as stereo WAV |
| `TWILIO_ACCOUNT_SID`, `TWILIO_FROM_NUMBER` | | outbound calls through Twilio |
| `TELNYX_TEXML_APP_ID`, `TELNYX_FROM_NUMBER` | | outbound calls through Telnyx |
| `JAMBONZ_API_URL`, `JAMBONZ_ACCOUNT_SID`, `JAMBONZ_API_KEY`, `JAMBONZ_APPLICATION_SID`, `JAMBONZ_FROM_NUMBER` | `https://api.jambonz.cloud` | outbound calls over SIP through jambonz |
| `TELEGRAM_BOT_TOKEN` | | Telegram |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | | WhatsApp Cloud API |
| `VPE_EMAIL_PROVIDER` | `smtp` | or `gmail` |
| `VPE_EMAIL_ADDRESS`, `VPE_EMAIL_PASSWORD`, `VPE_SMTP_HOST`, `VPE_SMTP_PORT`, `VPE_IMAP_HOST` | port 587 | a mail server (465 uses TLS from the start) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | | Google; the refresh token is normally saved by **Connect Google account** |
| `VPE_CALENDAR_ID`, `VPE_CALENDAR_HOURS`, `VPE_CALENDAR_DAYS` | `primary`, `09:00-17:00`, `mon-fri` | calendar actions |
| `VPE_TIMEZONE` | this computer's | schedules, quiet hours, calendars, "today" for tools |
| `VPE_OUTBOUND` | `off` | `on` lets triggers call and message people |
| `VPE_QUIET_HOURS`, `VPE_OUTBOUND_DAILY_CAP` | `21:00-08:00`, `50` | outbound limits |

## Phone calls (Twilio, Telnyx or SIP)

Twilio has to reach the engine over the internet. `--tunnel` opens a free
Cloudflare quick tunnel (no Cloudflare account needed) for as long as the
engine runs.

1. `brew install cloudflared`
2. Buy a voice-capable number in the Twilio Console (US list prices in
   October 2026: $1.15 a month, plus $0.0085 a minute to receive a call and
   $0.0044 a minute for the media stream). Add
   `TWILIO_AUTH_TOKEN=...` from Console > Account info to `.env`.
3. `scripts/engine-up.sh --tunnel`. It prints the webhook address.
4. In Twilio: Phone Numbers > your number > Voice configuration > "A call comes in":
   Webhook, `https://<printed>.trycloudflare.com/v1/twilio/voice?agent=front-desk`, HTTP POST. Save.
5. Call the number.

Telnyx works the same way: paste the public key from Mission Control (Keys &
Credentials) on the Phone numbers page (and an API key if you want the engine
to hang up), create a TeXML Application whose voice URL is
`https://<printed>.trycloudflare.com/v1/telnyx/voice?agent=front-desk` (POST),
and assign your number to it. The engine answers with a bidirectional RTP
stream (`<Connect><Stream bidirectionalMode="rtp">`).

While the tunnel is open every API needs a key (`VPE_TUNNEL_API_KEY`, created
in `.env` on first use); the call page asks for it once, and `vk test`
reads it from `VK_ENGINE_KEY`. The Phone numbers page shows the webhook to
copy while the tunnel is open. The quick-tunnel address changes on every
start, so step 4 is repeated each time; a named Cloudflare tunnel or any
https reverse proxy with `VPE_PUBLIC_URL` set gives a fixed address.

### SIP through jambonz

[jambonz](https://jambonz.org) (MIT, self-hosted or jambonz.cloud) connects SIP
trunks, phone systems and session border controllers to the engine, so calls
from your own carrier or PBX reach an agent without Twilio or Telnyx.

1. In jambonz, add your SIP trunk, PBX or SBC as a carrier and a phone number
   that routes to it.
2. Copy the account's webhook secret (Account → Webhook secret) to the Phone
   numbers page, or `JAMBONZ_WEBHOOK_SECRET` in `.env`.
3. Give the engine an address jambonz can reach: `scripts/engine-up.sh --tunnel`,
   or `VPE_PUBLIC_URL` when jambonz runs on your own network.
4. Create a jambonz application. Calling webhook:
   `https://<address>/v1/jambonz/call?agent=front-desk` (POST). Call status
   webhook: `https://<address>/v1/jambonz/done` (POST). Point the number at it.
5. Call the number.

The engine checks the `Jambonz-Signature` on every webhook and answers with a
`listen` verb: the call's audio streams to `/v1/jambonz/stream` as linear PCM
in both directions (8 kHz phone audio, or 16 kHz HD voice when you choose it
on the jambonz tile or set `VPE_JAMBONZ_SAMPLE_RATE=16000`; HD voice helps
listening when your trunk carries G.722, Opus or wideband SIP), with keypad presses passed through and a
one-time token in the stream's address. When the caller talks over the agent
the engine sends `killAudio`; when the agent ends the call it sends
`disconnect` and jambonz hangs up, or dials the person the step transfers to.

Outbound calls (schedules and the API trigger) go out the same way, through
your own carrier: on the jambonz tile, under "Outbound calls", add the API
address (`https://api.jambonz.cloud` or your own jambonz), the account SID, an
API key (Account → API keys), the application's SID and the number calls come
from. The engine asks jambonz's REST API to place the call through that
application, and tags it with the agent and the call's details; when the
person answers, the application's calling webhook runs the agent as it does
for inbound calls. Outbound has the same limits as Twilio and Telnyx: turned
on under Automation, quiet hours, a daily cap, one contact per person every 10
minutes.

### Transfer to a person

An End call step can hand the caller to a person instead of hanging up. In the
builder, open the step and fill in **Transfer to a person**: a phone number
(`+15551234567`) or a SIP address on your phone system or contact center
(`sip:desk@pbx.example.com`); `{{variables}}` work, so a lookup can pick the
desk. The agent says its line, then the carrier connects the caller.

The person picking up gets what the agent collected. On a SIP address the
details travel as SIP headers: `X-Lampline-Session` (the session ID, to look
the call up afterwards) and `X-Lampline-<name>` for each detail, ASCII and at
most 200 characters each. Choose which details to send, or leave the list
empty to send everything this call collected. For Avaya and Cisco, the step
can also send User-to-User data (`transfer_uui`, hex encoded as RFC 7433
describes), which their screen pops read. A phone number on the public
network can't carry headers; the details stay in the call's `transfer` event,
and after-call triggers can send them on (`{{transferred_to}}`, `{{details}}`,
`{{session_id}}`).

| Carrier | How the transfer happens |
| --- | --- |
| Twilio | The stream's TwiML ends with a redirect to `/v1/twilio/after`, which answers `<Dial>` with the number or SIP address |
| Telnyx | Call Control's transfer command (needs the API key on the Phone numbers page) |
| jambonz | The `listen` verb's action hook (`/v1/jambonz/done`) answers with a `dial` verb |

In text and browser calls, the Test call page shows where the caller would
have gone and what would have been sent. `vk lint` checks the address
(VK134). Dialogflow CX imports and exports map it to a live agent transfer
(`telephonyTransferCall`).

### Moonshine: streaming speech to text

[Moonshine](https://github.com/moonshine-ai/moonshine) (MIT) streams: it
transcribes 20 ms at a time while the caller talks, and when voice detection
hears them stop it finishes the line at once. Whisper waits for the whole turn
and then transcribes it, so the caller waits for all of it.

Measured on a 2-CPU cloud machine, real-time audio, from the end of speech to
the final text (lower is better):

| | After the caller stops | CPU while they talk |
|---|---|---|
| Whisper `base.en` (the Whisper default) | 0.66 s | |
| Whisper `tiny.en` | 0.35 s | |
| Moonshine small (the default) | 0.30 s | about half a core |
| Moonshine tiny | 0.17 s | about a third of a core |

All four transcribed the test sentences word for word, at 16 kHz and on 8 kHz
phone audio. Laptops and desktops are faster than this machine across the
board. The cost is CPU while the caller talks, so a small machine takes fewer
calls at once than with Whisper.

Choose it in Integrations → Listening → **Moonshine**, pick small or tiny, and
press **Download model** (140 or 46 MB, once, into `VPE_MODEL_CACHE`). It is
English only here: some of Moonshine's other languages use a non-commercial
licence. It installs with the engine on Linux and on Apple-silicon Macs with
macOS 15 or later (`pip install -e "engine[moonshine]"`; `scripts/engine-up.sh`
and the Docker image include it); elsewhere the tile says it isn't available.

### Parakeet: the most accurate speech to text on this computer

[Parakeet TDT 0.6B](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) is
NVIDIA's open speech recognition model (CC-BY-4.0), at the top of open English
leaderboards. It runs inside the engine through
[onnx-asr](https://github.com/istupakov/onnx-asr) (MIT) with int8 ONNX
exports, and transcribes each turn when the caller stops, like Whisper.

Same machine and clips as the Moonshine table, from the end of a 2 to 5 second
turn to the final text:

| | After the caller stops | Words |
|---|---|---|
| Whisper `base.en` | 0.69 to 0.86 s | all right |
| Whisper `tiny.en` | about 0.39 s | all right |
| Parakeet v2 (the default) | 0.25 to 0.45 s | all right, numbers as digits |

The same results on 8 kHz phone audio. Parakeet uses no CPU while the caller
talks (Moonshine does), but its download is bigger.

Choose it in Integrations → Listening → **Parakeet**, pick v2 (English) or v3
(25 European languages, detected per turn), and press **Download model**
(about 660 MB, once, into `VPE_MODEL_CACHE`).

### Qwen3-TTS: natural voices from your GPU server

[Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) sounds more natural than
Kokoro, but on a CPU it runs slower than real time, so it's offered as a
server: run it with [vLLM-Omni](https://docs.vllm.ai/projects/vllm-omni/) on
a machine with an NVIDIA GPU,

```bash
vllm serve Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice --omni --port 8091 --trust-remote-code
```

then choose Integrations → Speaking → **Qwen3-TTS** and set its address
(`VPE_QWEN3_TTS_URL`, like `http://gpu-box:8091/v1`), voice (`vivian`,
`serena`, `ryan`, `aiden`, ...) and language (`Auto`). The engine asks the
server to stream raw audio as it's generated, so the agent starts speaking
before the whole sentence is ready.

### Speech to speech (experimental)

A normal call is a chain: speech to text, the text model, then a voice. A
speech-to-speech model listens and answers with audio itself, so there's no
wait for a transcript or a voice, and it hears how things are said.

Turn it on in Integrations → Listening → **Speech to speech**
(`VPE_VOICE_MODE=speech-to-speech`). Voice calls then run on any server that
speaks the OpenAI Realtime protocol: OpenAI's `gpt-realtime` (your OpenAI key,
or `VPE_REALTIME_API_KEY`), or one you host (`VPE_REALTIME_URL`, like
`ws://gpu-box:8000/v1/realtime`). Steps, transfers and tools work as before:
each step's instructions and tools go to the model as the call moves on.

What's different, so it's off by default:

- Lines the agent must say word for word (Start's greeting, "say while
  running" lines) are spoken by the model, so the wording can differ slightly.
- Agents with scripted pages keep the normal chain; the call page says so.
- Collecting details still uses your text model, so keep its key set.
- The Test call page doesn't show response times for it yet.
- Calls cost what the realtime model costs, per minute of audio.

Text chats always use the text model.

## Users and roles

The owner key (`VPE_API_KEY`) can do everything. To share the engine, sign in
with it and open **Team** to make a key per person, with a role:

| Role | Can |
| --- | --- |
| admin | everything: settings, integrations, keys, deleting call history |
| builder | agents, data, knowledge, tests and triggers; not settings or keys |
| viewer | read everything and run test calls and chats; change nothing |

Keys start with `lpk_` and are shown once; only a SHA-256 fingerprint is kept
(`keys.json` next to the settings file, mode 600). Revoking one takes effect
at once. `GET /v1/me` says who a key is. By default, changes, records and
call history still have to come from this computer; behind an HTTPS proxy,
`VPE_REMOTE_EDITING=on` allows them through it, as each key's role allows.
Settings, secrets, downloads, sign-ins, imports, trying tools and running
triggers always stay local, and an agent saved through the proxy can't
change its tools, triggers or webhooks (they can use your secrets and
contact people). See [docs/HTTPS.md](../docs/HTTPS.md) for Caddy and nginx setups.

## Call history

Every finished call and chat is kept on the Calls page: when, which agent,
who it was with, how long, how it ended (or who it was transferred to), the
details it collected, the steps it went through, tool calls, response times
and the transcript. Search by number, detail or anything said; filter by
channel; delete a call when you're done with it.

It's a SQLite file next to records and knowledge (`VPE_DATA_DIR`, by default
`~/.local/share/vpengine/calls.sqlite3`), never sent anywhere. Calls older than
30 days are deleted (`VPE_CALL_HISTORY_DAYS`); `VPE_CALL_HISTORY=off` keeps
nothing. Recordings are off by default: with `VPE_CALL_RECORDINGS=on`, voice
calls are also saved as stereo WAV (caller left, agent right, 16 kHz) in
`recordings/`, and play on the Calls page. Tell callers if you record them;
the rules depend on where they are. Use synthetic test data unless the
machine is set up to protect real callers' details.

The API: `GET /v1/calls` (`agent`, `channel`, `q`, `before`, `limit`),
`GET /v1/calls/{id}`, `GET /v1/calls/{id}/recording`, `DELETE /v1/calls/{id}`.

## Turn taking and response time

On a voice call the agent has to decide when the caller has finished. By
default it uses Pipecat's **smart-turn** model, which runs on this computer and
listens to how the caller sounds (a trailing "and…" means more is coming), not
just to silence. It answers once the caller is done, or after the pause limit
(3 seconds) if they trail off. On lines where that doesn't suit, Integrations →
**Turn taking** switches to a fixed pause (0.8 seconds unless you change it).

The Test call page shows a **Response time** card. On a call it is how long
the caller waited after their last word for the agent's first sound, plus where
the time went: the turn decision, listening (time to the final transcript), the
model (time to its first words) and speaking (time to the first audio). In a
chat it is the model's time to its first words. The same numbers arrive as
`turn_metrics` events on `/v1/sessions/{id}/events`, so you can log them.

## Test agents on it

```bash
vk test agents/front-desk                   # VK_ENGINE_URL or http://localhost:7860
```

The folder runs as it is on disk: nothing to save or deploy first.

## API

| Method | Path | |
|---|---|---|
| GET | `/v1/health` | version, model, whether voice is on (no keys) |
| GET | `/v1/agents` | folders in the agents directory |
| GET / PUT | `/v1/agents/{folder}` | one agent with its prompts inline, plus lint findings; PUT saves it (this computer only) |
| POST | `/v1/agents/{folder}/check` | lint an unsaved draft |
| POST | `/v1/agents` | `{"name", "template"}`: create an agent folder (this computer only) |
| POST | `/v1/agents/generate` | `{"description", "name"}`: the model drafts a new agent from a description (this computer only) |
| POST | `/v1/agents/import?source=auto\|dfcx\|ces&name=&filename=` | body: the exported zip (up to 60 MB). Creates an agent and returns it with `import.notes` (this computer only) |
| GET | `/v1/agents/{folder}/export?target=ces\|dfcx` | the agent as a CX Agent Studio app or a Dialogflow CX agent (zip) |
| POST | `/v1/agents/{folder}/triggers/{id}/run` | `{"dry_run": true}`: run a schedule or after-call trigger now (this computer only) |
| GET | `/v1/automation` | channels and which agent answers each, outbound limits, recent activity |
| POST | `/v1/public/agent/{path}` | The API trigger: `{phone_number}` or `{channel, to, text}`, plus `initial_context`, `dry_run` (true/false). Send an `Idempotency-Key` header to retry safely: the same key gets the first answer for a day. 409: refused; 504: the phone provider didn't answer in time and the call may still ring (don't retry) |
| GET / POST | `/v1/whatsapp/webhook` | Meta's verification and messages (signed) |
| POST | `/v1/oauth/google/start` | starts **Connect Google account**; Google returns to `/v1/oauth/google/callback` |
| POST | `/v1/tools/mcp/list` | `{"mcp": {url, headers}}`: an MCP server's tools (this computer only) |
| POST | `/v1/settings/check/{service}` | test a provider or a connection (`telegram`, `whatsapp`, `email`, `google`) |
| GET | `/v1/templates` | templates a new agent can start from |
| GET | `/v1/secrets` | tool secret names and last four characters |
| PUT / DELETE | `/v1/secrets/{NAME}` | `{"value"}`: save or remove a tool secret (this computer only) |
| POST | `/v1/tools/try` | `{"tool", "arguments", "variables", "mock"}`: run a tool once (this computer only) |
| POST | `/v1/chat/sessions` | `{"agent": "front-desk"}` or `{"name", "definition", "context_variables", "tools"}`, plus optional `context` and `mock_tools` |
| POST | `/v1/chat/sessions/{id}/messages` | `{"text": "..."}`; returns when the agent has finished replying |
| POST | `/v1/chat/sessions/{id}/end` | |
| GET | `/v1/sessions/{id}` | steps visited, current step, collected values, events (the key that started it, or an admin) |
| GET | `/v1/sessions/{id}/events?token=` | the same events live (server-sent events); `token` is the `events_token` returned when the conversation started, so no key goes in the URL |
| GET / POST | `/v1/evals` | eval runs (newest first, `limit`, `before`); start one: `{caller_agent, method: google_voice\|speaker, number, persona, goal, criteria[], max_minutes}` ([docs/EVALS.md](../docs/EVALS.md)) |
| GET / DELETE | `/v1/evals/{id}` | one run: status, transcript, graded results (and `live` session while the call is on) |
| POST | `/v1/evals/{id}/offer`, `/v1/evals/{id}/stop` | the phone-on-speaker call's WebRTC offer; stop a run |
| GET / POST | `/v1/evals/cases` | saved test cases; save `{cases: [{name, persona, goal, criteria, number, notes}], source}` |
| POST | `/v1/evals/cases/convert` | `{text, filename}`: test cases read from a test script (Hammer, Cyara, CSV, notes) by your model; nothing is saved |
| DELETE | `/v1/evals/cases/{id}` | delete a test case |
| POST | `/v1/evals/bridge/offer` | the Lampline Phone Bridge extension's WebRTC offer, with its one-call token in `X-Lampline-Bridge` |
| POST | `/v1/tests/caller` | `{persona, goal, transcript}`: what a model-played test caller says next (`vk test`) |
| GET | `/v1/me` | who this key is, and its role |
| GET, POST, DELETE | `/v1/keys`, `/v1/keys/{id}` | named keys with roles (admin) |
| GET | `/v1/calls` | call history: `agent`, `channel`, `q`, `before`, `limit` |
| GET | `/v1/calls/{id}` | one call: transcript, events, collected details |
| GET | `/v1/calls/{id}/recording` | its recording (WAV), when recordings are on |
| DELETE | `/v1/calls/{id}` | delete a call and its recording |
| POST / PATCH | `/v1/calls/webrtc/offer` | browser call signalling |
| POST | `/v1/twilio/voice?agent=<folder>` | Twilio's incoming-call webhook (signed) |
| WebSocket | `/v1/twilio/stream` | Twilio media stream (one-time token) |
| POST | `/v1/twilio/after` | where Twilio goes when the stream closes: dials the transfer, or hangs up (signed) |
| POST | `/v1/telnyx/voice?agent=<folder>` | Telnyx TeXML voice URL (signed) |
| WebSocket | `/v1/telnyx/stream` | Telnyx media stream (one-time token) |
| POST | `/v1/jambonz/call?agent=<folder>` | jambonz calling webhook (signed); answers with a `listen` verb |
| POST | `/v1/jambonz/done` | jambonz listen action hook and call status webhook (signed) |
| WebSocket | `/v1/jambonz/stream` | jambonz audio stream (one-time token) |
| GET | `/v1/fish/voices` | your Fish Audio voices and popular English ones |

Events: `node_transition`, `bot_speech`, `user_speech`, `tool_call_started`,
`tool_call_result`, `variables`, `transfer`, `turn_metrics`, `notice`,
`execution_error`, `call_ended`.

## Develop

```bash
python3.12 -m venv .venv-engine && .venv-engine/bin/pip install -e cli -e "engine[dev,moonshine]"
.venv-engine/bin/pytest -q engine/tests
```

Tests run against a deterministic fake OpenAI-compatible model
(`tests/fakellm.py`) and fake speech services, including a real WebRTC
handshake between two aiortc peers and a simulated Twilio call. No keys or
network needed.

### The web app

The UI is a React app in `engine/ui` (Vite, Tailwind, Radix primitives in the
style of shadcn/ui, React Flow for the builder canvas). Its build is committed
to `engine/src/vpengine/static`, so running the engine needs no Node.

```bash
cd engine/ui && npm ci
npm run dev        # http://localhost:5173, talks to an engine on :7860
npm run build      # rebuilds src/vpengine/static and THIRD-PARTY-LICENSES.txt
```

CI rebuilds it and fails if the committed build doesn't match the source.
