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
pip install -e "cli[dev]" -e "engine[dev,moonshine]" && pytest -q cli/tests engine/tests
(cd engine/ui && npm ci && npm run build)       # commit the rebuilt engine/src/vpengine/static
python3 costs/cost_model.py > costs/COSTS.md   # commit the regenerated file if you changed prices or assumptions
```

The built UI in `engine/src/vpengine/static` has fixed file names. If a merge
(or GitHub's **Update branch**) reports a conflict there, don't pick a side:
run `(cd engine/ui && npm ci && npm run build)` and commit the result.

Price changes need a source link in the PR description.

## Dependency updates

Dependabot opens one pull request a week for the engine UI's packages. Its
`engine-ui` check fails until the committed build is rebuilt, because
Dependabot can't run the build. Check out its branch, run
`(cd engine/ui && npm ci && npm run build)`, commit
`engine/src/vpengine/static` and `engine/ui/public/THIRD-PARTY-LICENSES.txt`,
and push to the same branch.
