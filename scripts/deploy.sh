#!/usr/bin/env bash
# Safe deploy for the platform. Refuses to touch the wedding region or stack.
set -euo pipefail
REGION="us-east-1"
STACK="eventphotos-dev"
SAM="$(command -v sam || command -v sam.cmd || true)"
if [ -z "$SAM" ]; then
  echo "SAM CLI not found. Install it or add its bin folder to PATH." >&2; exit 1
fi
for arg in "$@"; do
  case "$arg" in
    *eu-west-1*|*wedding-photos*) echo "Refusing: eu-west-1 and the wedding-photos stack belong to the wedding." >&2; exit 1 ;;
  esac
done
cd "$(dirname "$0")/.."
"$SAM" build
"$SAM" deploy --region "$REGION" --stack-name "$STACK" "$@"
