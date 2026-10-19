## What changes

<!-- One or two sentences: what this does and why. -->

## Checks

- [ ] `pytest -q cli/tests` and `pytest -q engine/tests` pass
- [ ] If the web app changed: `cd engine/ui && npm run build`, and the rebuilt `engine/src/vpengine/static` is committed
- [ ] Docs updated (README, engine/README.md or cli/README.md) if behaviour changed
- [ ] Synthetic data only: no real names, phone numbers, patient or customer details, in code, tests, screenshots or this description
- [ ] No keys or tokens anywhere in the change (CI scans for them)

Pull requests go to `dev`; `qa` and `prod` only take pull requests from the branch before them ([docs/BRANCHES.md](../docs/BRANCHES.md)).
