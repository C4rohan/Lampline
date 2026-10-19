---
name: voice-agent-reviewer
description: Use before a voice agent change goes live on the self-hosted Lampline engine, or when asked to review an agent folder or its changes, using vk lint, git diff, vk test and a privacy review.
---

# Review a voice agent before it goes live

You decide whether a change is ready. It goes live only by merging it toward
production (see docs/BRANCHES.md), and **only after the user explicitly
approves** the summary you give them in step 4.

## 1. Mechanical checks

```bash
vk lint agents/<name> --format json     # must have zero errors
git diff prod -- agents/<name>          # what the change does vs what is live
vk test agents/<name> --format json     # conversations in tests/*.yaml, on the engine
```

If lint reports errors or a test fails, stop and list them; do not approve
the change. If the folder has no tests, say so and suggest two or three. A test
that fails once but passes on a re-run is still worth reporting (it means the
transition conditions are ambiguous).

## 2. Read every changed prompt and transition

For each node and edge in the diff, check:
- **Clarity:** does the prompt say what this step is for and when it's done?
- **Transitions:** are sibling conditions specific and mutually exclusive?
  Could the LLM loop between two nodes forever?
- **Exits:** can every caller reach an End Call or a human handoff?
- **Variables:** is every `{{variable}}` provided before it is used?
- **Voice fit:** short sentences, no lists or markdown read aloud, numbers and
  dates phrased the way people say them.

## 3. Privacy and safety review

- No real patient/customer data in prompts, greetings, context_variables or
  tests (vk lint's VK2xx rules catch common patterns; read anyway).
- Identity is verified before any account or health information is spoken.
- The agent never reads back full identifiers (SSN, card, member ID).
- If `setup.real_data` is true in agent.yaml (or missing; assume true): every
  vendor in the call path needs a BAA/DPA, and SECURITY.md's checklist must be
  satisfied. Say plainly if you cannot confirm this.

## 4. Summarize and ask

Give the user, in plain language:
1. What changes for callers (2–5 bullets).
2. Risks you found, most serious first, and what you'd fix.
3. Your verdict: **ready**, **ready with fixes**, or **not ready**.

Then ask: "Merge this toward production?" Only on an explicit yes, open or
approve the pull request the way docs/BRANCHES.md describes. CI re-runs lint
and the tests; never work around a failure. Re-running a test that failed on
a confirmed model hiccup is fine if the user says so.
