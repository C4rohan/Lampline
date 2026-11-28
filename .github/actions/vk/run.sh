#!/usr/bin/env bash
# The vk action's lint and test step (action.yml passes its inputs as INPUT_* variables).
# Runs locally too:  INPUT_AGENTS='agents/*' INPUT_TEST=true bash .github/actions/vk/run.sh
set -uo pipefail

vk="${VK:-vk}"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
on() { [ "${1:-}" = "true" ]; }
error() { echo "::error::$*"; exit 2; }

# --- which folders ------------------------------------------------------------------------
folders=()
add() {
  local d="${1%/}" f
  for f in "${folders[@]+"${folders[@]}"}"; do [ "$f" = "$d" ] && return; done
  folders+=("$d")
}
shopt -s nullglob
for pattern in ${INPUT_AGENTS:-agents/*}; do          # unquoted on purpose: entries split, globs expand
  for d in $pattern; do
    [ -d "$d" ] || continue
    if [ -f "$d/agent.yaml" ]; then
      add "$d"
    else                                              # a folder of agent folders
      for sub in "$d"/*/; do [ -f "$sub/agent.yaml" ] && add "$sub"; done
    fi
  done
done
shopt -u nullglob
[ ${#folders[@]} -gt 0 ] || error "no agent folders (a folder with agent.yaml) in: ${INPUT_AGENTS:-agents/*}"
echo "Agent folders: ${folders[*]}"

[ -n "${INPUT_ENGINE_URL:-}" ] && export VK_ENGINE_URL="$INPUT_ENGINE_URL"
[ -n "${INPUT_ENGINE_KEY:-}" ] && export VK_ENGINE_KEY="$INPUT_ENGINE_KEY"
if on "${INPUT_TEST:-}"; then
  [ -n "${INPUT_GOOGLE_AGENT:-}" ] && [ ${#folders[@]} -gt 1 ] &&
    error "google-agent tests one Google agent: give one agent folder in agents, not ${#folders[@]}"
  on "${INPUT_COVERAGE:-}" && [ -n "${INPUT_JUNIT:-}" ] &&
    error "coverage and junit don't go together; use one of them"
fi

failed=0
{ echo "### Lampline agents"; echo; echo "| Agent | Lint | Tests |"; echo "|---|---|---|"; } >> "$summary"

# --- each folder --------------------------------------------------------------------------
for folder in "${folders[@]}"; do
  name="$(basename "$folder")"
  lint_cell="–"; test_cell="–"
  echo "::group::$folder"

  if on "${INPUT_LINT:-true}"; then
    strict=(); on "${INPUT_STRICT:-true}" && strict=(--strict)
    "$vk" lint "${strict[@]}" "$folder"; code=$?
    # One annotation per finding, on the folder's agent.yaml, so they show on the pull request.
    "$vk" lint --format json "$folder" 2>/dev/null | python3 -c '
import json, sys
folder, strict = sys.argv[1], sys.argv[2] == "1"
for f in json.load(sys.stdin):
    level = "error" if f["severity"] == "error" or strict else "warning"
    msg = (f.get("where") or "") + ": " + f["message"]
    msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::%s file=%s/agent.yaml,title=%s::%s" % (level, folder, f["code"], msg))
' "$folder" "$([ ${#strict[@]} -gt 0 ] && echo 1 || echo 0)" || true
    if [ $code -eq 0 ]; then lint_cell="✅ clean"; else lint_cell="❌ problems"; failed=1; fi
  fi

  if on "${INPUT_TEST:-}"; then
    if ! compgen -G "$folder/tests/*.yaml" >/dev/null; then
      echo "no tests/*.yaml in $folder; skipping tests"
      test_cell="no tests"
    else
      opts=(--target "${INPUT_TARGET:-engine}" --repeat "${INPUT_REPEAT:-1}")
      [ -n "${INPUT_GOOGLE_AGENT:-}" ] && opts+=(--agent "$INPUT_GOOGLE_AGENT")
      on "${INPUT_COVERAGE:-}" && opts+=(--coverage)
      # shellcheck disable=SC2206  # extra arguments split on spaces, like on a command line
      extra=(${INPUT_ARGS:-})
      if [ -n "${INPUT_JUNIT:-}" ]; then
        mkdir -p "$INPUT_JUNIT"
        report="$INPUT_JUNIT/$name.xml"
        "$vk" test "$folder" "${opts[@]}" "${extra[@]+"${extra[@]}"}" --format junit > "$report"; code=$?
        # The report holds the details; print what passed and failed here too.
        test_cell="$(python3 - "$report" <<'PY'
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
except (ET.ParseError, OSError):
    print("no report (see the log)"); sys.exit()
cases = root.iter("testcase")
bad = 0; total = 0
for case in cases:
    total += 1
    fail = case.find("failure")
    if fail is None:
        fail = case.find("error")
    if fail is not None:
        bad += 1
        why = fail.get("message") or (fail.text or "").strip().splitlines()[0:1]
        print(f"FAIL  {case.get('name')}: {why if isinstance(why, str) else ''.join(why)}", file=sys.stderr)
    else:
        print(f"PASS  {case.get('name')}", file=sys.stderr)
print(f"{total - bad} of {total} passed")
PY
)"
        echo "$test_cell (JUnit report: $report)"
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
