---
name: voice-agent-builder
description: Use when asked to create a new voice agent, or to add sub-agents, steps, tools or webhooks to an existing one, for the self-hosted Lampline engine via the vk CLI.
---

# Build a voice agent with vk

You build agents as code: an agent is a folder (`agent.yaml` + `prompts/*.md`)
under `agents/`, edited locally, checked with `vk lint` and tested with
`vk test` on the Lampline engine. The engine runs the folder as it is on disk,
so changes reach real callers when the folder does: through a reviewed merge,
which is the reviewer's job and needs the user's explicit approval.

## 0. Check the engine

```bash
curl -fsS http://localhost:7860/v1/health     # or $VK_ENGINE_URL
```

If it isn't running, ask the user to start it (`scripts/docker-up.sh` or
`scripts/engine-up.sh`). If it needs a key, they set `VK_ENGINE_KEY`; never ask
them to paste a key into chat or a file in the repo.

## 1. Ask the setup questions

Ask these in one short message; offer the default for each so the user can
just say "defaults". Record the answers in `agent.yaml` under `setup:` with
these keys: `purpose`, `languages`, `llm`, `speech`, `real_data` (true/false),
`structure`, `systems`, `handoff`, `persona`, `examples`.

1. What should the agent do? Top 3 reasons people call.
2. Language: English (default) · Spanish · Bilingual EN/ES.
3. Model: **Claude** (default: `claude-haiku-5-5`, about a cent per call) · another provider from the engine's Integrations page (Gemini, OpenAI, Azure OpenAI, Bedrock, Groq, Mistral and more) · **Local via Ollama** (free, offline, weaker; only offer if `curl -s localhost:11434/api/tags` succeeds).
4. Speech: cloud (Deepgram, Fish Audio) (default) · on this computer (Whisper + Kokoro, free).
5. Will real patient/customer data touch it? **No, synthetic only** (default) · Yes.
   If yes: only BAA-covered vendors or local models, and tell the user the
   security checklist in SECURITY.md must pass before anything goes live.
6. Structure: root + sub-agents (default) · single open agent · guided steps.
7. Systems to call: none yet · pre-call lookup URL · in-call tools · post-call webhook URL.
8. Human handoff: none · phone number · another agent · contact-center queue.
9. Persona name, tone, greeting.
10. Two or three example calls (or "generate them").

## 2. Write the tests first

Turn the example calls from question 10 into `agents/<name>/tests/*.yaml`, one
conversation per file. They are what "done" means:

```yaml
name: caller moves an appointment
context: {first_name: Sam}            # optional test values for {{variables}}
turns:
  - user: I need to move my appointment to Tuesday.
    expect: {reaches: Appointments}   # optional check after this turn
  - user: Morning, please. That's everything.
expect:
  reaches: [Router, Appointments, Goodbye]   # step names (or ids), in order
  not_reaches: [Billing]
  calls_tool: [find_slots]            # real tools, or transition tool names
  says: [call back]                   # case-insensitive text the agent must say
  not_says: [account number]
  ends: true
```

Test behaviour (where the call goes, what must or must never be said), not
exact wording: the LLM phrases things differently each run. Synthetic data only.

## 3. Write the folder

Start from a template (`vk templates list`, then
`vk templates install front-desk --dir agents/<name> --name "<Agent Name>"`),
from an export (`vk import dfcx|ces <export.zip>`), or create
`agents/<name>/agent.yaml` by hand in the vk format:

```yaml
vk_format: 1
name: Front Desk
context_variables: {}        # test values for {{variables}} used in prompts
setup: {...answers from step 1...}
lint: {ignore: []}
nodes:
  - id: "1"
    type: startCall          # exactly one
    position: {x: 0, y: 0}
    data: {name: Greeting, prompt: {file: prompts/greeting.md}, greeting: "Hi, thanks for calling."}
  - id: "2"
    type: agentNode          # one per sub-agent / step
    position: {x: 300, y: 0}
    data: {name: Router, prompt: {file: prompts/router.md}}
  - id: "9"
    type: endCall
    position: {x: 600, y: 0}
    data: {name: Goodbye, prompt: {file: prompts/goodbye.md}}
edges:
  - id: "1-2"
    source: "1"
    target: "2"
    data: {label: greeted, condition: "The caller has been greeted."}
```

Rules that matter:
- **Root + sub-agents** = hub and spoke: a Router `agentNode` with a transition
  to each specialist, and a transition from each specialist back to the Router.
- A transition's `condition` is plain English the LLM uses to decide when to
  move; make each one specific and different from its siblings.
- Shared persona/rules go in one `globalNode`; set `add_global_prompt: true`
  on nodes that should inherit it.
- Keep each prompt focused on its one job. Use `{{variable}}` only for values
  in `context_variables` or extracted by an earlier node.
- **Never** put real names, phone numbers, dates of birth, member IDs or other
  patient/customer data in prompts or tests. Use obviously fake values.

## 4. Check and run the tests

```bash
vk lint agents/<name>        # fix every error; explain any warning you keep
vk test agents/<name>        # runs tests/*.yaml on the engine via text chat
```

When a test fails, read its transcript, fix the prompt or transition
condition that caused it (not the test, unless the test was wrong), then
`vk test agents/<name>` again. Stop after three rounds on the same
failure and explain it to the user. Each test costs a few LLM calls.

Then tell the user what you built (nodes, transitions, tools), test results,
what is still a placeholder, and that the reviewer skill (or they) must approve
it before it's merged toward production.

## Don'ts

- Don't merge, promote or deploy a change unless the user explicitly asks.
- Don't edit an agent in the engine's builder and in the folder at the same
  time; the builder saves to the same files.
