<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/lampline-logo-dark.png">
    <img src="docs/brand/lampline-logo-light.png" alt="Lampline" width="280">
  </picture>
</p>

<h3 align="center">Open-source, self-hosted voice agents.</h3>

<p align="center">
  Bring your agents over from <b>Dialogflow CX</b> and <b>CX Agent Studio</b>, or start fresh.<br>
  Build, test and run them on your own machine or servers, in one command,<br>
  for about $0.02–0.17 a call instead of $0.50.
</p>

<p align="center">
  <a href="https://github.com/C4rohan/lampline/actions/workflows/ci.yml"><img src="https://github.com/C4rohan/lampline/actions/workflows/ci.yml/badge.svg?branch=dev" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0"></a>
  <img src="https://img.shields.io/badge/imports-Dialogflow_CX_%C2%B7_CX_Agent_Studio-2f6fb0" alt="Imports Dialogflow CX and CX Agent Studio">
  <img src="https://img.shields.io/badge/models-20_providers-f2b84b" alt="20 model providers">
  <img src="https://img.shields.io/badge/runs-local_%26_on--prem-15212b" alt="Runs local and on-prem">
  <a href="https://codespaces.new/C4rohan/lampline"><img src="https://github.com/codespaces/badge.svg" alt="Open in GitHub Codespaces" height="20"></a>
</p>

<p align="center">
  <a href="#-quick-start"><b>🚀 Quick start</b></a> &nbsp;·&nbsp;
  <a href="#-test-any-phone-agent-for-free">📋 Free phone evals</a> &nbsp;·&nbsp;
  <a href="#-use-it-from-claude-code-cursor-or-codex">🤖 From Claude Code</a> &nbsp;·&nbsp;
  <a href="#-bring-your-dialogflow-cx-and-cx-agent-studio-agents">🔁 Import from Google</a> &nbsp;·&nbsp;
  <a href="#-a-quick-tour">👀 Tour</a> &nbsp;·&nbsp;
  <a href="#-models">🧠 Models</a> &nbsp;·&nbsp;
  <a href="#-phone-calls-and-sip">📞 Phone and SIP</a> &nbsp;·&nbsp;
  <a href="docs/ROADMAP.md">🗺️ Roadmap</a> &nbsp;·&nbsp;
  <a href="engine/README.md">📘 Engine docs</a> &nbsp;·&nbsp;
  <a href="cli/README.md">🧩 <code>vk</code> CLI</a>
</p>

<br>

<p align="center">
  <img src="docs/images/hero.gif" width="900" alt="30 seconds: import a Dialogflow CX export, open it in the builder, chat with it while its steps light up, then a real phone call to it played by the Test Caller and graded Passed">
  <br><sub>Import a Dialogflow CX agent, test it, and grade a real phone call to it. Sample agent and made-up data; replies from Lampline's test model.</sub>
</p>

Lampline imports your Dialogflow CX and CX Agent Studio agents, flows, pages,
playbooks, forms and tools included, and runs them on hardware you control. It gives
you a drag-and-drop builder where LLM steps and scripted pages mix in one agent, a test console that lights up each step as the call moves, HTTP and
MCP tools, records and document search of its own, and phone calls over Twilio,
Telnyx or any SIP trunk through jambonz. Built on
[Pipecat](https://github.com/pipecat-ai/pipecat).

<table>
  <tr>
    <td width="50%" valign="top"><b>🏠 Local first</b><br>One command on a laptop, a GPU box or an on-prem server. No cloud account needed.</td>
    <td width="50%" valign="top"><b>🔒 Private by default</b><br>No telemetry, bound to localhost, a key on every request, and keys never in your agents or the repo.</td>
  </tr>
  <tr>
    <td valign="top"><b>🧠 Any model</b><br>Claude, OpenAI, Gemini, Vertex AI, Azure OpenAI, Bedrock, Groq, Mistral, DeepSeek, Grok and more, or Ollama on your own hardware.</td>
    <td valign="top"><b>🧩 Agents as code</b><br>Every agent is a folder of YAML and Markdown: reviewed in git, tested with <code>vk</code>, exported back to Google's tools whenever you like.</td>
  </tr>
</table>

## 📋 Test any phone agent for free

Lampline's **Evals** tab phones a voice agent over a real phone line, plays the
caller with an AI **Test Caller**, and grades the call against what should
happen. It works on any agent with a phone number: one built in Lampline,
Dialogflow CX, CX Agent Studio, Vapi, Retell, or your IVR.

<table>
  <tr>
    <td width="52%" valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/evals-dark.webp">
  <img src="docs/images/evals-light.webp" alt="A graded eval: the call's transcript, the agent's response time as the caller heard it, and each check passed">
</picture>
    </td>
    <td valign="top">

- **Free calls through Google Voice.** The Lampline Phone Bridge extension (Chrome or Edge) connects the call's audio to the Test Caller inside the browser: no cables, Windows or Mac. Or use any phone on speaker.
- **Graded, with reasons.** You write who is calling, what they want and what must happen; your model grades the transcript, check by check. A call that heard nothing shows *Didn't run*, never *Failed*.
- **Tough callers.** Background noise, accents, interruptions, long pauses, hesitant or rambling speech.
- **The agent's speed, as callers hear it.** Typical and p95 response time, phone line included.
- **Bring your test scripts.** Paste Hammer, Cyara or spreadsheet tests and they become runnable cases. Replay any failed call.
- **Improve an agent from its failures.** Lampline runs your test cases as text chats, rewrites the prompts of the steps where checks failed, and keeps a change only if more checks pass and nothing that passed breaks. You review the before and after, then apply.

[How to run evals →](docs/EVALS.md)
    </td>
  </tr>
</table>

Only call agents you own or are allowed to test. You press Call yourself:
Google Voice doesn't allow automated dialing.

## 🤖 Use it from Claude Code, Cursor or Codex

Lampline agents are plain files (`agent.yaml` and `prompts/*.md`), and `vk`
lints and tests them from the terminal, so a coding agent can build, test and
review a phone agent for you. Four skills teach it how:

- **voice-agent-builder:** asks what the agent should do, writes the tests
  first, builds the folder, and runs `vk lint` and `vk test` until they pass
- **voice-agent-fixer:** makes failing tests pass by changing only the
  prompts or routes behind them, and keeps a change only if more tests pass
  and none breaks
- **dialogflow-tester:** writes tests for an existing Dialogflow CX or CX
  Agent Studio agent and runs them against it live, or side by side with its
  Lampline copy
- **voice-agent-reviewer:** checks a change before it goes live: lint, the
  diff, the tests, and a privacy review, then asks you to approve

**Claude Code**, from the plugin marketplace in this repo:

```
/plugin marketplace add C4rohan/lampline
/plugin install lampline@lampline
```

On Claude Code 2.1.275 or later, one line does both:
`/plugin install lampline --marketplace C4rohan/lampline`.

**Cursor, Codex and other agents** read [`AGENTS.md`](AGENTS.md). To copy the
skills into a project instead, run `vk skills install --target claude`
(or `--target agents`, `--target gemini`).

Then ask, for example: *"Build a phone agent for a dental office that books
cleanings, with tests"*, *"Make the failing tests in agents/front-desk pass"*,
*"Write regression tests for my Dialogflow CX agent"*, or *"Review my change
to agents/front-desk before it goes live"*.

## 🔁 Bring your Dialogflow CX and CX Agent Studio agents

Export an agent from Google's console, drop the .zip on **New agent → Import**, and
it opens in the builder: flows, pages, forms, routes, playbooks and tools carried
over, with a report of anything to check before it takes calls. It works the
other way too: **Export** writes any agent back as a Dialogflow CX package or a
CX Agent Studio app.

<table>
  <tr>
    <td width="50%" valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/import-done-dark.webp">
  <img src="docs/images/import-done-light.webp" alt="Importing a Dialogflow CX export: what came over, and what to review">
</picture>
      <p><b>📥 Import.</b> A Dialogflow CX package or a CX Agent Studio app (JSON or YAML). The conversion runs on your computer and never runs code from the export.</p>
    </td>
    <td width="50%" valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/import-builder-dark.webp">
  <img src="docs/images/import-builder-light.webp" alt="The imported agent in the builder: flows, scripted pages and a playbook">
</picture>
      <p><b>✏️ Ready to edit.</b> Pages become scripted pages, playbooks become LLM steps, flows group them, and webhooks and OpenAPI tools become tools.</p>
    </td>
  </tr>
</table>

| From Dialogflow CX | Becomes |
|---|---|
| Flows and pages | Flows of [scripted pages](engine/README.md#scripted-pages-and-flows): entry messages said word for word |
| Form parameters, entity types | Form fields with their types, choices and synonyms, patterns and keypad input |
| Intent and condition routes, no-match handlers | Transfers the model decides, rules, and "no match" transfers |
| Playbooks | LLM steps with their goal, guidelines and steps |
| Webhooks, OpenAPI tools, data store tools | Tools (webhooks answer with a mock until connected) and local knowledge search |

| From CX Agent Studio | Becomes |
|---|---|
| Root agent and child agents | A root agent with sub-agents and transfers both ways |
| Global instruction, variables | Global instructions and context variables |
| OpenAPI toolsets, Python tools, data stores | HTTP tools, tools with example answers, and local knowledge search |

From the terminal: `vk import auto export.zip` and `vk export dfcx|ces agents/<name>`.
The full mapping, both ways, and what doesn't carry over yet:
[cli/README.md](cli/README.md#import).

## 👀 A quick tour

<p align="center">
  <img src="docs/images/demo.gif" alt="A 25-second tour: the agent list, the builder with a transfer to a person, speech options in Integrations, and the Calls page with a call's details" width="880">
  <br><sub>Sample data only.</sub>
</p>

<table>
  <tr>
    <td width="50%" valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/test-dark.webp">
  <img src="docs/images/test-light.webp" alt="Test call: a chat with a scripted agent, the step it's on, and the details it collected">
</picture>
      <p><b>🧪 Test calls.</b> Chat or talk through your microphone. The steps light up as the call moves, and every collected detail shows on the right.</p>
    </td>
    <td width="50%" valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/integrations-dark.webp">
  <img src="docs/images/integrations-light.webp" alt="Integrations: model providers as tiles, with the one in use highlighted">
</picture>
      <p><b>🧠 20 model providers.</b> Pick one, paste a key, press Test connection. Speech runs in the cloud or free on your own computer.</p>
    </td>
  </tr>
  <tr>
    <td valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/connections-dark.webp">
  <img src="docs/images/connections-light.webp" alt="Phone numbers and SIP, and contact centers marked coming soon">
</picture>
      <p><b>📞 Phone calls and SIP.</b> Twilio, Telnyx, or your own SIP trunk, PBX or SBC through jambonz. Genesys, Avaya, Cisco and more are coming.</p>
    </td>
    <td valign="top">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/data-dark.webp">
  <img src="docs/images/data-light.webp" alt="Data: an appointments collection with sample records">
</picture>
      <p><b>🗄️ Data of its own.</b> Collections agents look up and save during calls, and document search over your FAQs, all on your machine.</p>
    </td>
  </tr>
</table>

<sub>Screenshots use the bundled sample agents and made-up data.</sub>

## 🏠 Where it runs

| | Status | |
|---|---|---|
| **Local and on-prem** | ✅ Available | One Docker container, or the engine natively, on any machine you control |
| **Google Cloud** | 🔜 Coming soon | A one-command deployment to your own Google Cloud project |

## 🚀 Quick start

Needs Docker with Compose v2 (Docker Desktop is fine). **No API keys or accounts:**

```bash
git clone https://github.com/C4rohan/lampline && cd lampline
scripts/docker-up.sh --free          # everything on this computer: Ollama, Moonshine and Kokoro
```

`--free` runs the model (Qwen3 4B in Ollama) and speech (Moonshine listening,
Kokoro speaking) on your own computer. The first start downloads about 3 GB and
shows progress; after that it starts in seconds. Replies are slower and simpler
than a cloud model's, and you can switch to any provider on the Integrations
page whenever you like. On a Mac, the native route is faster, because Ollama
uses the GPU outside Docker: install [Ollama](https://ollama.com), then
`scripts/engine-up.sh --free`.

Have API keys? Run `scripts/docker-up.sh` without `--free`. Either way it prints
a link like `http://localhost:7860/#/?key=…`: open it once and the page
remembers the key. Pick your model and voice on the Integrations page (or put keys like `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPGRAM_API_KEY` in
`.env`; see `.env.example`). Your agents are in `./agents` (edit them, keep them
in git) and everything else the engine keeps, settings with your keys, records,
knowledge and downloaded models, is in `./.vpe-data`.
`scripts/docker-up.sh --down` stops it; `--logs` follows its log. Plain
`docker compose up -d --build` works too once `.env` has `VPE_DOCKER_API_KEY`.
Details: [engine/README.md](engine/README.md#docker).

Prefer no Docker? `scripts/engine-up.sh` runs the engine natively (Python 3.11+)
at http://localhost:7860, which also lets browser calls reach it directly.
Free, private speech: Integrations → Whisper and Kokoro → **Download models**,
or Moonshine for listening, which transcribes while the caller talks.

## 🧠 Models

Pick one on the Integrations page; each starts on a sensible default model you
can change, and **Test connection** checks the key.

| Provider | Key |
|---|---|
| Claude (Anthropic) | `ANTHROPIC_API_KEY` |
| OpenAI, or any OpenAI-compatible endpoint | `OPENAI_API_KEY`, `VPE_LLM_BASE_URL` |
| Gemini | `GEMINI_API_KEY` |
| Gemini on Vertex AI | `GOOGLE_CLOUD_PROJECT` plus a service account key or this computer's Google credentials |
| Azure OpenAI | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` |
| Amazon Bedrock | AWS keys, or this computer's AWS credentials |
| Groq, Mistral, DeepSeek, Grok (xAI), Together AI, Fireworks AI, OpenRouter, Cerebras, NVIDIA NIM, Qwen, SambaNova, Nebius, Novita AI | each provider's API key |
| Ollama | none: open models on this computer or your own server |

## 📞 Phone calls and SIP

| | |
|---|---|
| Twilio | a Twilio number rings your agent; outbound calls from triggers |
| Telnyx | the same, through a TeXML application |
| SIP through [jambonz](https://jambonz.org) | any SIP trunk, PBX or session border controller: point a jambonz application at the engine |

The Phone numbers page has the webhook to copy and the steps for each.
Details: [engine/README.md](engine/README.md#phone-calls-twilio-telnyx-or-sip).

### 🏢 Contact centers (coming soon)

Genesys Cloud, Avaya, Cisco Webex Contact Center, Cisco UCCE and PCCE, Amazon
Connect, NICE CXone, Five9 and Twilio Flex show on the Integrations page as
coming soon. Until each lands, most of them can already send calls to your
agents over a SIP trunk through jambonz. The plan for each:
[docs/ROADMAP.md](docs/ROADMAP.md#contact-centers-coming-soon).

## ⚙️ How it works

```mermaid
flowchart LR
  you((You)) --> app[Lampline app<br/>builder, tests, integrations :7860]
  app --> engine[Engine<br/>Pipecat pipelines]
  phone((Callers)) --> carriers[Twilio · Telnyx ·<br/>SIP via jambonz]
  carriers --> engine
  engine --> agents[(agents/<br/>YAML + Markdown)]
  engine --> data[(Records and<br/>knowledge, SQLite)]
  engine --> models{{Model and speech<br/>cloud APIs or local<br/>Whisper · Moonshine · Parakeet · Kokoro · Qwen3-TTS · Ollama}}
  engine -- tools --> hooks[Your APIs and<br/>MCP servers]
```

The engine reads the agent folders on every call, so a change is live on the
next call. Settings and keys live in a `chmod 600` file outside the repo.

## 💰 What a voice-agent call actually costs

Monthly totals for 4-minute calls at list prices (October 2026). Staff time is
not included. Reproduce or change any assumption with `python3 costs/cost_model.py`.

| Deployment | 10k calls/mo | 50k calls/mo | 200k calls/mo |
|---|---|---|---|
| CX Agent Studio (list price, baseline) | $5,000 | $25,000 | $100,000 |
| On-prem server (no GPU) + Google model APIs | $1,384 | $6,235 | $24,426 |
| On-prem GPU servers + self-hosted models | $681 | $1,363 | $3,748 |
| Cloud Run + Google model APIs (coming soon) | $1,423 | $6,356 | $24,908 |
| GKE production + mixed vendors: Deepgram, Claude Haiku, Fish Audio (coming soon) | $1,105 | $3,612 | $13,037 |
| GKE production + self-hosted models on L4 GPUs, 3-yr CUD (coming soon) | $1,054 | $1,675 | $3,888 |

What the numbers say:
- 📉 **Hosting is the small part.** Model APIs are almost the whole bill, so the model choice matters far more than local vs. cloud.
- 🖥️ **Self-hosted models are where the big savings are,** but they rest on two assumptions nobody has published good data for: concurrent calls per GPU, and whether an 8B open model is good enough for your flows. [Help measure them](CONTRIBUTING.md).

Every row, per-call figures and assumptions: [costs/COSTS.md](costs/COSTS.md).

## 🧩 Agents as code (`vk`)

Build and change agents from a terminal or an AI coding agent as well as the
builder. Each agent is a folder: `agent.yaml` for the graph and one Markdown
file per prompt, so changes are reviewable in git.

```bash
pip install -e cli                  # installs the `vk` command
vk templates install front-desk     # start from a ready-made agent
vk lint agents/front-desk           # graph checks + a scan for real-looking patient data
vk test agents/front-desk           # runs tests/*.yaml conversations on the engine
vk import dfcx export.zip           # bring an agent over from Dialogflow CX or CX Agent Studio
vk skills install --target claude   # the four coding-agent skills (or install the Claude Code plugin, above)
```

Details in [cli/README.md](cli/README.md). The engine itself, its builder,
tools, data and knowledge: [engine/README.md](engine/README.md).

## 🗺️ Roadmap

- [x] The engine on Pipecat: builder, text chat, browser calls, phone calls ([engine/README.md](engine/README.md))
- [x] Agents as code: `vk` lint, conversation tests, templates, coding-agent skills ([cli/README.md](cli/README.md))
- [x] Scripted pages and flows mixed with LLM steps ([engine/README.md](engine/README.md#scripted-pages-and-flows))
- [x] Dialogflow CX and CX Agent Studio import and export ([cli/README.md](cli/README.md#import))
- [x] Records and local RAG of its own ([engine/README.md](engine/README.md#data-and-knowledge))
- [x] One Docker container for local and on-prem
- [x] 20 model providers; SIP through jambonz
- [x] Transfer to a person with the call's details as SIP headers ([engine/README.md](engine/README.md#transfer-to-a-person))
- [ ] **Google Cloud deployment** (coming soon)
- [ ] **Contact centers:** Genesys Cloud, Avaya, Cisco Webex Contact Center, Cisco UCCE, Amazon Connect, NICE CXone, Five9, Twilio Flex (coming soon)
- [x] Users and roles, and an HTTPS guide ([docs/HTTPS.md](docs/HTTPS.md))
- [x] Test a phone agent for free from your laptop ([docs/PHONE-TESTING.md](docs/PHONE-TESTING.md))
- [x] Evals: real phone calls through Google Voice, played by a Test Caller and graded ([docs/EVALS.md](docs/EVALS.md))
- [x] Import Hammer, Cyara or spreadsheet test scripts as eval test cases ([docs/EVALS.md](docs/EVALS.md#import-test-scripts))
- [x] Improve an agent from its failing test cases ([docs/EVALS.md](docs/EVALS.md#improve-an-agent))
- [ ] Evals placed by Lampline through Twilio, for unattended runs
- [x] Call history and optional recordings, on this computer ([engine/README.md](engine/README.md#call-history))
- [ ] Published benchmarks

The full list: [docs/ROADMAP.md](docs/ROADMAP.md).

## ✅ Tested

CI runs on every push and pull request to `dev`, `qa` and `prod`:
- the `vk` CLI test suite
- the engine test suite: flows, extraction, text chat, evals, every model provider's setup, Twilio, Telnyx and jambonz calls, the voice pipeline and a real WebRTC handshake, on fake models
- the web app builds, and the committed build matches its source
- the Docker image builds, starts as a non-root user, answers health and refuses requests without its key
- shellcheck, and a check that `COSTS.md` matches the cost model
- a scan of every commit for keys and tokens (gitleaks)

Issues and PRs welcome (against `dev`; [how dev, qa and prod work](docs/BRANCHES.md)). See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), the [code of conduct](CODE_OF_CONDUCT.md) and the [changelog](CHANGELOG.md).

## 🙌 Credit and citation

Lampline is created and maintained by **Rohan Salvi**. If you use it in a
project, product, paper or talk, please credit "Lampline by Rohan Salvi" and
link this repository. GitHub's **Cite this repository** button (from
[CITATION.cff](CITATION.cff)) gives APA and BibTeX:

```bibtex
@software{salvi_lampline_2026,
  author  = {Salvi, Rohan},
  title   = {Lampline: open-source, self-hosted voice agents},
  year    = {2026},
  version = {0.3.0},
  url     = {https://github.com/C4rohan/lampline}
}
```

If you redistribute Lampline or build on it, the Apache-2.0 licence requires
you to keep the [NOTICE](NOTICE) file, which carries this attribution.

## 📄 License

Apache-2.0. The engine uses Pipecat (BSD-2-Clause), installed from PyPI;
other credits are in [NOTICE](NOTICE). Not affiliated with Google or any
provider named here. Prices are list-price estimates that change often; check
them before budgeting.
