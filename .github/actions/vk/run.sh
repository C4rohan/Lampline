#!/usr/bin/env bash
# The vk action's lint and test step (action.yml passes its inputs as INPUT_* variables).
# Runs locally too:  INPUT_AGENTS='agents/*' INPUT_TEST=true bash .github/actions/vk/run.sh
# Written for bash 3.2 as well (macOS runners): no mapfile, no ${x,,}, empty arrays guarded.
set -uo pipefail

vk="${VK:-vk}"
py="${PY:-python3}"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
error() { echo "::error::$*"; exit 2; }

# true/false inputs: anything else is a mistake, not a quiet "off".
flag() {  # flag VAR INPUT_NAME VALUE DEFAULT: sets VAR to true or false
  local v
  v="$(printf '%s' "${3:-$4}" | tr '[:upper:]' '[:lower:]')"
  case "$v" in
    true|false) printf -v "$1" '%s' "$v" ;;
    *) error "input $2 is true or false; got '$3'" ;;
  esac
}
lint="" strict="" test="" coverage=""
flag lint lint "${INPUT_LINT:-}" true
flag strict strict "${INPUT_STRICT:-}" true
flag test test "${INPUT_TEST:-}" false
flag coverage coverage "${INPUT_COVERAGE:-}" false

# A pull request from a fork gets none of the repository's secrets.
fork_pr() {
  case "${GITHUB_EVENT_NAME:-}" in pull_request|pull_request_target) ;; *) return 1 ;; esac
  [ -f "${GITHUB_EVENT_PATH:-}" ] || return 1
  "$py" - "$GITHUB_EVENT_PATH" "${GITHUB_REPOSITORY:-}" <<'PY'
import json, sys
try:
    event = json.load(open(sys.argv[1]))
    head = ((event.get("pull_request") or {}).get("head") or {}).get("repo") or {}
except (OSError, ValueError, AttributeError):
    sys.exit(1)
sys.exit(0 if head.get("full_name") and head["full_name"] != sys.argv[2] else 1)
PY
}

# --- which folders ------------------------------------------------------------------------
# Entries are separated by newlines; on a single line, by spaces. A folder whose name has a
# space goes on its own line. Globs expand, and the folders they match may contain spaces.
folders=()
add() {
  local d="${1%/}" f
  for f in "${folders[@]+"${folders[@]}"}"; do [ "$f" = "$d" ] && return; done
  folders+=("$d")
}
consider() {
  local d="$1" sub
  [ -d "$d" ] || return 0
  if [ -f "$d/agent.yaml" ]; then
    add "$d"
  else                                              # a folder of agent folders
    for sub in "$d"/*/; do [ -f "$sub/agent.yaml" ] && add "$sub"; done
  fi
}
input="${INPUT_AGENTS:-agents/*}"
case "$input" in
  *$'\n'*) entries="$input" ;;
  *) entries="$(printf '%s' "$input" | tr -s ' \t' '\n\n')" ;;
esac
while IFS= read -r pattern; do
  pattern="$(printf '%s' "$pattern" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -n "$pattern" ] || continue
  case "$pattern" in
    *[*?[]*) while IFS= read -r match; do consider "$match"; done < <(compgen -G "$pattern" | LC_ALL=C sort || true) ;;
    *) consider "$pattern" ;;
  esac
done <<< "$entries"
[ ${#folders[@]} -gt 0 ] || error "no agent folders (a folder with agent.yaml) in: $input"
echo "Agent folders:"; for f in "${folders[@]}"; do echo "  $f"; done

[ -n "${INPUT_ENGINE_URL:-}" ] && export VK_ENGINE_URL="$INPUT_ENGINE_URL"
[ -n "${INPUT_ENGINE_KEY:-}" ] && export VK_ENGINE_KEY="$INPUT_ENGINE_KEY"
skip_tests=""
if [ "$test" = true ]; then
  [ -n "${INPUT_GOOGLE_AGENT:-}" ] && [ ${#folders[@]} -gt 1 ] &&
    error "google-agent tests one Google agent: give one agent folder in agents, not ${#folders[@]}"
  [ "$coverage" = true ] && [ -n "${INPUT_JUNIT:-}" ] &&
    error "coverage and junit don't go together; use one of them"
  with_tests=0
  for f in "${folders[@]}"; do compgen -G "$f/tests/*.yaml" >/dev/null && with_tests=$((with_tests + 1)); done
  [ $with_tests -gt 0 ] || error "test is on, but none of the agent folders has tests/*.yaml"
  if [ -n "${LAMPLINE_ACTION_SKIP_TESTS:-}" ]; then
    skip_tests="$LAMPLINE_ACTION_SKIP_TESTS"
  else
    case ",${INPUT_TARGET:-engine}," in
      *,dfcx,*|*,ces,*)
        if [ -z "${VK_GOOGLE_TOKEN:-}" ] && fork_pr; then
          skip_tests="a pull request from a fork has no Google token"
        fi ;;
    esac
    case ",${INPUT_TARGET:-engine}," in
      *,engine,*)
        if [ -z "$skip_tests" ] && [ -n "${INPUT_ENGINE_URL:-}" ] && [ -z "${VK_ENGINE_KEY:-}" ] && fork_pr; then
          skip_tests="a pull request from a fork has no engine-key"
        fi ;;
    esac
  fi
  [ -n "$skip_tests" ] && echo "::warning::not running tests: $skip_tests. Lint still runs."
fi
# The engine step's skip is for this use of the action only, not for later steps of the job.
[ -n "${LAMPLINE_ACTION_SKIP_TESTS:-}" ] && [ -n "${GITHUB_ENV:-}" ] && echo "LAMPLINE_ACTION_SKIP_TESTS=" >> "$GITHUB_ENV"

failed=0
{ echo "### Lampline agents"; echo; echo "| Agent | Lint | Tests |"; echo "|---|---|---|"; } >> "$summary"

# --- each folder --------------------------------------------------------------------------
for folder in "${folders[@]}"; do
  lint_cell="–"; test_cell="–"
  echo "::group::$folder"

  if [ "$lint" = true ]; then
    strict_args=(); [ "$strict" = true ] && strict_args=(--strict)
    "$vk" lint "${strict_args[@]+"${strict_args[@]}"}" "$folder"; code=$?
    # One annotation per finding, on the folder's agent.yaml, so they show on the pull request.
    "$vk" lint --format json "$folder" 2>/dev/null | "$py" -c '
import json, sys
folder, strict = sys.argv[1], sys.argv[2] == "true"
for f in json.load(sys.stdin):
    level = "error" if f["severity"] == "error" or strict else "warning"
    msg = (f.get("where") or "") + ": " + f["message"]
    msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::%s file=%s/agent.yaml,title=%s::%s" % (level, folder, f["code"], msg))
' "$folder" "$strict" || true
    if [ $code -eq 0 ]; then lint_cell="✅ clean"; else lint_cell="❌ problems"; failed=1; fi
  fi

  if [ "$test" = true ]; then
    if ! compgen -G "$folder/tests/*.yaml" >/dev/null; then
      echo "no tests/*.yaml in $folder; skipping tests"
      test_cell="no tests"
    elif [ -n "$skip_tests" ]; then
      test_cell="⚠️ skipped ($skip_tests)"
    else
      opts=(--target "${INPUT_TARGET:-engine}" --repeat "${INPUT_REPEAT:-1}")
      [ -n "${INPUT_GOOGLE_AGENT:-}" ] && opts+=(--agent "$INPUT_GOOGLE_AGENT")
      [ "$coverage" = true ] && opts+=(--coverage)
      # shellcheck disable=SC2206  # extra arguments split on spaces, like on a command line
      extra=(${INPUT_ARGS:-})
      if [ -n "${INPUT_JUNIT:-}" ]; then
        mkdir -p "$INPUT_JUNIT"
        # Named after the whole path, so agents/desk and other/desk don't overwrite each other.
        rel="${folder#"$PWD"/}"
        report="$INPUT_JUNIT/$(printf '%s' "$rel" | sed -e 's#^\./##' -e 's#^/##' -e 's#[^A-Za-z0-9._-]#_#g').xml"
        "$vk" test "$folder" "${opts[@]}" "${extra[@]+"${extra[@]}"}" --format junit > "$report"; code=$?
        if [ ! -s "$report" ]; then
          rm -f "$report"                            # nothing ran: no report, rather than an empty one
          test_cell="no report: the tests didn't run (see the log)"
        else
          # The report holds the details; print what passed and failed here too.
          test_cell="$("$py" - "$report" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
total = bad = errors = 0
for case in root.iter("testcase"):
    total += 1
    problem = case.find("failure")
    if problem is None:
        problem = case.find("error")
        errors += problem is not None
    if problem is not None:
        bad += 1
        why = problem.get("message") or ((problem.text or "").strip().splitlines() or [""])[0]
        print(f"{problem.tag.upper():7} {case.get('name')}: {why}", file=sys.stderr)
    else:
        print(f"PASS    {case.get('name')}", file=sys.stderr)
note = f", {errors} engine error(s)" if errors else ""
print(f"{total - bad} of {total} passed{note}")
PY
)" || test_cell="unreadable report (see the log)"
          echo "$test_cell (JUnit report: $report)"
        fi
      else
        log="$(mktemp)"
        "$vk" test "$folder" "${opts[@]}" "${extra[@]+"${extra[@]}"}" | tee "$log"; code=${PIPESTATUS[0]}
        test_cell="$(grep -E '^[0-9]+ passed' "$log" | tail -1)"
        [ -n "$test_cell" ] || test_cell="see the log"
        rm -f "$log"
      fi
      if [ $code -eq 0 ]; then test_cell="✅ $test_cell"; else test_cell="❌ $test_cell"; failed=1; fi
    fi
  fi

  echo "::endgroup::"
  echo "| \`$folder\` | $lint_cell | $test_cell |" >> "$summary"
done

exit $failed
