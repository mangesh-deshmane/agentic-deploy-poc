#!/usr/bin/env bash
# Wrapper so terraform always gets the right backend config and var file.
# Each environment keeps its own state prefix - one env's destroy can never
# touch another's resources.
#
#   ./tf.sh dev init
#   ./tf.sh dev plan
#   ./tf.sh dev apply
#   ./tf.sh dev output
#   ./tf.sh dev destroy
set -euo pipefail

ENV_NAME="${1:?usage: ./tf.sh <env> <terraform-command> [args]}"
shift
CMD="${1:?usage: ./tf.sh <env> <terraform-command> [args]}"
shift || true

VARS="envs/${ENV_NAME}.tfvars"
[ -f "$VARS" ] || { echo "No such env file: $VARS"; exit 1; }

MANIFEST=".platform-state/bootstrap.json"
[ -f "$MANIFEST" ] || { echo "No manifest. Run ./install.sh first."; exit 1; }
BUCKET=$(python3 -c "import json;print(json.load(open('$MANIFEST'))['state_bucket'])")

cd terraform

if [ ! -d .terraform ] || [ "$CMD" = "init" ]; then
  terraform init -reconfigure \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="prefix=state/${ENV_NAME}"
  [ "$CMD" = "init" ] && exit 0
fi

case "$CMD" in
  plan|apply|destroy)
    terraform "$CMD" -var-file="../${VARS}" "$@" ;;
  *)
    terraform "$CMD" "$@" ;;
esac
