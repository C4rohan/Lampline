# Branches: dev, qa, prod

Work moves one way: **dev → qa → prod**, each step a pull request, so every
change is reviewed and tested before anyone runs it.

| Branch | What it is | How changes get in | Checks before merging |
|---|---|---|---|
| `dev` | Day-to-day work; the default branch while the repo is private | commits, or short-lived branches merged by pull request | CI green |
| `qa` | A candidate release, frozen for testing | a pull request from `dev` | CI green, plus the QA checklist below |
| `prod` | What people install; tagged releases (`v0.2.0`) | a pull request from `qa` | CI green, QA signed off |

CI (`.github/workflows/ci.yml`) runs on every push and pull request to all three.

## Shipping

`scripts/ship.sh` does every step; GitHub does the rest.

```bash
scripts/ship.sh "Fix the transfer message"   # commit, push, open a pull request into dev
scripts/ship.sh qa                           # open dev -> qa, listing the pull requests in it
scripts/ship.sh prod                         # open qa -> prod; merging it releases the version in VERSION
scripts/ship.sh status                       # what's waiting where
```

Add `--check` to run CI's tests first. You still review and merge every pull request yourself.

**Releasing a new version:** on dev, run `scripts/version.sh set 0.6.0` (it
updates `VERSION` and every copy: both packages, the web app and the
citation), commit it, and promote as usual. When it reaches prod, the
`release` workflow tags `v0.6.0` and publishes the release. Without a bump,
merges into prod just update prod. CI fails if any copy of the version differs.

What runs on its own:

| Workflow | When | What it does |
|---|---|---|
| `ci` | every push and pull request to dev, qa, prod | tests, UI build check, secret scan, Docker image |
| `sync` | something lands on qa or prod | merges it back into dev, and opens a prod -> qa pull request after a hotfix, so the next promotion never conflicts |
| `ui-build` | a pull request into dev changes `engine/ui` | rebuilds the committed UI if it's stale and pushes it to the branch (Dependabot's weekly update too) |
| `release` | anything lands on prod, or **Actions → release → Run workflow** | releases the version in `VERSION` if it isn't tagged yet: tags prod and publishes a GitHub release with notes |
| `prune-branches` | a pull request merges, every Monday, or **Run workflow** | deletes the merged branch; the weekly sweep deletes any branch whose work is already in dev or prod, unless it has an open pull request (restore one from its pull request's page) |

## QA checklist

- `scripts/docker-up.sh` on a clean checkout: the page opens with the printed link
- Builder: open an agent, add a scripted page and a rule, save, test chat
- Import a Dialogflow CX and a CX Agent Studio sample, export both back
- Data and Knowledge: import a CSV, upload a document, search it
- One real test call (browser, and a phone number if set up), synthetic data only

## Fixes to prod

Branch from `prod` and open a pull request into `prod`. Once it merges, the
`sync` workflow carries it back into `dev` and opens the pull request into `qa`.

## Going public

When the repo goes public, make `prod` the default branch so visitors land on
the released version, and keep working on `dev`.
