# Branches: dev, qa, prod

Work moves one way: **dev → qa → prod**, each step a pull request, so every
change is reviewed and tested before anyone runs it.

| Branch | What it is | How changes get in | Checks before merging |
|---|---|---|---|
| `dev` | Day-to-day work; the default branch while the repo is private | commits, or short-lived branches merged by pull request | CI green |
| `qa` | A candidate release, frozen for testing | a pull request from `dev` | CI green, plus the QA checklist below |
| `prod` | What people install; tagged releases (`v0.2.0`) | a pull request from `qa` | CI green, QA signed off |

CI (`.github/workflows/ci.yml`) runs on every push and pull request to all three.

## Promoting

```bash
# dev -> qa: open a pull request, review, merge
gh pr create --base qa --head dev --title "QA: <what's in it>"

# qa -> prod, then tag the release
gh pr create --base prod --head qa --title "Release v0.2.0"
git switch prod && git pull && git tag -a v0.2.0 -m "v0.2.0" && git push origin v0.2.0
```

## QA checklist

- `scripts/docker-up.sh` on a clean checkout: the page opens with the printed link
- Builder: open an agent, add a scripted page and a rule, save, test chat
- Import a Dialogflow CX and a CX Agent Studio sample, export both back
- Data and Knowledge: import a CSV, upload a document, search it
- One real test call (browser, and a phone number if set up), synthetic data only

## Fixes to prod

Branch from `prod`, open a pull request into `prod`, then merge `prod` back into
`qa` and `dev` so the fix isn't lost on the next release.

## Going public

When the repo goes public, make `prod` the default branch so visitors land on
the released version, and keep working on `dev`.
