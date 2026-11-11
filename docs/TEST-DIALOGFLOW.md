# Test a Dialogflow CX agent in 10 minutes

You have a Dialogflow CX agent (or a CX Agent Studio app) and you check it by
talking to it by hand after every change. This guide gives it regression
tests: plain YAML files that say what a caller says and what must happen, run
against your **live Google agent** with one command. Nothing moves off Google.

You need Python 3.10 or later and the `gcloud` CLI. You don't need Docker or
the Lampline engine for the first four steps.

> **Before you start:** tests talk to the real agent, so its webhooks and
> tools run for real. Use a test project or a draft agent, made-up caller
> details only, and never an agent you aren't responsible for.

## 1. Install `vk` and sign in to Google

```bash
pipx install lampline        # installs the vk command (or: pip install lampline)
gcloud auth login            # your own Google account
```

`vk` asks `gcloud` for an access token each time it runs
(`gcloud auth print-access-token`), uses it for these requests, and never
saves it. In CI, set `VK_GOOGLE_TOKEN` instead.

## 2. Export your agent and turn it into a folder

In the Dialogflow CX console: **Agent settings → Export → JSON package**. Then:

```bash
vk import dfcx ~/Downloads/my-agent.zip --dir agents/my-agent
```

This is offline and changes nothing on Google. `vk` needs the folder for two
things: a place to keep the tests, and the names of your flows, pages and
playbooks, which is what tests refer to. `agents/my-agent/import-report.md`
lists what came across. (CX Agent Studio: export the app, then `vk import ces`.)

## 3. Write two tests

Tests live in `agents/my-agent/tests/`, one conversation per file. Start with
the most common reason people call. Replace the names in `reaches` with your
own flow, page or playbook names (they're in `agent.yaml`).

`agents/my-agent/tests/book-a-visit.yaml`:

```yaml
name: caller books a visit
turns:
  - user: I'd like to book an appointment.
    expect: {reaches: Booking}          # checked right after this turn
  - user: Thursday morning works.
expect:
  reaches: [Booking]                    # pages, flows or playbooks, in order
  says: [Thursday]                      # text the agent must say (case-insensitive)
  not_says: [account number]            # text it must never say
```

And one for a caller who must reach a person.
`agents/my-agent/tests/wants-a-person.yaml`:

```yaml
name: caller asks for a person
turns:
  - user: Can I talk to someone, please?
expect:
  ends: true                            # an end of session, a live-agent handoff or a phone transfer
```

Other checks you can use: `not_reaches`, `calls_tool` (a tool's display
name), and `context:` at the top of a file to set session parameters for the
first turn. Test where the call goes and what must or must never be said, not
exact sentences: models phrase things differently every run.

## 4. Run them against your agent

Your agent's name is in the console's URL:
`projects/PROJECT/locations/LOCATION/agents/AGENT_ID`.

```bash
vk test agents/my-agent --target dfcx \
  --agent projects/my-project/locations/global/agents/AGENT_ID
```

Example output:

```text
PASS  caller books a visit  (2 turn(s), 1.9s)  Default Start Flow → Booking
FAIL  caller asks for a person  (1 turn(s), 0.8s)  Default Start Flow
      - expected the conversation to end
          caller: Can I talk to someone, please?
           agent: I can help with bookings and opening hours.

1 passed, 1 failed
```

A failure shows what was expected, the steps the call actually went through,
and the conversation. The command exits with 1 when anything fails, so it
works as a CI check.

- **A published environment, not the draft:** add `--environment ENVIRONMENT_ID`.
- **CX Agent Studio:** `--target ces --agent projects/P/locations/L/apps/APP_ID`
  (add `--deployment DEPLOYMENT_ID` for a deployment).
- **Another language:** `--language es`.
- **For scripts and coding agents:** `--format json`.

## 5. Catch flaky routes

One passing run proves little, because the model can answer differently next
time. Run each test a few times:

```bash
vk test agents/my-agent --target dfcx --agent projects/... --repeat 3
```

A test that passes on some runs and fails on others is reported as **FLAKY**,
with its failing run. That usually means two routes or intents overlap, which
is worth fixing before a caller finds it.

## 6. Optional: callers you can't script

Some callers ramble, interrupt or change their mind. Describe one and let a
model play them:

```yaml
name: impatient caller with a billing question
caller:
  persona: Sam, in a hurry, confused by a late fee
  goal: get the fee explained or talk to someone in billing
  max_turns: 8
expect:
  not_says: [card number]
```

The Lampline engine's model plays the caller, so this kind of test needs the
engine running. It's free and needs no API keys:

```bash
git clone https://github.com/C4rohan/lampline && cd lampline
scripts/docker-up.sh --free
```

Then run `vk test` as before (`--url` if the engine isn't on
`http://localhost:7860`).

## 7. Optional: let your coding agent do it

The `dialogflow-tester` skill teaches Claude Code, Cursor or Codex these
steps. In Claude Code:

```
/plugin marketplace add C4rohan/lampline
/plugin install lampline@lampline
```

Then ask: *"Write regression tests for my Dialogflow CX agent"*.

## Where to go next

- **Run the tests in CI** on every change to the agent, with `VK_GOOGLE_TOKEN`
  from your CI's Google sign-in.
- **Thinking of moving off Dialogflow?** The folder from step 2 runs on
  Lampline as it is. `--target engine,dfcx` runs every test on both and prints
  a side-by-side table, so you can see exactly where the two differ.
- **All the options:** [the vk CLI reference](../cli/README.md).

## Good to know

- **Text only for now.** These tests send text turns to the agent. For real
  phone calls graded by an AI caller, see [EVALS.md](EVALS.md).
- **What the import doesn't carry over** is listed in `import-report.md`. It
  doesn't affect tests against the live Google agent, only running the folder
  on Lampline.
- **Costs:** each turn is one Dialogflow request on your Google bill.
