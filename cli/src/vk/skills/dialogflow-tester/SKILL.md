---
name: dialogflow-tester
description: Use when asked to test an existing Dialogflow CX or CX Agent Studio agent, write regression tests for it, or check that an agent moved to Lampline behaves like the original, using vk test against the live Google agent.
---

# Test a Dialogflow CX or CX Agent Studio agent with vk

`vk test` runs the same conversation tests against a live Google agent that
it runs on Lampline: scripted turns, or a model playing the caller. It checks
which flows, pages or playbooks the call went through, which tools ran, and
what the agent said. Nothing moves to Lampline unless the person wants it to.

## 0. What you need from the person

1. **The agent's resource name:**
   - Dialogflow CX: `projects/P/locations/L/agents/ID`
   - CX Agent Studio: `projects/P/locations/L/apps/ID`
2. **Their own Google login:** `gcloud auth login` once. vk reads the token
   from `gcloud auth print-access-token` (or `VK_GOOGLE_TOKEN`). Never ask
   them to paste a token into chat or a file, and never print or store one.
3. **An export of the agent:**
   - Dialogflow CX: Agent settings → Export → JSON package
   - CX Agent Studio: export the app
   vk needs a folder to keep the tests in, and it reads step names from the
   export.

Only test agents the person owns or is allowed to test, and use made-up test
data: these calls run on their real Google project and its logs.

## 1. Make the folder

```bash
pip install -e cli                                     # if vk isn't installed
vk import dfcx export.zip --dir agents/<name>          # or: vk import ces app-export.zip
vk lint agents/<name>
```

The import report lists what was converted. Lint errors block `vk test`;
fix them or explain them. Step names in the folder match the export's flows,
pages and playbooks, which is what the tests refer to.

## 2. Write the tests

One conversation per file in `agents/<name>/tests/*.yaml`. Ask the person for
the three to five calls that matter most (the top reasons people call, and
one that must hand off to a person), or offer to draft them.

```yaml
name: caller books a checkup
turns:
  - user: I'd like to book a checkup for my dog.
    expect: {reaches: Booking}            # a flow, page or playbook name
  - user: Thursday morning works.
expect:
  reaches: [Booking]
  says: [Thursday]                        # case-insensitive text the agent must say
  not_says: [account number]
  calls_tool: [find_slots]                # if the agent uses tools
```

For callers you can't script line by line, a model plays the caller:

```yaml
name: impatient caller wants a person
caller:
  persona: Sam, in a hurry, annoyed by the menu
  goal: talk to a person about a billing question
  max_turns: 8
expect: {reaches: [Live Agent Handoff]}
```

A `caller:` test needs the Lampline engine running (`scripts/docker-up.sh
--free`, no keys), because the engine's model plays the caller. Scripted
`turns:` tests don't.

Test behaviour, not wording: where the call goes and what must or must never
be said.

## 3. Run them against the Google agent

```bash
# Dialogflow CX (add --environment ID to test a published environment, not the draft)
vk test agents/<name> --target dfcx --agent projects/P/locations/L/agents/ID --format json

# CX Agent Studio (add --deployment ID for a deployment)
vk test agents/<name> --target ces --agent projects/P/locations/L/apps/ID --format json
```

For each failure, read `failures`, `steps` and `transcript`, and say plainly
what the Google agent did differently. Run a failing test once more
(`-k "<test name>"`) before reporting it: a test that passes the second time
is **flaky**, which usually means two routes overlap.

## 4. Moving to Lampline? Compare side by side

```bash
vk test agents/<name> --target engine,dfcx --agent projects/P/locations/L/agents/ID
```

This runs every test on both and prints a side-by-side summary. A test that
passes on Google but fails on Lampline is a migration gap. Fix it in the
folder with the `voice-agent-fixer` skill, never by changing the test.

## 5. Report

Tell the person, in plain language:

1. How many tests passed on each target, and which failed or were flaky.
2. For each failure: what the agent did, and the likely cause.
3. That the tests are now files: rerun them after every change to the agent,
   or in CI.

## Never

- Never change the Google agent itself. This skill only tests it.
- Never store or print access tokens, and never commit an export that holds
  real customer data.
- Never test an agent the person doesn't own or have permission to test.
