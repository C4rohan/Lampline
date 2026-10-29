---
name: voice-agent-fixer
description: Use when asked to make a Lampline voice agent's failing tests pass, or to fix how an agent behaves on a call, by running vk test, changing the prompts or transition conditions behind the failures, and keeping a change only if it helps without breaking anything.
---

# Fix a voice agent from its failing tests

You change an agent folder (`agents/<name>/`: `agent.yaml` + `prompts/*.md`)
until more of its `tests/*.yaml` pass, one careful round at a time. A change
is kept only if **more tests pass and none that passed before fails**.
Nothing you do here goes live: the person reviews the result, and the
`voice-agent-reviewer` skill checks it before it's merged.

## 0. Check the engine and start clean

```bash
curl -fsS http://localhost:7860/v1/health     # or $VK_ENGINE_URL
git status --short agents/<name>
```

If the engine isn't running, ask the person to start it
(`scripts/docker-up.sh --free` needs no API keys). If the folder has
uncommitted changes, ask whether to commit them first: you need a clean
starting point to undo a round that doesn't help.

## 1. Run the tests and read the failures

```bash
vk lint agents/<name> --format json     # fix lint errors first; they block testing
vk test agents/<name> --format json
```

For each failed test, read `failures` (what didn't match) and `transcript`
and `steps` (what actually happened). Name the cause before changing
anything, for example:

- **Wrong route:** two transition `condition`s overlap, so the model picks
  the wrong next step. Fix: make the conditions specific and mutually
  exclusive.
- **Missing behaviour:** the step's prompt never tells the agent to do the
  thing the test expects (ask for the pet's type, confirm before hanging up).
  Fix: one clear instruction in that step's prompt.
- **Said something it mustn't:** add a plain rule to the step (or the
  `globalNode` if it applies everywhere).
- **Never ends:** no route reaches an End Call, or its condition is too
  strict.
- **The test itself is wrong** (it expects a step that was renamed, or
  exact wording): don't change it yourself. Explain and ask the person.

Models phrase things differently each run. Before calling a failure real,
run it a few more times: `vk test agents/<name> -k "<test name>" --repeat 3`.
If it passes on some runs (`flaky: true` in the JSON), report it as **flaky**,
which usually means two conditions overlap, rather than "fixing" a test that
mostly works. When you judge a change in step 3, `--repeat 2` or `3` makes
the before-and-after comparison more trustworthy.

## 2. Change as little as possible

- Edit only what the failures point at: one step's prompt in `prompts/*.md`,
  or one transition `condition` in `agent.yaml`.
- Keep every `{{variable}}` a prompt already uses.
- Don't invent facts the agent doesn't have (prices, hours, policies). If a
  fix needs one, tell the agent to ask, look it up with an existing tool, or
  transfer, and tell the person the value is missing.
- Don't add or remove steps, tools, triggers or webhooks unless the person
  asks; those change what the agent can reach.
- Prompts are spoken: short sentences, no lists or markdown read aloud.

## 3. Re-run everything and decide

```bash
vk lint agents/<name> --format json
vk test agents/<name> --format json
```

Compare with the run before your change:

- **More pass, and every test that passed still passes:** keep it.
- **Anything that passed now fails, or nothing improved:** undo it
  (`git checkout -- agents/<name>` returns to the last commit), and try a
  different cause or stop.

Stop after **three rounds**, or sooner if every test passes. Each test run
costs a few model calls.

## 4. Report to the person

In plain language:

1. Before and after: how many tests passed (and which were flaky).
2. Each change: the step, what you changed and why, as a short diff.
3. What's still failing and your best guess why.

Then suggest running the `voice-agent-reviewer` skill before anything is
merged toward production.

## Never

- Never weaken, delete or skip a test to make it pass.
- Never put real names, phone numbers, IDs or other customer data in a
  prompt or test. Use obviously fake values.
- Never merge, promote or deploy; the person decides that.
