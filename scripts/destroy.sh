#!/usr/bin/env bash
# Four-layer teardown. Terraform state is NOT trusted as the source of truth:
# a partial apply leaves resources state never recorded, and those are exactly
# the ones that keep billing.
#
#   ./destroy.sh              dry run - prints what WOULD be deleted
#   ./destroy.sh --yes        actually delete
#   ./destroy.sh --yes --nuke delete the whole project (most thorough)
#
# Deliberately NOT using `set -e`: a failure in layer 1 is precisely when
# layers 2-4 matter most. Failures are collected and reported at the end.
set -uo pipefail

DRY=1; NUKE=0
for a in "$@"; do
  case "$a" in
    --yes)  DRY=0 ;;
    --nuke) NUKE=1 ;;
    *) echo "unknown flag: $a"; exit 2 ;;
  esac
done

MANIFEST=".platform-state/bootstrap.json"
PREFIX="apoc"
B=$(printf '\033[1m'); R=$(printf '\033[0m')
GREEN=$(printf '\033[32m'); RED=$(printf '\033[31m'); YEL=$(printf '\033[33m'); DIM=$(printf '\033[90m')

FAILS=()
hdr()  { printf '\n%s%s%s\n' "$B" "$*" "$R"; }
act()  { printf '  %s->%s %s\n' "$DIM" "$R" "$*"; }
ok()   { printf '  %sgone%s   %s\n' "$GREEN" "$R" "$*"; }
err()  { printf '  %sfail%s   %s\n' "$RED" "$R" "$*"; FAILS+=("$*"); }
warn() { printf '  %swarn%s   %s\n' "$YEL" "$R" "$*"; }

# run <description> <command...>
run() {
  local desc="$1"; shift
  if [ "$DRY" -eq 1 ]; then act "would delete: $desc"; return 0; fi
  if "$@" >/dev/null 2>&1; then ok "$desc"; else err "$desc"; fi
}

# ---------- resolve target ----------
if [ -f "$MANIFEST" ]; then
  PROJECT_ID=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['project_id'])" 2>/dev/null)
  STATE_BUCKET=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['state_bucket'])" 2>/dev/null)
  POOL_ID=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['wif_pool'])" 2>/dev/null)
  PROVIDER_ID=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['wif_provider'])" 2>/dev/null)
  SA_EMAIL=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['ci_service_account'])" 2>/dev/null)
else
  warn "no manifest - falling back to the active project and name patterns"
  PROJECT_ID="${POC_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
  STATE_BUCKET=""; POOL_ID="${PREFIX}-gh-pool"; PROVIDER_ID="${PREFIX}-gh-provider"
  SA_EMAIL="${PREFIX}-ci@${PROJECT_ID}.iam.gserviceaccount.com"
fi

[ -n "${PROJECT_ID:-}" ] || { echo "Cannot determine project."; exit 1; }

# Guard: never run against a project that is not ours.
case "$PROJECT_ID" in
  agentic-poc-*) ;;
  *) echo "${RED}Refusing to destroy '$PROJECT_ID' - not an agentic-poc-* project.${R}"; exit 1 ;;
esac

ACTIVE=$(gcloud config get-value project 2>/dev/null)
if [ "$ACTIVE" != "$PROJECT_ID" ]; then
  warn "active project is '$ACTIVE' but manifest says '$PROJECT_ID'; using the manifest"
fi

printf '%sTeardown target: %s%s\n' "$B" "$PROJECT_ID" "$R"
if [ "$DRY" -eq 1 ]; then
  printf '%sDRY RUN - nothing will be deleted. Re-run with --yes to act.%s\n' "$YEL" "$R"
else
  printf '%sThis permanently deletes resources.%s\n' "$RED" "$R"
  printf 'Type the project id to confirm: '
  read -r TYPED
  [ "$TYPED" = "$PROJECT_ID" ] || { echo "Mismatch. Aborted."; exit 1; }
fi

# ---------- nuke path ----------
if [ "$NUKE" -eq 1 ]; then
  hdr "Deleting the entire project"
  warn "removes every resource, all state, and the project itself"
  warn "recoverable for 30 days via: gcloud projects undelete $PROJECT_ID"
  run "project $PROJECT_ID" gcloud projects delete "$PROJECT_ID" --quiet
  if [ "$DRY" -eq 0 ] && [ ${#FAILS[@]} -eq 0 ]; then
    rm -f "$MANIFEST"
    printf '\n%s%sProject deleted. Nothing can bill.%s\n\n' "$B" "$GREEN" "$R"
  fi
  exit 0
fi

# ---------- layer 1: terraform ----------
hdr "Layer 1 - terraform destroy"
if [ -d terraform ] && [ -d envs ]; then
  shopt -s nullglob
  TFVARS=(envs/*.tfvars)
  shopt -u nullglob
  if [ ${#TFVARS[@]} -eq 0 ]; then
    act "no envs/*.tfvars found"
  else
    for f in "${TFVARS[@]}"; do
      ENV_NAME=$(basename "$f" .tfvars)
      act "env: $ENV_NAME"
      if [ "$DRY" -eq 1 ]; then
        act "  would run terraform destroy for $ENV_NAME"
        continue
      fi
      (
        cd terraform || exit 1
        terraform init -reconfigure -upgrade=false \
          -backend-config="bucket=${STATE_BUCKET}" \
          -backend-config="prefix=state/${ENV_NAME}" >/dev/null 2>&1
        terraform destroy -auto-approve -var-file="../${f}" >/dev/null 2>&1
      )
      if [ $? -eq 0 ]; then ok "terraform destroy $ENV_NAME"; else
        err "terraform destroy $ENV_NAME (layer 2 will sweep the remains)"
        warn "  if this was a state lock: terraform force-unlock <LOCK_ID>"
      fi
    done
  fi
else
  act "no terraform/ or envs/ directory yet"
fi

# ---------- layer 2: orphan sweep ----------
# Finds resources by label/prefix rather than by state, so it catches
# anything a partial apply created and never recorded.
hdr "Layer 2 - orphan sweep"

RUN_FOUND=0
for REGION in us-central1 asia-south1; do
  SVCS=$(gcloud run services list --project="$PROJECT_ID" --region="$REGION" \
         --format="value(metadata.name)" 2>/dev/null)
  [ -n "$SVCS" ] && RUN_FOUND=1
  for s in $SVCS; do
    case "$s" in
      app1-*|app2-*|${PREFIX}-*)
        run "cloud run $s ($REGION)" \
          gcloud run services delete "$s" --project="$PROJECT_ID" --region="$REGION" --quiet ;;
      *) warn "cloud run $s ($REGION) - unrecognised name, left alone" ;;
    esac
  done
done
[ "$RUN_FOUND" -eq 0 ] && act "no cloud run services in either region"

# Firestore holds demo data only; the database itself is free and is left in
# place because deleting it blocks re-creation for a period.
FS_DOCS=$(gcloud firestore databases list --project="$PROJECT_ID" --format="value(name)" 2>/dev/null)
[ -n "$FS_DOCS" ] && act "firestore database kept (free, and recreating it is slow)"

# These should never exist. If one does, something bypassed the policy gate.
# Skip any whose API is disabled: the resource type cannot exist, and querying
# a disabled API just waits for a timeout.
ENABLED_APIS=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null)
for KIND in \
  "container.googleapis.com:container clusters:GKE cluster" \
  "compute.googleapis.com:compute instances:VM" \
  "sqladmin.googleapis.com:sql instances:Cloud SQL"
do
  API="${KIND%%:*}"; REST="${KIND#*:}"; CMD="${REST%%:*}"; LABEL="${REST##*:}"
  if ! grep -qx "$API" <<<"$ENABLED_APIS"; then
    act "$LABEL - API disabled, cannot exist"
    continue
  fi
  # shellcheck disable=SC2086
  ITEMS=$(gcloud $CMD list --project="$PROJECT_ID" --format="value(name)" 2>/dev/null)
  if [ -z "$ITEMS" ]; then
    act "no $LABEL found"
    continue
  fi
  for i in $ITEMS; do
    warn "$LABEL '$i' exists - this should have been blocked by policy"
    # shellcheck disable=SC2086
    run "$LABEL $i" gcloud $CMD delete "$i" --project="$PROJECT_ID" --quiet
  done
done

# ---------- layer 3: bootstrap ----------
hdr "Layer 3 - bootstrap teardown"

# Order matters: IAM bindings before the service account, or the bindings are
# left pointing at 'deleted:serviceAccount:...' entries.
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  if [ "$DRY" -eq 1 ]; then
    act "would remove project IAM bindings for $SA_EMAIL"
  else
    ROLES=$(gcloud projects get-iam-policy "$PROJECT_ID" \
      --flatten="bindings[].members" \
      --filter="bindings.members:serviceAccount:${SA_EMAIL}" \
      --format="value(bindings.role)" 2>/dev/null)
    for role in $ROLES; do
      gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SA_EMAIL}" --role="$role" --quiet >/dev/null 2>&1 \
        && ok "unbound $role" || err "unbind $role"
    done
  fi
  run "service account $SA_EMAIL" \
    gcloud iam service-accounts delete "$SA_EMAIL" --project="$PROJECT_ID" --quiet
else
  act "service account already gone"
fi

if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
     --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
  run "wif provider $PROVIDER_ID" \
    gcloud iam workload-identity-pools providers delete "$PROVIDER_ID" \
      --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" --quiet
else
  act "wif provider already gone"
fi

if gcloud iam workload-identity-pools describe "$POOL_ID" \
     --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  run "wif pool $POOL_ID" \
    gcloud iam workload-identity-pools delete "$POOL_ID" \
      --project="$PROJECT_ID" --location=global --quiet
  warn "pools soft-delete for 30 days; install.sh will undelete this id on re-run"
else
  act "wif pool already gone"
fi

# State bucket last - layer 1 needed it.
if [ -n "$STATE_BUCKET" ] && gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  if [ "$DRY" -eq 1 ]; then
    act "would empty and delete gs://${STATE_BUCKET}"
  else
    # Versioning is on, so every object version must go or the delete fails.
    gcloud storage rm --recursive --all-versions "gs://${STATE_BUCKET}/**" >/dev/null 2>&1
    run "bucket gs://${STATE_BUCKET}" gcloud storage buckets delete "gs://${STATE_BUCKET}" --quiet
  fi
else
  act "state bucket already gone"
fi

# ---------- layer 4: verify ----------
hdr "Layer 4 - verification"
LEFT=0
check() {
  local what="$1"; shift
  local out
  out=$("$@" 2>/dev/null)
  if [ -n "$out" ]; then
    printf '  %sstill here%s %s: %s\n' "$RED" "$R" "$what" "$(tr '\n' ' ' <<<"$out")"
    LEFT=1
  else
    printf '  %sclear%s  %s\n' "$GREEN" "$R" "$what"
  fi
}

if [ "$DRY" -eq 1 ]; then
  act "skipped in dry run"
else
  check "cloud run (us-central1)" gcloud run services list --project="$PROJECT_ID" --region=us-central1 --format="value(metadata.name)"
  check "gke clusters"  gcloud container clusters list --project="$PROJECT_ID" --format="value(name)"
  check "compute vms"   gcloud compute instances list --project="$PROJECT_ID" --format="value(name)"
  check "cloud sql"     gcloud sql instances list --project="$PROJECT_ID" --format="value(name)"
  check "static ips"    gcloud compute addresses list --project="$PROJECT_ID" --format="value(name)"
  check "service accts" bash -c "gcloud iam service-accounts list --project='$PROJECT_ID' --format='value(email)' | grep '^${PREFIX}-' || true"
fi

# ---------- verdict ----------
echo
if [ "$DRY" -eq 1 ]; then
  printf '%sDry run complete. Re-run with --yes to delete.%s\n\n' "$YEL" "$R"
  exit 0
fi

if [ ${#FAILS[@]} -gt 0 ]; then
  printf '%s%s%d step(s) failed:%s\n' "$B" "$RED" "${#FAILS[@]}" "$R"
  for f in "${FAILS[@]}"; do printf '  - %s\n' "$f"; done
  printf '\n%sRe-run ./destroy.sh --yes, or use --nuke to delete the project.%s\n\n' "$DIM" "$R"
  exit 1
fi

if [ "$LEFT" -eq 1 ]; then
  printf '%s%sResources remain - see above.%s\n\n' "$B" "$RED" "$R"
  exit 1
fi

rm -f "$MANIFEST"
printf '%s%sEverything removed. Nothing can bill.%s\n' "$B" "$GREEN" "$R"
printf '%sConfirm with ./status.sh%s\n\n' "$DIM" "$R"
