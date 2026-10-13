#!/usr/bin/env bash
# Phase 0 bootstrap for the agentic deploy PoC (GCP side).
# Idempotent: safe to run repeatedly. Creates only always-free resources.
set -euo pipefail

# ---------- config ----------
PROJECT_ID="${POC_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${POC_REGION:-us-central1}"
# nam5 = US multi-region. Firestore's free quota applies per database; keeping
# it in the US matches the state bucket and the Cloud Run region.
FIRESTORE_LOCATION="${POC_FIRESTORE_LOCATION:-nam5}"
GITHUB_REPO="${POC_GITHUB_REPO:-mangesh-deshmane/agentic-deploy-poc}"
PREFIX="apoc"

SUFFIX="${PROJECT_ID##*-}"
STATE_BUCKET="${PREFIX}-tfstate-${SUFFIX}"
POOL_ID="${PREFIX}-gh-pool"
PROVIDER_ID="${PREFIX}-gh-provider"
SA_NAME="${PREFIX}-ci"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
MANIFEST_DIR=".platform-state"
MANIFEST="${MANIFEST_DIR}/bootstrap.json"

APIS=(
  run.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
  cloudresourcemanager.googleapis.com
  storage.googleapis.com
  firestore.googleapis.com
  serviceusage.googleapis.com
  billingbudgets.googleapis.com
)

# Scanning is the only API here that bills per use (~$0.26 per image push).
# containerregistry on its own costs nothing - it only matters if images are
# stored, which shows up as artifacts.* GCS buckets. status.sh checks for those.
FORBIDDEN_APIS=(
  containerscanning.googleapis.com
)

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
# IAM policy writes are read-modify-write against a shared ETag. Anything else
# touching the project policy at the same moment - Firestore provisioning its
# service agent, for instance - makes the write fail. Retry with backoff.
retry() {
  local n=0 max=5 delay=2
  until "$@" >/dev/null 2>&1; do
    n=$((n + 1))
    if [ "$n" -ge "$max" ]; then return 1; fi
    sleep "$delay"
    delay=$((delay * 2))
  done
  return 0
}
ok()   { printf '    \033[32mok\033[0m    %s\n' "$*"; }
skip() { printf '    \033[90mexists\033[0m %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m  %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- preflight ----------
say "Preflight"
[ -n "$PROJECT_ID" ] || die "No project set. Run: gcloud config set project <id>"
case "$PROJECT_ID" in
  agentic-poc-*) ;;
  *) die "Refusing to run against '$PROJECT_ID' - expected a project named agentic-poc-*" ;;
esac
ok "project $PROJECT_ID"

gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q . \
  || die "Not authenticated. Run: gcloud auth login"

BILLING_ENABLED=$(gcloud billing projects describe "$PROJECT_ID" --format="value(billingEnabled)" 2>/dev/null || echo "False")
[ "$BILLING_ENABLED" = "True" ] || die "Billing not enabled on $PROJECT_ID"
ok "billing enabled"

BILLING_ID=$(gcloud billing projects describe "$PROJECT_ID" --format="value(billingAccountName)" 2>/dev/null | sed 's|billingAccounts/||')
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
ok "project number $PROJECT_NUMBER"

# ---------- APIs ----------
say "Enabling APIs"
ENABLED=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)")
TO_ENABLE=()
for api in "${APIS[@]}"; do
  if grep -qx "$api" <<<"$ENABLED"; then skip "$api"; else TO_ENABLE+=("$api"); fi
done
if [ ${#TO_ENABLE[@]} -gt 0 ]; then
  gcloud services enable "${TO_ENABLE[@]}" --project="$PROJECT_ID"
  for api in "${TO_ENABLE[@]}"; do ok "$api"; done
fi

say "Checking forbidden APIs are off"
for api in "${FORBIDDEN_APIS[@]}"; do
  if grep -qx "$api" <<<"$ENABLED"; then
    warn "$api is ENABLED - this bills per image scan. Disable with:"
    warn "  gcloud services disable $api --project=$PROJECT_ID"
  else
    ok "$api off"
  fi
done

# ---------- Terraform state bucket ----------
say "Terraform state bucket"
if gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  skip "gs://${STATE_BUCKET}"
else
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="$PROJECT_ID" \
    --location="US" \
    --uniform-bucket-level-access \
    --public-access-prevention
  ok "created gs://${STATE_BUCKET} (US multi-region, inside the 5GB free tier)"
fi

gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning >/dev/null
ok "versioning on"

# Soft delete retains deleted objects for 7 days AND BILLS FOR THEM.
# Off by default is what we want; the flag name has changed across gcloud versions.
if gcloud storage buckets update "gs://${STATE_BUCKET}" --clear-soft-delete >/dev/null 2>&1; then
  ok "soft-delete retention cleared"
elif gcloud storage buckets update "gs://${STATE_BUCKET}" --soft-delete-duration=0 >/dev/null 2>&1; then
  ok "soft-delete retention set to 0"
else
  warn "could not clear soft-delete. Check manually:"
  warn "  gcloud storage buckets describe gs://${STATE_BUCKET} --format='value(softDeletePolicy)'"
fi

# ---------- Firestore database ----------
# One database per project, shared by every environment - each customer+env
# gets its own COLLECTION inside it, not its own database. Deliberately not
# Terraform-managed: a per-environment destroy would otherwise wipe every
# other environment's data. Same reasoning as the state bucket.
say "Firestore database"
if gcloud firestore databases describe --database='(default)' \
     --project="$PROJECT_ID" >/dev/null 2>&1; then
  skip "(default) database"
else
  if gcloud firestore databases create \
       --database='(default)' \
       --location="$FIRESTORE_LOCATION" \
       --type=firestore-native \
       --project="$PROJECT_ID" >/dev/null 2>&1; then
    ok "created (default) in ${FIRESTORE_LOCATION} (1GiB + 50k reads/day free)"
  else
    warn "could not create the Firestore database. If it was deleted recently,"
    warn "the name is reserved for a period. Check with:"
    warn "  gcloud firestore databases list --project=$PROJECT_ID"
  fi
fi

# ---------- CI service account ----------
say "CI service account"
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  skip "$SA_EMAIL"
else
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="Agentic PoC CI" \
    --description="Used by GitHub Actions via Workload Identity Federation. No keys."
  ok "created $SA_EMAIL"
fi

# Project-level roles. Deliberately minimal.
#
# Notably ABSENT:
#   resourcemanager.projectIamAdmin - could grant itself owner
#   iam.serviceAccountAdmin         - could mint new identities
#   datastore.owner                 - CI has no business reading app data
#
# serviceAccountUser is granted per runtime identity below, not project-wide,
# so CI can deploy AS those identities and no others.
ROLES=(
  roles/run.admin
  roles/serviceusage.serviceUsageConsumer
)
for role in "${ROLES[@]}"; do
  if retry gcloud projects add-iam-policy-binding "$PROJECT_ID" \
       --member="serviceAccount:${SA_EMAIL}" \
       --role="$role" \
       --condition=None; then
    ok "$role"
  else
    die "could not bind $role after retries. Re-run ./install.sh - it is idempotent."
  fi
done

# State bucket access is bucket-scoped, not project-scoped.
if retry gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/storage.objectAdmin"; then
  ok "roles/storage.objectAdmin on gs://${STATE_BUCKET} only"
else
  die "could not grant bucket access after retries. Re-run ./install.sh."
fi

# ---------- runtime identities ----------
# One per app, created here rather than by Terraform. Terraform would need
# project-level IAM admin to grant datastore.user, and that role can grant ANY
# role to ANY principal - a CI identity holding it could make itself owner.
#
# Keeping these out of Terraform means the CI service account never needs to
# write project IAM at all. Per-environment isolation is at the Firestore
# collection level (app-customer-env-items), which the app enforces itself.
say "Runtime identities"
for app in app1 app2; do
  RT_NAME="${PREFIX}-${app}-runtime"
  RT_EMAIL="${RT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

  if gcloud iam service-accounts describe "$RT_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
    skip "$RT_EMAIL"
  else
    gcloud iam service-accounts create "$RT_NAME" \
      --project="$PROJECT_ID" \
      --display-name="Runtime identity for ${app}" \
      --description="What the ${app} container runs as. Managed by agentic-poc."
    ok "created $RT_EMAIL"
  fi

  if retry gcloud projects add-iam-policy-binding "$PROJECT_ID" \
       --member="serviceAccount:${RT_EMAIL}" \
       --role="roles/datastore.user" \
       --condition=None; then
    ok "roles/datastore.user -> ${app} runtime"
  else
    die "could not grant datastore.user to $RT_EMAIL. Re-run ./install.sh."
  fi

  # CI must be able to run services AS this identity, but cannot change what
  # the identity is allowed to do.
  if retry gcloud iam service-accounts add-iam-policy-binding "$RT_EMAIL" \
       --project="$PROJECT_ID" \
       --member="serviceAccount:${SA_EMAIL}" \
       --role="roles/iam.serviceAccountUser"; then
    ok "CI may deploy as ${app} runtime"
  else
    die "could not let CI use $RT_EMAIL. Re-run ./install.sh."
  fi
done

# ---------- Workload Identity Federation ----------
say "Workload Identity Federation"
if gcloud iam workload-identity-pools describe "$POOL_ID" \
     --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  STATE=$(gcloud iam workload-identity-pools describe "$POOL_ID" \
            --project="$PROJECT_ID" --location=global --format="value(state)")
  if [ "$STATE" = "DELETED" ]; then
    warn "pool $POOL_ID is soft-deleted; undeleting (30-day window)"
    gcloud iam workload-identity-pools undelete "$POOL_ID" \
      --project="$PROJECT_ID" --location=global
    ok "undeleted $POOL_ID"
  else
    skip "pool $POOL_ID"
  fi
else
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --project="$PROJECT_ID" --location=global \
    --display-name="Agentic PoC GitHub"
  ok "created pool $POOL_ID"
fi

POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}"

# Deleting a pool soft-deletes its providers too, and `describe` still returns
# a deleted provider - so an existence check alone silently leaves federation
# broken, and the only symptom is an "invalid audience" error at CI time.
if gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
     --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
  PSTATE=$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
             --project="$PROJECT_ID" --location=global \
             --workload-identity-pool="$POOL_ID" --format="value(state)")
  if [ "$PSTATE" = "DELETED" ]; then
    warn "provider $PROVIDER_ID is soft-deleted; undeleting (30-day window)"
    gcloud iam workload-identity-pools providers undelete "$PROVIDER_ID" \
      --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID"
    ok "undeleted $PROVIDER_ID"
  else
    skip "provider $PROVIDER_ID"
  fi
else
  # The attribute-condition is the security boundary. Without it, ANY GitHub
  # repository on earth can impersonate this service account.
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --project="$PROJECT_ID" --location=global \
    --workload-identity-pool="$POOL_ID" \
    --display-name="GitHub Actions" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}'"
  ok "created provider $PROVIDER_ID, locked to ${GITHUB_REPO}"
fi

if retry gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
     --project="$PROJECT_ID" \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.repository/${GITHUB_REPO}"; then
  ok "bound ${GITHUB_REPO} -> ${SA_EMAIL}"
else
  die "could not bind the federated identity after retries. Re-run ./install.sh."
fi

# Federation is the one thing here whose breakage only shows up later, in CI,
# as an opaque "invalid audience". Assert it is actually usable now.
POOL_STATE=$(gcloud iam workload-identity-pools describe "$POOL_ID" \
  --project="$PROJECT_ID" --location=global --format="value(state)" 2>/dev/null)
PROV_STATE=$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --project="$PROJECT_ID" --location=global \
  --workload-identity-pool="$POOL_ID" --format="value(state)" 2>/dev/null)

if [ "$POOL_STATE" = "ACTIVE" ] && [ "$PROV_STATE" = "ACTIVE" ]; then
  ok "federation verified: pool and provider both ACTIVE"
else
  die "federation is not usable - pool=${POOL_STATE:-missing} provider=${PROV_STATE:-missing}. CI auth will fail with 'invalid audience'."
fi

# ---------- budget tripwire ----------
say "Budget alert"
if [ -z "$BILLING_ID" ]; then
  warn "no billing account id resolved; skipping budget"
elif gcloud billing budgets list --billing-account="$BILLING_ID" --format="value(displayName)" 2>/dev/null \
     | grep -qx "agentic-poc-tripwire"; then
  skip "budget agentic-poc-tripwire"
else
  # Account-wide, not project-filtered: --filter-projects is rejected by some
  # gcloud versions, and an account-wide tripwire catches spend anywhere.
  if gcloud billing budgets create \
       --billing-account="$BILLING_ID" \
       --display-name="agentic-poc-tripwire" \
       --budget-amount=1USD \
       --threshold-rule=percent=0.5 \
       --threshold-rule=percent=1.0 >/dev/null 2>&1; then
    ok "budget alert at \$1 across the billing account (50% and 100%)"
  else
    warn "gcloud refused to create the budget. Known to fail on some accounts."
    warn "Create it by hand (2 min): console.cloud.google.com/billing"
    warn "  -> Budgets & alerts -> Create budget"
    warn "  -> name agentic-poc-tripwire, amount \$1, thresholds 50% and 100%"
    warn "Not a blocker: this is detection. The policy gate prevents spend."
  fi
fi

# ---------- manifest ----------
say "Writing manifest"
mkdir -p "$MANIFEST_DIR"
cat > "$MANIFEST" <<JSON
{
  "created_at":      "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "project_id":      "${PROJECT_ID}",
  "project_number":  "${PROJECT_NUMBER}",
  "billing_account": "${BILLING_ID}",
  "region":          "${REGION}",
  "firestore_location": "${FIRESTORE_LOCATION}",
  "prefix":          "${PREFIX}",
  "github_repo":     "${GITHUB_REPO}",
  "state_bucket":    "${STATE_BUCKET}",
  "wif_pool":        "${POOL_ID}",
  "wif_provider":    "${PROVIDER_ID}",
  "wif_pool_name":   "${POOL_NAME}",
  "ci_service_account": "${SA_EMAIL}"
}
JSON
ok "$MANIFEST"

# ---------- summary ----------
cat <<SUMMARY

$(printf '\033[1m')Bootstrap complete.$(printf '\033[0m')

  Project           ${PROJECT_ID}
  State bucket      gs://${STATE_BUCKET}
  CI identity       ${SA_EMAIL}
  Federated from    ${GITHUB_REPO}

  No service-account keys were created. Nothing here costs money.

$(printf '\033[1m')GitHub Actions secrets$(printf '\033[0m') - add these as repository variables:

  GCP_PROJECT_ID       ${PROJECT_ID}
  GCP_WIF_PROVIDER     ${POOL_NAME}/providers/${PROVIDER_ID}
  GCP_CI_SA            ${SA_EMAIL}
  GCP_STATE_BUCKET     ${STATE_BUCKET}

$(printf '\033[1m')Runtime identities$(printf '\033[0m') - put the matching one in each envs/*.tfvars:

  app1   ${PREFIX}-app1-runtime@${PROJECT_ID}.iam.gserviceaccount.com
  app2   ${PREFIX}-app2-runtime@${PROJECT_ID}.iam.gserviceaccount.com

  CI can deploy as these, but cannot change what they are allowed to do.

Next:  ./status.sh
SUMMARY
