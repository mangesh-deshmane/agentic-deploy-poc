#!/usr/bin/env bash
# "Am I being charged right now?" - read-only inventory.
# All API calls run concurrently; output is reassembled in order.
set -uo pipefail

PROJECT_ID="${POC_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
PREFIX="apoc"
MANIFEST=".platform-state/bootstrap.json"
REGIONS="us-central1 asia-south1"

B=$(printf '\033[1m'); R=$(printf '\033[0m')
GREEN=$(printf '\033[32m'); RED=$(printf '\033[31m'); YEL=$(printf '\033[33m'); DIM=$(printf '\033[90m')

[ -n "$PROJECT_ID" ] || { echo "No project set."; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

printf '%sStatus for %s%s  %s%s%s\n' "$B" "$PROJECT_ID" "$R" "$DIM" "$(date -u +'%Y-%m-%d %H:%M UTC')" "$R"
printf '%schecking...%s' "$DIM" "$R"

# q <slot> <command...>  - run a gcloud query into a slot file, in background
q() { local slot="$1"; shift; "$@" >"$TMP/$slot" 2>/dev/null & }

# ---------- fire every query at once ----------
q run_uc   gcloud run services list --project="$PROJECT_ID" --region=us-central1 --format=value'(metadata.name,status.url,spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])'
q run_as   gcloud run services list --project="$PROJECT_ID" --region=asia-south1 --format=value'(metadata.name,status.url,spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])'
q buckets  gcloud storage buckets list --project="$PROJECT_ID" --format='value(name,softDeletePolicy.retentionDurationSeconds)'
q firestore gcloud firestore databases list --project="$PROJECT_ID" --format='value(name)'
q gke      gcloud container clusters list --project="$PROJECT_ID" --format='value(name)'
q vms      gcloud compute instances list --project="$PROJECT_ID" --format='value(name)'
q sql      gcloud sql instances list --project="$PROJECT_ID" --format='value(name)'
q ips      gcloud compute addresses list --project="$PROJECT_ID" --format='value(name,status)'
q lbs      gcloud compute forwarding-rules list --project="$PROJECT_ID" --format='value(name)'
q conn     gcloud compute networks vpc-access connectors list --project="$PROJECT_ID" --region=us-central1 --format='value(name)'
q ar       gcloud artifacts repositories list --project="$PROJECT_ID" --format='value(name)'
q apis     gcloud services list --enabled --project="$PROJECT_ID" --format='value(config.name)'
q sas      gcloud iam service-accounts list --project="$PROJECT_ID" --format='value(email)'

wait
printf '\r%s\r' "$(printf ' %.0s' {1..20})"

FOUND=0
hdr()  { printf '\n%s%s%s\n' "$B" "$*" "$R"; }
good() { printf '  %sok%s     %s\n' "$GREEN" "$R" "$*"; }
bad()  { printf '  %sBILLS%s  %s\n' "$RED" "$R" "$*"; FOUND=1; }
info() { printf '  %s-%s      %s\n' "$DIM" "$R" "$*"; }
warn() { printf '  %swarn%s   %s\n' "$YEL" "$R" "$*"; }

# empty <slot> <ok-message> <bad-prefix>
empty() {
  local slot="$1" okmsg="$2" badpfx="$3"
  if [ -s "$TMP/$slot" ]; then
    while read -r line; do [ -n "$line" ] && bad "$badpfx $line"; done <"$TMP/$slot"
  else
    good "$okmsg"
  fi
}

# ---------- free-tier services ----------
hdr "Cloud Run"
RUN_ANY=0
for r in $REGIONS; do
  slot="run_${r%%-*}"; [ "$r" = "us-central1" ] && slot=run_uc || slot=run_as
  [ -s "$TMP/$slot" ] || continue
  RUN_ANY=1
  while read -r name url min; do
    [ -z "$name" ] && continue
    min="${min:-0}"
    if [ "$min" = "0" ]; then
      good "$name ($r) scale-to-zero  $url"
    else
      bad "$name ($r) minScale=$min - billing continuously"
    fi
  done <"$TMP/$slot"
done
[ "$RUN_ANY" -eq 0 ] && info "none"

hdr "Storage"
if [ -s "$TMP/buckets" ]; then
  while read -r name sd; do
    [ -z "$name" ] && continue
    case "$name" in
      artifacts.*|*.artifacts.*|eu.artifacts.*|us.artifacts.*|asia.artifacts.*)
        bad "gs://$name - Container Registry image storage, this one does cost" ;;
      *)
        if [ -n "${sd:-}" ] && [ "$sd" != "0" ]; then
          warn "gs://$name  soft-delete ${sd}s - deleted objects still bill"
        else
          good "gs://$name  (5GB free)"
        fi ;;
    esac
  done <"$TMP/buckets"
else
  info "none"
fi

hdr "Firestore"
if [ -s "$TMP/firestore" ]; then
  while read -r d; do [ -n "$d" ] && good "${d##*/}  (1GB + 50k reads/day free)"; done <"$TMP/firestore"
else
  info "none"
fi

# ---------- must stay empty ----------
hdr "Billable resources"
empty gke  "no GKE clusters"    "GKE cluster"
empty vms  "no Compute VMs"     "VM"
empty sql  "no Cloud SQL"       "Cloud SQL"
empty ips  "no static IPs"      "static IP"
empty lbs  "no load balancers"  "load balancer (~\$18/mo)"
empty conn "no VPC connectors"  "VPC connector (~\$9/mo)"

if [ -s "$TMP/ar" ]; then
  while read -r a; do [ -n "$a" ] && warn "Artifact Registry ${a##*/} - 0.5GB free only; we use GHCR"; done <"$TMP/ar"
else
  good "no Artifact Registry repos"
fi

hdr "Image scanning"
# Only scanning bills per-use. containerregistry being enabled is harmless
# on its own - what costs is image storage, caught in the Storage section.
if grep -qx "containerscanning.googleapis.com" "$TMP/apis"; then
  bad "containerscanning enabled (~\$0.26 per image push)"
else
  good "containerscanning off"
fi

# ---------- bootstrap ----------
hdr "Bootstrap"
if [ -f "$MANIFEST" ]; then
  good "manifest present"
else
  warn "no manifest - run ./install.sh"
fi

OURS=$(grep "^${PREFIX}-" "$TMP/sas" 2>/dev/null || true)
if [ -n "$OURS" ]; then
  for s in $OURS; do info "service account $s"; done
  KEYS=0
  for s in $OURS; do
    n=$(gcloud iam service-accounts keys list --iam-account="$s" --managed-by=user \
        --project="$PROJECT_ID" --format='value(name)' 2>/dev/null | grep -c . || true)
    KEYS=$((KEYS + n))
  done
  if [ "$KEYS" -gt 0 ]; then
    warn "$KEYS user-managed key(s) exist - the design uses none"
  else
    good "no service-account keys (keyless via WIF)"
  fi
else
  info "no ${PREFIX}-* service accounts"
fi

# ---------- verdict ----------
echo
if [ "$FOUND" -eq 0 ]; then
  printf '%s%sNothing here bills. Estimated spend: $0.00/month%s\n\n' "$B" "$GREEN" "$R"
else
  printf '%s%sBillable resources found - see BILLS lines%s\n\n' "$B" "$RED" "$R"
fi
