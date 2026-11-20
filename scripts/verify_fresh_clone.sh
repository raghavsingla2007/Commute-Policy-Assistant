#!/usr/bin/env bash
# Fresh-clone gate rehearsal. Run it AFTER committing: a clone contains only committed files, so this also
# proves that nothing the gate needs is gitignored.
#
#   bash scripts/verify_fresh_clone.sh                 # clone this repo (its committed HEAD)
#   bash scripts/verify_fresh_clone.sh <git-url>       # or clone from a remote
#   NO_CACHE=1 bash scripts/verify_fresh_clone.sh      # also rebuild every image layer (slow: downloads again)
#   CLONE_PARENT=/some/dir bash ...                    # where the temp clone goes (default: $HOME; Colima only
#                                                      # shares your home folder with its VM, see README)
#
# Steps, each timed: clone, build, `docker compose up -d` until /health is ready, pytest (online), pytest with no
# network, the FakeLLM eval, the deliverable check, the docs-number check. Prints a PASS/FAIL table and cleans
# up (containers, the clone's volume, the temp folder). Needs: git, docker with compose, curl.
# Port 8000 must be free: stop the main stack first (docker compose stop).
set -uo pipefail

SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
PARENT="${CLONE_PARENT:-$HOME}"
DIR=$(mktemp -d "$PARENT/movein-fresh-clone.XXXXXX")
PROJECT="freshclone$$"                     # own compose project: own network and volume, nothing shared
RESULTS=()
FAILED=0
T0=$(date +%s)

cleanup() {
  if [[ -f "$DIR/repo/docker-compose.yml" ]]; then
    (cd "$DIR/repo" && docker compose -p "$PROJECT" down -v >/dev/null 2>&1)
  fi
  rm -rf "$DIR"
}
trap cleanup EXIT

run_step() {   # run_step <name> <command...>; output goes to $DIR/<n>.log
  local name="$1"; shift
  local n=${#RESULTS[@]}
  local t=$(date +%s)
  printf '%-34s ' "$name"
  if "$@" >"$DIR/$n.log" 2>&1; then status=PASS; else status=FAIL; FAILED=1; fi
  local secs=$(( $(date +%s) - t ))
  printf '%s  %4ss\n' "$status" "$secs"
  RESULTS+=("$(printf '%-34s %-5s %5ss' "$name" "$status" "$secs")")
  if [[ $status == FAIL ]]; then echo "---- last lines of $name ----"; tail -20 "$DIR/$n.log"; echo "----"; fi
  [[ $status == PASS ]]
}

if curl -s -o /dev/null --max-time 2 http://localhost:8000/health; then
  echo "Port 8000 is in use (is the main stack running? docker compose stop). Aborting."; exit 2
fi

echo "Fresh clone of $SRC into $DIR/repo"
run_step "git clone" git clone --quiet "$SRC" "$DIR/repo" || { echo "clone failed"; exit 1; }
cd "$DIR/repo"
echo "commit: $(git log -1 --format='%h %s')"
for f in PROJECT_LOG.md VIVA_PREP.md .env; do [[ -e $f ]] && echo "WARNING: $f is in the clone (should be ignored)"; done

BUILD_ARGS=(); [[ "${NO_CACHE:-0}" == "1" ]] && BUILD_ARGS=(--no-cache)
run_step "docker compose build" docker compose -p "$PROJECT" build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}
run_step "docker compose up -d" docker compose -p "$PROJECT" up -d

wait_health() {
  for _ in $(seq 1 120); do
    body=$(curl -s --max-time 2 http://localhost:8000/health) && grep -q '"ready":true' <<<"$body" && { echo "$body"; return 0; }
    sleep 1
  done
  return 1
}
run_step "/health ready" wait_health && echo "  $(cat "$DIR/$(( ${#RESULTS[@]} - 1 )).log")"

run_step "pytest (online)" docker compose -p "$PROJECT" run --rm api pytest -q
tail -1 "$DIR/$(( ${#RESULTS[@]} - 1 )).log" | sed 's/^/  /'
run_step "pytest (--network none)" docker run --rm --network none commute-policy-assistant:dev pytest -q
tail -1 "$DIR/$(( ${#RESULTS[@]} - 1 )).log" | sed 's/^/  /'
run_step "eval (FakeLLM, exit 0)" docker compose -p "$PROJECT" run --rm api python -m eval.run_eval --quiet
run_step "eval output on the host" test -s eval/results/fake_vector_all.summary.json

check_deliverable() {
  docker compose -p "$PROJECT" run --rm api python - <<'EOF'
import json
lines = open("eval/eval_set.jsonl", encoding="utf-8").read().splitlines()
recs = [json.loads(x) for x in lines]
assert len(recs) == 55, len(recs)
assert all(list(r) == ["id", "input", "expected", "actual", "pass", "notes"] for r in recs)
from eval.freeze import heldout_sha256
import re
assert heldout_sha256() == re.findall(r"HELDOUT sha256=([0-9a-f]{64})", open("eval/FREEZE.md").read())[-1]
print("eval_set.jsonl: 55 records, six fields; HELD-OUT hash matches eval/FREEZE.md")
EOF
}
run_step "deliverable + freeze hash" check_deliverable
run_step "docs numbers match results" docker compose -p "$PROJECT" run --rm api python -m scripts.check_docs_numbers

echo
echo "================ fresh-clone gate ================"
printf '%s\n' "${RESULTS[@]}"
echo "--------------------------------------------------"
printf '%-34s %-5s %5ss\n' "TOTAL (wall clock)" "$([[ $FAILED == 0 ]] && echo PASS || echo FAIL)" "$(( $(date +%s) - T0 ))"
exit $FAILED
