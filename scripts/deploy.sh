#!/usr/bin/env bash
# Safe deploy for the platform. Refuses to touch the wedding region or stack.
set -euo pipefail
REGION="us-east-1"
STACK="eventphotos-dev"
for arg in "$@"; do
  case "$arg" in
    *eu-west-1*|*wedding-photos*) echo "Refusing: eu-west-1 and the wedding-photos stack belong to the wedding." >&2; exit 1 ;;
  esac
done
cd "$(dirname "$0")/.."
sam build
sam deploy --region "$REGION" --stack-name "$STACK" "$@"
