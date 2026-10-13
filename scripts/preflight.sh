#!/usr/bin/env bash
# Checks the local workstation before you waste time on a failing push or
# deploy. Read-only: it reports and tells you the fix, it changes nothing.
#
#   ./scripts/preflight.sh
set -uo pipefail

B=$(printf '\033[1m'); R=$(printf '\033[0m')
GREEN=$(printf '\033[32m'); RED=$(printf '\033[31m'); YEL=$(printf '\033[33m'); DIM=$(printf '\033[90m')

FAIL=0; WARN=0
hdr()  { printf '\n%s%s%s\n' "$B" "$*" "$R"; }
ok()   { printf '  %sok%s    %s\n' "$GREEN" "$R" "$*"; }
bad()  { printf '  %sFAIL%s  %s\n' "$RED" "$R" "$*"; FAIL=1; }
warn() { printf '  %swarn%s  %s\n' "$YEL" "$R" "$*"; WARN=1; }
fix()  { printf '        %s%s%s\n' "$DIM" "$*" "$R"; }

cd "$(dirname "$0")/.." 2>/dev/null || true

# ---------- tooling ----------
hdr "Tooling"
need() {
  local cmd="$1" why="$2" how="$3"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd  $($cmd --version 2>/dev/null | head -1 | cut -c1-48)"
  else
    bad "$cmd missing - $why"; fix "$how"
  fi
}
need gcloud    "talks to GCP"        "pre-installed in Cloud Shell"
need terraform "builds the infra"    "see README, pin to 1.16.2"
need git       "version control"     "pre-installed"
need gh        "GitHub auth and API" "pre-installed in Cloud Shell"
need python3   "manifest parsing"    "pre-installed"

if command -v terraform >/dev/null 2>&1; then
  TFV=$(terraform version -json 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['terraform_version'])" 2>/dev/null || echo "?")
  case "$TFV" in
    1.16.*) ok "terraform $TFV matches the pin" ;;
    *) warn "terraform $TFV - CI runs 1.16.2; plans may differ between here and the pipeline" ;;
  esac
fi

# ---------- identity ----------
hdr "Identity"
if gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q .; then
  ok "gcloud: $(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
else
  bad "gcloud not authenticated"; fix "gcloud auth login"
fi

PROJ=$(gcloud config get-value project 2>/dev/null)
case "$PROJ" in
  agentic-poc-*) ok "project $PROJ" ;;
  "")  bad "no active project";  fix "gcloud config set project agentic-poc-XXXX" ;;
  *)   bad "wrong project: $PROJ"; fix "gcloud config set project agentic-poc-XXXX" ;;
esac

if [ -n "$(git config user.email 2>/dev/null)" ] && [ -n "$(git config user.name 2>/dev/null)" ]; then
  ok "git identity: $(git config user.name) <$(git config user.email)>"
else
  bad "git identity unset - commits will be refused"
  fix 'git config --global user.name "Your Name"'
  fix 'git config --global user.email "you@example.com"'
fi

if gh auth status >/dev/null 2>&1; then
  ok "gh authenticated"
  if git config --get-regexp 'credential.*helper' >/dev/null 2>&1; then
    ok "git credential helper configured"
  else
    warn "gh is logged in but git is not using it - push will ask for a password"
    fix "gh auth setup-git"
  fi
else
  bad "gh not authenticated - push over HTTPS will fail"
  fix "gh auth login    # GitHub.com -> HTTPS -> Yes -> browser"
  fix "gh auth setup-git"
fi

# ---------- repo hygiene ----------
hdr "Repository"
if [ -f .gitignore ]; then
  MISSING=""
  for pat in '.terraform/' '.platform-state/' '.env' '*.tfstate'; do
    grep -qF "$pat" .gitignore || MISSING="$MISSING $pat"
  done
  if [ -z "$MISSING" ]; then ok ".gitignore covers the important paths"
  else bad ".gitignore missing:$MISSING"; fix "see README for the full file"; fi
else
  bad "no .gitignore - provider binaries and state will be committed"
fi

# A provider binary is ~200MB and GitHub hard-rejects anything over 100MB.
BIG=$(git ls-files -z 2>/dev/null | xargs -0 -r ls -l 2>/dev/null | awk '$5 > 50000000 {print $9, int($5/1048576)"MB"}')
if [ -n "$BIG" ]; then
  bad "files over 50MB are tracked - GitHub rejects anything over 100MB"
  while read -r f; do [ -n "$f" ] && fix "$f"; done <<<"$BIG"
  fix "git rm -r --cached <path>   then commit again"
else
  ok "no oversized files tracked"
fi

if git ls-files 2>/dev/null | grep -q '^terraform/\.terraform/'; then
  bad "terraform/.terraform/ is tracked - that is a local cache"
  fix "git rm -r --cached terraform/.terraform"
else
  ok ".terraform/ not tracked"
fi

if git ls-files 2>/dev/null | grep -qE '(^|/)dockerignore$'; then
  bad "found 'dockerignore' without a leading dot - Docker ignores it"
  fix "git mv app1/dockerignore app1/.dockerignore"
elif [ -f app1/.dockerignore ]; then
  ok "app1/.dockerignore present"
fi

for f in .env '*-key.json'; do
  if git ls-files 2>/dev/null | grep -q "$f"; then
    bad "secret-looking file tracked: $f"; fix "git rm --cached $f"
  fi
done
ok "no obvious secrets tracked"

# ---------- bootstrap state ----------
hdr "Bootstrap"
if [ -f .platform-state/bootstrap.json ]; then
  ok "manifest present"
else
  warn "no manifest - run ./scripts/install.sh"
fi

for d in terraform envs app1; do
  [ -d "$d" ] && ok "$d/ present" || warn "$d/ missing"
done

# ---------- verdict ----------
echo
if [ "$FAIL" -ne 0 ]; then
  printf '%s%sFix the FAIL lines before pushing or deploying.%s\n\n' "$B" "$RED" "$R"
  exit 1
elif [ "$WARN" -ne 0 ]; then
  printf '%s%sUsable, but see the warnings.%s\n\n' "$B" "$YEL" "$R"
else
  printf '%s%sReady.%s\n\n' "$B" "$GREEN" "$R"
fi
