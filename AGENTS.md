# Working in the Lampline repo

Lampline is a self-hosted platform for phone agents. An agent is a folder of
plain files, built and tested with the `vk` CLI and run by the engine.

## Building or changing a phone agent

An agent folder lives under `agents/<name>/`:

- `agent.yaml`: steps, routes between them, tools, values
- `prompts/*.md`: one prompt per step
- `tests/*.yaml`: conversations that define "done" (scripted turns, or a
  model-played `caller:` with a persona and goal)

```bash
pip install -e cli                          # the vk command
vk templates list                           # starter agents
vk templates install front-desk --dir agents/my-agent --name "My Agent"
vk lint agents/my-agent --format json       # offline checks; must have zero errors
vk test agents/my-agent --format json       # runs tests/*.yaml on the engine
vk import dfcx export.zip                   # from Dialogflow CX (or `ces` for CX Agent Studio)
vk export dfcx agents/my-agent              # back to Dialogflow CX (or `ces`)
```

`vk test` needs the engine running (`curl -fsS http://localhost:7860/v1/health`).
If it isn't, ask the person to start it: `scripts/docker-up.sh --free` runs
everything locally with no API keys. `vk test --target dfcx --agent projects/P/locations/L/agents/ID`
runs the same tests against a live Dialogflow CX agent with the person's own
`gcloud` login (`--target ces` for CX Agent Studio).

The full step-by-step guides are the skills in `cli/src/vk/skills/`:
`voice-agent-builder` (new agents and changes) and `voice-agent-reviewer`
(before a change goes live). Follow them.

## Rules

- **Synthetic data only:** made-up names, 555-01xx phone numbers, no real
  patient or customer details in prompts, tests, screenshots or commits.
  `vk lint` flags common patterns (VK2xx), but read anyway.
- **No secrets:** never ask the person to paste a key into chat or a file in
  the repo; keys go in the engine's settings or environment. CI scans every
  commit.
- **Tests describe behaviour, not wording:** check where the call goes and
  what must or must never be said. Never weaken or delete a test to make it
  pass.
- **Nothing goes live without the person's approval.** Changes reach `prod`
  only through reviewed pull requests: `dev` → `qa` → `prod`
  ([docs/BRANCHES.md](docs/BRANCHES.md)).

## Contributing to Lampline itself

Open pull requests against `dev`. Before pushing, run what CI runs:

```bash
shellcheck -S warning scripts/*.sh
pip install -e "cli[dev]" -e "engine[dev,moonshine]"
pytest -q cli/tests
pytest -q engine/tests                      # run the two suites separately
(cd engine/ui && npm ci && npm run build)   # if engine/ui changed: commit engine/src/vpengine/static too
python3 costs/cost_model.py > costs/COSTS.md
```

More in [CONTRIBUTING.md](CONTRIBUTING.md).
