# vk: agents as code for Lampline

`vk` lets you build, check and test Lampline voice agents from a terminal, a CI
job, or an AI coding agent (Claude Code, Gemini CLI, Codex), alongside the
engine's drag-and-drop builder. Agents are folders the engine runs as they are
on disk, so there's nothing to upload: edit, lint, test, commit.

```bash
pip install -e cli            # from the repo root; Python 3.10+
```

`vk test` talks to a running engine: `VK_ENGINE_URL` (default
`http://localhost:7860`) and, when the engine needs one, its API key in
`VK_ENGINE_KEY`. The key is never written into an agent folder.

## An agent is a folder

```
agents/front-desk/
  agent.yaml              # graph: nodes, edges, settings, context variables
  prompts/greeting.md     # one Markdown file per prompt
  prompts/router.md
  prompts/reschedule.extraction.md
  tests/                  # optional; scanned for patient data by vk lint
```

`agent.yaml` points at prompt files (`prompt: {file: prompts/router.md}`), so a
prompt change shows up in git as a Markdown diff. Folder ↔ JSON conversion
is lossless, including fields vk doesn't know about.

Two sections are notes for you and your tools, not part of the agent itself:
- `setup`: answers to the new-agent questions (purpose, LLM cloud vs local,
  whether real patient data is involved, and so on).
- `lint`: `prompt_budget_chars` and `ignore` (a code like `VK105`, or
  `VK105:<node-id>` for one node).

## Commands

| Command | What it does |
|---|---|
| `vk lint DIR... [--format json] [--strict]` | Offline checks; exit 1 on errors (`--strict`: on warnings too) |
| `vk test DIR [-k TEXT] [--url URL] [--format json] [-v] [--live-tools] [--repeat N] [--allow-flaky]` | Run `tests/*.yaml` conversations on the engine, with the folder as it is on disk |
| `vk test DIR --target dfcx\|ces\|engine,dfcx --agent RESOURCE` | Run the same tests against your live Dialogflow CX agent or CX Agent Studio app, or side by side with the engine |
| `vk templates list` / `vk templates install NAME [--dir D] [--name N]` | Starter agents |
| `vk import dfcx\|ces\|auto EXPORT [--dir D] [--name N]` | Turn a Dialogflow CX or CX Agent Studio export (zip or folder) into an agent folder, offline. See [Import](#import) |
| `vk export ces\|dfcx DIR [--out F] [--folder]` | Write an agent folder as a CX Agent Studio app or a Dialogflow CX agent, offline. See [Export](#export) |
| `vk skills install --target claude\|agents\|gemini` | Install the coding-agent skills (builder, fixer, Dialogflow tester, reviewer) |
| `vk mcp [--dir agents] [--url URL]` | Serve the agent folders and their tests to MCP clients over stdio ([MCP server](#mcp-server)) |

Changes reach real callers when the folder does: review them as pull requests
and promote dev → qa → prod ([docs/BRANCHES.md](../docs/BRANCHES.md)), with
`vk lint` and `vk test` in CI.

Exit codes: 0 ok, 1 failure (including lint errors), 2 bad usage.

## Tests

A test is a scripted conversation in `tests/*.yaml`. `vk test` runs each one
through the engine's text chat, with the folder as it is on disk and the model
set on the engine's Integrations page, so you're testing exactly what a call
would do.

```yaml
name: caller moves an appointment
context: {first_name: Sam}            # optional values for {{variables}}
turns:
  - user: I need to move my appointment to Tuesday.
    expect: {reaches: Appointments}   # optional check after this turn
  - user: Morning, please. That's everything.
expect:
  reaches: [Router, Appointments, Goodbye]   # step names or ids, in order
  not_reaches: [Billing]
  calls_tool: [find_slots]            # a tool, or a transition's tool name
  says: [call back]                   # case-insensitive
  not_says: [account number]
  ends: true                          # reached an end step
```

```
$ vk test agents/front-desk
PASS  caller moves an appointment  (3 turn(s), 4.1s)  Greeting → Router → Appointments → Goodbye
FAIL  caller asks about Saturday hours  (1 turn(s), 1.9s)  Greeting → Router
      - expected to reach 'Hours and location'; steps were Greeting → Router
          caller: What time are you open on Saturday?
           agent: ...
```

### Let a model play the caller

For conversations you don't want to script line by line, describe the caller
and let a model play them. The engine's own model plays the caller, so `vk`
needs no key of its own:

```yaml
name: impatient caller with a billing question
caller:
  persona: Sam, in a hurry, confused by a late fee on this month's bill
  goal: get the fee explained or talk to someone in billing
  opening: Hi, I've got a question about my bill.   # optional first line
  max_turns: 8                                      # optional, 1 to 30 (default 8)
expect:
  reaches: Billing
  not_says: [card number, social security]
```

The caller answers the agent's questions with made-up details and stops when
its goal is met, the agent says goodbye, or `max_turns` runs out. It says
something different every run, so keep `expect` to what any good conversation
must do, and run it a few times after changing the agent (`--repeat`, below).
A file has either `turns` or a `caller`, not both.

### Catch flaky tests

Models answer differently from run to run, so one passing run proves little.
`--repeat N` runs every test N times (up to 10). For example:

```
$ vk test agents/front-desk --repeat 3
PASS  billing question goes to the billing team  (2 turn(s), 2.4s, passed 3 of 3 runs)  Greeting → Router → Billing
FLAKY  caller asks about Saturday hours  (1 turn(s), 2.5s, passed 2 of 3 runs)  Greeting → Router
      - expected to reach 'Hours and location'; steps were Greeting → Router

1 passed, 0 failed, 1 flaky
```

A flaky test shows its failing run, which usually points at two transition
conditions that overlap. It fails the run like a failed test does; add
`--allow-flaky` to let it pass. With `--format json`, each test has `runs`,
`passes` and `flaky`.

### Test the original Google agent, side by side

The same test files run against a live Dialogflow CX agent or CX Agent Studio
app, so a folder you imported can be checked against the original:

```bash
gcloud auth login                       # your own Google account; vk uses `gcloud auth print-access-token`
vk test agents/clinic --target dfcx --agent projects/my-project/locations/global/agents/AGENT_ID
vk test agents/clinic --target ces  --agent projects/my-project/locations/us/apps/APP_ID --deployment DEPLOYMENT_ID
vk test agents/clinic --target engine,dfcx --agent projects/...      # both, then a side-by-side table
```

```
Side by side
  test                         engine  dfcx
  caller moves an appointment  PASS    PASS
  caller asks for billing      PASS    FAIL    ← differs
```

- **What's checked:** for Dialogflow CX, `reaches` matches the pages, flows and
  playbooks the agent went through; for CX Agent Studio, the agents it
  transferred to. Imported folders use the same names, so one test file fits
  both. `calls_tool` matches tool display names, `says` the agent's text, and
  `ends` an end of session, a live-agent handoff or a phone transfer.
- **Context:** `context:` values go in as session parameters (Dialogflow CX)
  or variables (CX Agent Studio) on the first turn.
- **Model callers:** tests with a `caller:` block need the Lampline engine
  too (`--url`), because its model plays the caller.
- **Options:** `--environment` (Dialogflow CX, default the draft),
  `--deployment` (CX Agent Studio), `--language` (default `en`).
- **Your own agents only.** The access token is yours (`gcloud`, or
  `VK_GOOGLE_TOKEN`), used for these requests and never saved. Tests talk to
  the real agent, so its webhooks and tools run for real: use a test project
  and synthetic data, and never a production agent you aren't responsible for.
- Text only for now; voice (calling the agent's phone number) is next.

- Moving between steps is reported as a call to the transition's tool, named
  from its label (`hours or location` → `hours_or_location`).
- LLMs phrase things differently every run: assert on where the call goes and on
  must/must-not phrases, not exact sentences. Each test costs a few LLM calls.
- `vk lint` checks tests offline too: VK110 flags a test that names a step the
  agent doesn't have, or a malformed test file.
- HTTP tools answer with their mock responses, so tests never
  touch real services. `--live-tools` calls the real ones. Check a tool was used
  with `calls_tool: [find_open_slots]`.

## Templates

| Template | What it is |
|---|---|
| `front-desk` | Root agent routing to appointments, hours and location, billing, or a person |
| `appointment-scheduling` | Guided steps: collect details, read back, confirm |
| `bilingual-front-desk` | English and Spanish front desk that answers in the caller's language |
| `appointment-finder` | Root agent with an HTTP tool that looks up open times (mock answers until you add a URL) |
| `nurse-callback` | A scripted page that takes a callback request word for word: exact lines, a form, rule transfers, and no model calls at all |
| `test-caller` | An AI caller with a persona and a goal, for testing another phone agent from your laptop ([docs/PHONE-TESTING.md](../docs/PHONE-TESTING.md)); its Start waits for the other side to speak first |

Each comes with prompts, tests, a fictional business (Riverside Clinic) in
`context_variables`, and `setup.real_data: false`. Edit the context variables
and prompts, then `vk lint` and `vk test`.

## Import

Bring an agent over from Google's tools. Export it there, then:

```bash
vk import dfcx  ~/Downloads/front-desk.zip           # Dialogflow CX: Agent settings → Export, JSON package
vk import ces   ~/Downloads/front-desk.zip           # CX Agent Studio: Export app (JSON or YAML)
vk import auto  ~/Downloads/front-desk.zip --dir agents/front-desk
```

The builder's **New agent → Import** tab does the same. The conversion runs on
your computer, reads only the export, and never runs code from it.
`import-report.md` in the new folder lists everything to review.

| Dialogflow CX | Becomes |
|---|---|
| start flow | Start (greeting from the welcome route), then a "Main menu" step with the flow's routes |
| flows | a flow name on their steps; a start page that only passes the caller on is skipped |
| pages | [scripted pages](../engine/README.md#scripted-pages-and-flows): the entry message is said word for word; an entry webhook runs on entry (`on_entry`), a webhook on the form-complete route once the form is complete (`on_done`) |
| form parameters | form fields (`@sys.number` → number, `@sys.date` → date, custom entities → choices with synonyms, regexp entities → patterns, DTMF → keypad); reprompts carry over |
| intent routes | transfers the model decides, from the intent's description or up to four English training phrases |
| condition routes | rules (`$session.params.age >= 18` → `age >= 18`); `$page.params.status = "FINAL"` → otherwise; expressions with functions become transfers for the model (review these) |
| `sys.no-match-N` handlers | a "no match" transfer after N tries; the default handler's message is what the page says when it doesn't understand |
| route groups | their routes are added to every flow or page that uses them |
| route fulfillment | what the agent says while transferring |
| End Session / End Flow | an End call step (from a page: "Hang up", which ends without another word); End Flow With Human Escalation → "Hand off to a person" |
| playbooks | steps with the goal, guidelines and steps; referenced playbooks and flows become transfers |
| OpenAPI tools | HTTP tools, one per operation |
| webhooks | tools with a mock answer (Dialogflow's webhook format differs from ours) |
| data store tools | `knowledge.search` tools on a local knowledge base of the same name (upload the documents on the Knowledge page) |

| CX Agent Studio | Becomes |
|---|---|
| root agent | Start (no fixed greeting: the root agent opens, as in CX Agent Studio), then the root agent |
| agents, `childAgents` | steps, with a transfer to each child (its description says when) and back |
| `end_session` tool | a transfer to an End call step |
| global instruction, variables | global instructions, context variables with their defaults |
| `{@TOOL: x}`, `{@AGENT: y}`, `{var}` | plain names and `{{var}}` |
| OpenAPI tools and toolsets | HTTP tools (a toolset's chosen operations only) |
| Python and client-function tools | tools with a mock answer until you put them behind an HTTP endpoint |
| data store and file search tools | `knowledge.search` tools on a local knowledge base of the same name |

Not carried over yet, and listed in the report: other event handlers
(no-input, custom events), webhooks on routes, conditional responses,
generators, callbacks, guardrails, examples and
evaluations, and languages other than the default. Values webhooks set
(`$session.params.x` that no form collects) become context variables, blank or
null, for you to fill in. Tools need their real address and secrets
(Integrations → Tool secrets) before live calls; until then `vk test`
and the builder's Test chat use the mock answers. Test with synthetic data.

## Export

The other direction: an agent folder as something Google's tools import.

```bash
vk export ces  agents/front-desk                   # front-desk-cx-agent-studio.zip: CX Agent Studio → Import app
vk export dfcx agents/front-desk --time-zone America/Chicago   # Dialogflow CX: Agent settings → Restore, then Validate
vk export ces  agents/front-desk --folder --out build/front-desk   # files instead of a zip, to diff or edit
```

The builder's **Export** menu does the same. Secrets never leave:
`{{secrets.NAME}}` headers become an API-key entry in the OpenAPI schema for
you to fill in there.

| Ours | CX Agent Studio | Dialogflow CX |
|---|---|---|
| root agent (Start's hand-over) | `rootAgent`; its instruction opens with Start's greeting | the start playbook (`startPlaybook`) |
| steps | agents with `instruction.txt` | playbooks (goal, steps) |
| transfers | child agents, plus `{@AGENT: x}` rules in the instruction | `${PLAYBOOK:x}` in the steps |
| End call | the `end_session` system tool | an "End Conversation" flow, reached with `${FLOW:End Conversation}` |
| global instructions | `global_instruction.txt` | added to every playbook |
| variables, details to collect | `variableDeclarations` | `$session.params.x` in the text; output parameters |
| HTTP tools | OpenAPI toolsets (one per service address) | OpenAPI tools with `schema.yaml` |
| tools with only a mock | Python tools that return the example answer | function tools |
| scripted pages | instructions written from the script (say exactly, ask these questions, rules in words) | pages in flows: entry fulfillment, form parameters, entity types for choices, yes/no and patterns |
| flows | | flows; a page entered from outside its flow (when the flow already starts elsewhere) gets a flow of its own |
| rules, otherwise, no match | rules in words | condition routes (gated on `$page.params.status = "FINAL"` when the page has a form), `sys.no-match-N` handlers |
| transfers the model decides, from a scripted page | | intents (training phrases from “quoted” examples in the condition) |
| tools a page runs (`on_entry`, `on_done`) | listed with the agent's tools | webhooks on the entry fulfillment and on the routes out |
| data and knowledge tools | Python tools with the example answer, and a note | function tools, and a note to use your own database or a data store |

Ids are stable (the same agent exports to the same files), and importing an
export gives back the same steps, transfers, greeting and variables, which the
tests check for every bundled template and for a mixed scripted agent (pages,
forms, rules and flows survive a Dialogflow CX round trip; checks like a
field's 1–8 range and read-back confirmations don't, as Dialogflow forms have
no place for them, and the report says so). CX Agent Studio can check an import
without applying it (`importOptions.validateOnly`); for Dialogflow CX, restore
into a test agent and press Validate. Triggers, connector and MCP tools, and
the API Trigger and Webhook nodes have no direct equivalent and are listed
in the notes.

## Lint

| Code | Severity | Check |
|---|---|---|
| VK001 | error | exactly one start node |
| VK003 | error | at most one global node and one trigger |
| VK004 | error | edge points at a missing node |
| VK005 | error | transition without a label or condition |
| VK006 | error | two transitions from one node become the same tool name |
| VK007 | error | connection rules (end has no outgoing, global/trigger are detached, …) |
| VK008 | error | agent or end node with no incoming edge |
| VK101 | warning | node unreachable from start |
| VK102 | warning | no end node |
| VK103 | warning | dead end (agent node with no way out) |
| VK104 | warning | empty prompt |
| VK105 | warning | prompt over budget (default 8000 chars) |
| VK106 | warning | sibling transitions with identical conditions |
| VK107 | warning | `{{variable}}` that nothing provides |
| VK108 | warning | webhook or pre-call fetch without a URL |
| VK109 | warning | duplicate node names |
| VK110 | error | a test names a step the agent doesn't have, or a test file is malformed |
| VK111 | error / warning | a tool definition problem: name, URL, method, parameters, a pasted key in a header, caller values in the address |
| VK112 | error | two tools with the same name |
| VK113 | error | a step lists a tool the agent doesn't define |
| VK114 | error | a step's tool has the same name as one of its transfers |
| VK115 | warning | a tool no step uses |
| VK116 | warning | a tool uses `{{name}}` that isn't a parameter or a known variable |
| VK120 | error / warning | a trigger definition problem: type, channel, cron, recipients, send settings |
| VK121 | error | two triggers with the same id |
| VK122 | error | two message triggers answer the same channel |
| VK130 | error / warning | a scripted page's settings: field types, choices, limits, patterns, attempts |
| VK131 | error / warning | a rule that can't be read or uses a variable nothing provides; otherwise / no match from an LLM step; a rule on Start |
| VK132 | warning | a scripted page that never moves on once its form is complete, or reads answers exactly but has transfers for the model |
| VK133 | error / warning | a page's `on_entry` / `on_done` names a tool that doesn't exist, or one whose arguments no variable provides |
| VK134 | error / warning | a transfer to a person: only on End call steps, to a phone number (E.164) or SIP address; details and variables it sends must exist; warns when the address comes from what the caller said |
| VK201 / 202 / 206 | error | looks like an SSN, a Luhn-valid card number, a medical record or member ID |
| VK203 / 204 / 205 | warning | full date, phone number, email |

The VK2xx scan covers prompts, greetings, transitions, context variables, tools'
descriptions, URLs, bodies, fixed values and mock answers, triggers' recipients and
messages, and `tests/*.yaml`. Obvious fakes are allowed: `123-45-6789`, `555-01xx` numbers,
`@example.com`. It is a guard rail for copy-paste mistakes, not a compliance
control: it cannot prove text is free of patient data. Use synthetic data.

The graph rules (VK0xx) catch what would stop an agent running: a missing or
second Start step, routes to steps that don't exist, steps nobody can reach,
and the like.

## Skills for coding agents

`vk skills install --target claude` copies four skills into `.claude/skills/`
(`agents` → `.agents/skills/`, `gemini` → `.gemini/skills/`):

- **voice-agent-builder**: asks the ten setup questions, writes tests from the
  example calls, builds the folder (often from a template), lints, and
  iterates until `vk test` passes. Never merges anything toward production.
- **voice-agent-fixer**: runs `vk test`, names the cause of each failure,
  changes only the prompt or transition behind it, and keeps a change only if
  more tests pass and none that passed breaks. Never edits a test to pass it.
- **dialogflow-tester**: imports a Dialogflow CX or CX Agent Studio export,
  writes tests for it, and runs them against the live Google agent
  (`--target dfcx|ces`), or side by side with Lampline (`--target engine,dfcx`).
- **voice-agent-reviewer**: lint, the diff against what's live, tests, a prompt
  and privacy review, then approves the merge only after you say yes.

The same four install as a Claude Code plugin: `/plugin marketplace add C4rohan/lampline`,
then `/plugin install lampline@lampline`.

## MCP server

`vk mcp` serves your agent folders to MCP clients that don't run a terminal,
like Claude Desktop and Cursor, over stdio. Its tools:

| Tool | What it does |
|---|---|
| `list_agents` | the agent folders, with their step and test counts |
| `lint_agent` | `vk lint`, as findings |
| `run_tests` | `vk test` (same JSON as `--format json`), with an optional `match` and `repeat` |
| `read_prompt` | one step's prompt, plus every step's name and id |
| `write_prompt` | replace one step's prompt |
| `list_failed_calls` | real calls whose call checks failed: when, and which checks failed and why |

On purpose, it can't change steps, routes, tools, triggers or webhooks (they
can use secrets or contact people), and it never returns call transcripts or
callers' numbers: MCP results go to the model. A prompt write is refused if it
drops a `{{variable}}` the step uses, or if it looks like real personal data.
It only reads inside the agents folder.

**Claude Code:**

```bash
claude mcp add lampline -- vk mcp --dir "$PWD/agents"
```

**Claude Desktop** (`claude_desktop_config.json`) or **Cursor** (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "lampline": {
      "command": "vk",
      "args": ["mcp", "--dir", "/full/path/to/your/agents"],
      "env": { "VK_ENGINE_URL": "http://localhost:7860" }
    }
  }
}
```

Use the full path to `vk` (`which vk`) if the app can't find it. `run_tests`
and `list_failed_calls` need the engine running; add `VK_ENGINE_KEY` to `env`
if it uses a key.

## Develop

```bash
pip install -e "cli[dev]"
pytest -q cli/tests
```

Tests run against an in-memory fake of the engine's text chat. The engine's own
test suite also runs `vk test` end to end against a real engine driven by a
deterministic fake model.
