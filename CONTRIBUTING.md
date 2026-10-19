# Contributing

Open pull requests against `dev`, and follow the [code of conduct](CODE_OF_CONDUCT.md). Changes reach `qa` and then `prod` by pull request; see [docs/BRANCHES.md](docs/BRANCHES.md).

Thanks for helping. The most valuable contributions right now:

1. **Benchmarks.** Real numbers for latency, quality and concurrent calls per
   GPU/vCPU for any STT + LLM + TTS combination. Add them under `benchmarks/`
   with your hardware, versions and method, then update the assumptions in
   `costs/cost_model.py`.
2. **Contact-center connections.** Genesys Cloud AudioHook (Pipecat ships
   `GenesysAudioHookSerializer`) is the top roadmap item, then Avaya, Cisco,
   Amazon Connect, NICE CXone, Five9 and Twilio Flex ([roadmap](docs/ROADMAP.md)).
3. **Google Cloud deployment.** A one-command deployment of the engine to your
   own project is next; AWS and Azure after it.
4. **Agent templates.** Ready-to-use agent folders (appointment scheduling,
   bilingual EN/ES front desk, ...). No real customer data.

## Before opening a PR

Run the same checks CI runs:

```bash
shellcheck -S warning scripts/*.sh
pip install -e "cli[dev]" -e "engine[dev]" && pytest -q cli/tests engine/tests
(cd engine/ui && npm ci && npm run build)       # commit the rebuilt engine/src/vpengine/static
python3 costs/cost_model.py > costs/COSTS.md   # commit the regenerated file if you changed prices or assumptions
```

Price changes need a source link in the PR description.
