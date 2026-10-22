#!/usr/bin/env bash
# One command for every step of getting a change out. Needs git and gh (logged in).
#
#   scripts/ship.sh "What this change does"   commit what you've changed, push, open a pull request into dev
#   scripts/ship.sh qa                        open the dev -> qa pull request (a release candidate)
#   scripts/ship.sh prod v0.4.0               open the qa -> prod pull request; merging it tags v0.4.0
#   scripts/ship.sh sync                      carry prod and qa back into dev (the sync workflow does this for you)
#   scripts/ship.sh status                    what's waiting where
#
# Add --check to run the same tests CI runs before pushing.
set -euo pipefail

die() { echo "ship: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "needs $1"; }
need git
need gh
cd "$(git rev-parse --show-toplevel)"

check=0
args=()
for a in "$@"; do
  case "$a" in
    --check) check=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) args+=("$a") ;;
  esac
done
set -- "${args[@]+"${args[@]}"}"
[ $# -ge 1 ] || { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

run_checks() {
  echo "Running the CI checks..."
  pytest -q cli/tests engine/tests
  # the UI changed (committed or not): rebuild it so the committed build matches
  if [ -n "$(git diff --name-only origin/dev -- engine/ui; git status --porcelain -- engine/ui)" ]; then
    (cd engine/ui && npm ci --no-audit --no-fund && npm run build)
  fi
  python3 costs/cost_model.py > costs/COSTS.md
}

# open a pull request unless one is already open; print its link either way
open_pr() {   # base head title body
  local url
  url=$(gh pr list --base "$1" --head "$2" --state open --json url -q '.[0].url')
  if [ -n "$url" ]; then echo "Already open: $url"; return; fi
  gh pr create --base "$1" --head "$2" --title "$3" --body "$4"
}

prs_between() {   # base head: the pull requests merged into head that base doesn't have yet
  git log --merges --format='%s' "origin/$1..origin/$2" \
    | sed -nE 's/^Merge pull request #([0-9]+) from [^ ]+$/- #\1/p' | sort -u
}

case "$1" in
  qa)
    git fetch -q origin
    [ -n "$(git rev-list origin/qa..origin/dev)" ] || die "qa already has everything on dev"
    open_pr qa dev "QA: $(date +%Y-%m-%d)" "Release candidate from dev.

$(prs_between qa dev)

QA checklist: docs/BRANCHES.md"
    ;;

  prod)
    version="${2:-}"
    [[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "give the version: scripts/ship.sh prod v0.4.0"
    git fetch -q --tags origin
    git rev-parse -q --verify "refs/tags/$version" >/dev/null && die "$version is already tagged"
    [ -n "$(git rev-list origin/prod..origin/qa)" ] || die "prod already has everything on qa"
    open_pr prod qa "Release $version" "Merging this tags $version and publishes the release.

$(prs_between prod qa)"
    ;;

  sync)
    git fetch -q origin
    [ -z "$(git status --porcelain)" ] || die "commit or stash your changes first"
    here=$(git branch --show-current)
    git switch -q dev && git pull -q --ff-only origin dev
    git merge --no-edit origin/prod origin/qa || die "conflict: resolve it, commit, then: git push origin dev"
    git push origin dev
    [ -n "$(git rev-list origin/qa..origin/prod)" ] && open_pr qa prod "Sync: prod into qa" "Carries prod's changes back into qa."
    git switch -q "$here"
    ;;

  status)
    git fetch -q origin
    echo "dev  -> qa  : $(git rev-list --count origin/qa..origin/dev) commits waiting"
    echo "qa   -> prod: $(git rev-list --count origin/prod..origin/qa) commits waiting"
    echo "prod -> dev : $(git rev-list --count origin/dev..origin/prod) commits not yet back in dev"
    echo
    gh pr list --state open
    ;;

  *)
    title="$1"
    branch=$(git branch --show-current)
    git fetch -q origin dev
    if [[ "$branch" =~ ^(dev|qa|prod)$ ]]; then
      [ -n "$(git status --porcelain)" ] || die "nothing to ship: no changes here"
      slug=$(echo "$title" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//; s/-$//' | cut -c1-40)
      branch="change/${slug:-update}"
      git switch -q -c "$branch"
    fi
    [ "$check" = 1 ] && run_checks
    if [ -n "$(git status --porcelain)" ]; then
      echo "Committing:"; git status --short
      git add -A
      git commit -q -m "$title"
    fi
    git push -q -u origin "$branch"
    open_pr dev "$branch" "$title" "$(git log --format='- %s' "origin/dev..HEAD")"
    ;;
esac
