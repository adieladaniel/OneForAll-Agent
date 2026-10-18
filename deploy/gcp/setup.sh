#!/usr/bin/env bash
# One-time Google Cloud setup for the OneForAll Agent deployment (Cloud Run + Artifact Registry + Secret Manager, deployed
# from GitHub Actions through Workload Identity Federation: no service-account key is ever created).
#
# Run it once in Cloud Shell (or anywhere gcloud is logged in as a project owner):
#
#   PROJECT_ID=my-project GITHUB_REPO=my-user/oneforall-agent BILLING_ACCOUNT=012345-ABCDEF-678901 bash deploy/gcp/setup.sh
#
# Options (flags or environment variables):
#   --project ID          PROJECT_ID       Google Cloud project (required)
#   --github-repo O/N     GITHUB_REPO      GitHub repository allowed to deploy, owner/name (required)
#   --region R            REGION           default asia-south1
#   --billing-account ID  BILLING_ACCOUNT  optional; without it no budget alert is created
#   --budget N            BUDGET_AMOUNT    monthly budget in USD, default 5
#
# It is safe to run again: every step checks before it creates, and existing secrets keep their values. It prints the
# GitHub variables to set and the secrets you still have to fill in (the database URL and the Gemini key). Docs: docs/DEPLOY.md.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-asia-south1}"
GITHUB_REPO="${GITHUB_REPO:-}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
BUDGET_AMOUNT="${BUDGET_AMOUNT:-5}"

AR_REPO="oneforall"
DEPLOYER="ofa-deployer"
RUNTIME="ofa-runtime"
POOL="github"
PROVIDER="github-oidc"
# environment name : secret prefix : Cloud Run service name
ENVIRONMENTS=("staging:ofa-staging:oneforall-agent-staging" "production:ofa-prod:oneforall-agent-prod")

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project) PROJECT_ID="${2:?--project needs a value}"; shift 2 ;;
    --github-repo) GITHUB_REPO="${2:?--github-repo needs a value}"; shift 2 ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --billing-account) BILLING_ACCOUNT="${2:?--billing-account needs a value}"; shift 2 ;;
    --budget) BUDGET_AMOUNT="${2:?--budget needs a value}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -n "$PROJECT_ID" ] || die "PROJECT_ID is required (--project)"
[[ "$GITHUB_REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "GITHUB_REPO must look like owner/name (got '${GITHUB_REPO}')"
[[ "$BUDGET_AMOUNT" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "BUDGET_AMOUNT must be a number (got '${BUDGET_AMOUNT}')"
command -v gcloud > /dev/null || die "gcloud is not installed (use Cloud Shell, or see https://cloud.google.com/sdk/docs/install)"
if ! command -v python3 > /dev/null && ! command -v openssl > /dev/null; then
  die "python3 or openssl is needed to generate random secrets"
fi

# Random values without extra packages. The vault key is a Fernet key: urlsafe base64 of 32 random bytes (44 characters).
random_urlsafe() { # number of random bytes
  if command -v python3 > /dev/null; then
    python3 -c 'import secrets, sys; print(secrets.token_urlsafe(int(sys.argv[1])))' "$1"
  else
    openssl rand "$1" | base64 | tr -d '\n=' | tr '+/' '-_'
    echo
  fi
}
random_fernet_key() {
  if command -v python3 > /dev/null; then
    python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
  else
    openssl rand 32 | base64 | tr -d '\n' | tr '+/' '-_'
    echo
  fi
}
random_demo_password() { echo "Demo-$(random_urlsafe 12)"; }

# IAM changes can lag a few seconds behind the creation of the account they mention.
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if "$@"; then return 0; fi
    note "not ready yet, retrying in 5 s (attempt ${attempt}/6)"
    sleep 5
  done
  return 1
}

DEPLOYER_SA="${DEPLOYER}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME}@${PROJECT_ID}.iam.gserviceaccount.com"

say "Project ${PROJECT_ID}, region ${REGION}, GitHub repository ${GITHUB_REPO}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')" || die "cannot read project ${PROJECT_ID}"
note "project number ${PROJECT_NUMBER}"

# ---------------------------------------------------------------- APIs
say "Enabling APIs"
gcloud services enable --project "$PROJECT_ID" \
  run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com billingbudgets.googleapis.com cloudresourcemanager.googleapis.com

# ---------------------------------------------------------------- Artifact Registry
say "Artifact Registry repository '${AR_REPO}' (keeps the 3 newest images)"
if gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" --project "$PROJECT_ID" > /dev/null 2>&1; then
  note "exists"
else
  gcloud artifacts repositories create "$AR_REPO" --project "$PROJECT_ID" --location "$REGION" \
    --repository-format docker --description "OneForAll Agent images"
fi
policy_file="$(mktemp)"
cat > "$policy_file" << 'POLICY'
[
  {"name": "keep-newest-3", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-the-rest", "action": {"type": "Delete"}, "condition": {"tagState": "any"}}
]
POLICY
gcloud artifacts repositories set-cleanup-policies "$AR_REPO" --project "$PROJECT_ID" --location "$REGION" \
  --policy "$policy_file" --no-dry-run
rm -f "$policy_file"

# ---------------------------------------------------------------- service accounts
say "Service accounts"
for account in "$DEPLOYER" "$RUNTIME"; do
  if gcloud iam service-accounts describe "${account}@${PROJECT_ID}.iam.gserviceaccount.com" --project "$PROJECT_ID" > /dev/null 2>&1; then
    note "${account} exists"
  else
    gcloud iam service-accounts create "$account" --project "$PROJECT_ID" \
      --display-name "OneForAll Agent ${account#ofa-}"
  fi
done

say "Roles of ${DEPLOYER}: deploy Cloud Run, push images, act as ${RUNTIME} only"
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${DEPLOYER_SA}" \
  --role roles/run.admin --condition None --quiet > /dev/null
retry gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --project "$PROJECT_ID" --location "$REGION" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/artifactregistry.writer --quiet > /dev/null
retry gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --project "$PROJECT_ID" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/iam.serviceAccountUser --quiet > /dev/null
note "done"

# ---------------------------------------------------------------- Workload Identity Federation
say "Workload Identity Federation for GitHub Actions (only ${GITHUB_REPO} may deploy)"
if gcloud iam workload-identity-pools describe "$POOL" --location global --project "$PROJECT_ID" > /dev/null 2>&1; then
  note "pool '${POOL}' exists"
else
  gcloud iam workload-identity-pools create "$POOL" --location global --project "$PROJECT_ID" --display-name "GitHub Actions"
fi
mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref"
condition="assertion.repository == '${GITHUB_REPO}'"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool "$POOL" --location global \
  --project "$PROJECT_ID" > /dev/null 2>&1; then
  note "provider '${PROVIDER}' exists; refreshing its mapping and repository condition"
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --workload-identity-pool "$POOL" --location global \
    --project "$PROJECT_ID" --attribute-mapping "$mapping" --attribute-condition "$condition"
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --workload-identity-pool "$POOL" --location global \
    --project "$PROJECT_ID" --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "$mapping" --attribute-condition "$condition"
fi
principal="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}"
retry gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --project "$PROJECT_ID" \
  --member "$principal" --role roles/iam.workloadIdentityUser --quiet > /dev/null
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
note "done"

# ---------------------------------------------------------------- secrets
# ensure_secret NAME KIND: create the secret if missing, let the runtime account read it (and the deployer see that it
# exists), and give it a first value when KIND is generated and it has none. KIND "manual" is left for you to fill in.
PENDING=()
ensure_secret() {
  local name="$1" kind="$2" value=""
  if gcloud secrets describe "$name" --project "$PROJECT_ID" > /dev/null 2>&1; then
    note "${name}: exists"
  else
    gcloud secrets create "$name" --project "$PROJECT_ID" --replication-policy automatic --labels app=oneforall-agent > /dev/null
    note "${name}: created"
  fi
  retry gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor --quiet > /dev/null
  retry gcloud secrets add-iam-policy-binding "$name" --project "$PROJECT_ID" \
    --member "serviceAccount:${DEPLOYER_SA}" --role roles/secretmanager.viewer --quiet > /dev/null
  if [ -n "$(gcloud secrets versions list "$name" --project "$PROJECT_ID" --limit 1 --format 'value(name)')" ]; then
    return 0
  fi
  case "$kind" in
    manual) PENDING+=("$name"); return 0 ;;
    session) value="$(random_urlsafe 48)" ;;
    fernet) value="$(random_fernet_key)" ;;
    token) value="$(random_urlsafe 32)" ;;
    demo) value="$(random_demo_password)" ;;
    *) die "internal error: unknown secret kind ${kind}" ;;
  esac
  printf '%s' "$value" | gcloud secrets versions add "$name" --project "$PROJECT_ID" --data-file - > /dev/null
  note "${name}: generated a value"
}

say "Secrets"
ensure_secret "ofa-gemini-api-key" manual
for entry in "${ENVIRONMENTS[@]}"; do
  prefix="$(echo "$entry" | cut -d: -f2)"
  ensure_secret "${prefix}-database-url" manual
  ensure_secret "${prefix}-session-secret" session
  ensure_secret "${prefix}-vault-key" fernet
  ensure_secret "${prefix}-metrics-token" token
  ensure_secret "${prefix}-sim-tenant-secret" token
  ensure_secret "${prefix}-demo-password" demo
done
note "A vault key that already has a value is never replaced: data encrypted with it could not be read again."

# ---------------------------------------------------------------- budget
if [ -n "$BILLING_ACCOUNT" ]; then
  say "Budget alert: ${BUDGET_AMOUNT} USD per month for ${PROJECT_ID} (alerts at 50%, 90% and 100%)"
  budget_name="oneforall-agent ${PROJECT_ID}"
  if gcloud billing budgets list --billing-account "$BILLING_ACCOUNT" --format 'value(displayName)' 2> /dev/null | grep -Fxq "$budget_name"; then
    note "budget '${budget_name}' exists"
  else
    gcloud billing budgets create --billing-account "$BILLING_ACCOUNT" --display-name "$budget_name" \
      --budget-amount "${BUDGET_AMOUNT}USD" --filter-projects "projects/${PROJECT_NUMBER}" \
      --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 > /dev/null
    note "created (alerts go to the billing account's administrators; the amount is in USD, so the billing account must bill in USD)"
  fi
else
  say "Budget alert skipped (no BILLING_ACCOUNT). Create one in the console under Billing > Budgets & alerts."
fi

# ---------------------------------------------------------------- what to do next
say "Done. Next steps"

echo
if [ "${#PENDING[@]}" -gt 0 ]; then
  echo "1. Give these secrets their values (type or paste the value, then press Ctrl+D; nothing is echoed to the log):"
  for name in "${PENDING[@]}"; do
    echo "     gcloud secrets versions add ${name} --project ${PROJECT_ID} --data-file=-"
  done
  echo "   - *-database-url: the Neon DIRECT connection string (the host without '-pooler'), for example"
  echo "     postgresql://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/DBNAME?sslmode=require"
  echo "   - ofa-gemini-api-key: your Gemini API key (https://aistudio.google.com/apikey)"
else
  echo "1. All secrets already have a value."
fi

echo
echo "2. GitHub repository variables (Settings > Secrets and variables > Actions > Variables):"
echo "     GCP_PROJECT_ID  = ${PROJECT_ID}"
echo "     GCP_REGION      = ${REGION}"
echo "     GAR_REPOSITORY  = ${AR_REPO}"
echo "     WIF_PROVIDER    = ${WIF_PROVIDER}"
echo "     DEPLOYER_SA     = ${DEPLOYER_SA}"
echo "     RUNTIME_SA      = ${RUNTIME_SA}"
echo
echo "3. GitHub Environment variables (Settings > Environments; add required reviewers to 'production'):"
for entry in "${ENVIRONMENTS[@]}"; do
  environment="$(echo "$entry" | cut -d: -f1)"
  prefix="$(echo "$entry" | cut -d: -f2)"
  service="$(echo "$entry" | cut -d: -f3)"
  echo "     environment ${environment}: CLOUD_RUN_SERVICE = ${service}, SECRET_PREFIX = ${prefix}"
done
echo
echo "   With the GitHub CLI (gh) the same thing is:"
echo "     gh variable set GCP_PROJECT_ID --repo ${GITHUB_REPO} --body '${PROJECT_ID}'"
echo "     gh variable set GCP_REGION     --repo ${GITHUB_REPO} --body '${REGION}'"
echo "     gh variable set GAR_REPOSITORY --repo ${GITHUB_REPO} --body '${AR_REPO}'"
echo "     gh variable set WIF_PROVIDER   --repo ${GITHUB_REPO} --body '${WIF_PROVIDER}'"
echo "     gh variable set DEPLOYER_SA    --repo ${GITHUB_REPO} --body '${DEPLOYER_SA}'"
echo "     gh variable set RUNTIME_SA     --repo ${GITHUB_REPO} --body '${RUNTIME_SA}'"
for entry in "${ENVIRONMENTS[@]}"; do
  environment="$(echo "$entry" | cut -d: -f1)"
  prefix="$(echo "$entry" | cut -d: -f2)"
  service="$(echo "$entry" | cut -d: -f3)"
  echo "     gh variable set CLOUD_RUN_SERVICE --repo ${GITHUB_REPO} --env ${environment} --body '${service}'"
  echo "     gh variable set SECRET_PREFIX     --repo ${GITHUB_REPO} --env ${environment} --body '${prefix}'"
done
echo
echo "4. Run the 'Deploy' workflow manually once with seed_demo ticked (first deploy and demo users)."
echo "   The generated demo password of an environment can be read with:"
echo "     gcloud secrets versions access latest --secret ofa-staging-demo-password --project ${PROJECT_ID}"
