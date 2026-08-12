#!/usr/bin/env bash
# Refresh evaluator/vendor/ from the upstream library source trees.
#
# Only needed if you maintain the upstream libraries locally and want to pull
# in changes. The sample ships with the vendored code already in place.
#
# Usage:
#   scripts/sync_vendor.sh [OTEL_SRC_ROOT] [CONTRACTS_SRC_ROOT]
#
# Defaults assume the library repos sit next to the sample checkout:
#   ../agentic-otel                      (agentic_otel package)
#   ../agentic-behavioral-contracts     (agent_validator package)
set -euo pipefail

SAMPLE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="$SAMPLE_ROOT/evaluator/vendor"

OTEL_ROOT="${1:-$SAMPLE_ROOT/../agentic-otel}"
CONTRACTS_ROOT="${2:-$SAMPLE_ROOT/../agentic-behavioral-contracts}"

for root in "$OTEL_ROOT" "$CONTRACTS_ROOT"; do
  if [ ! -d "$root" ]; then
    echo "error: upstream source tree not found: $root" >&2
    echo "Pass the source roots explicitly: scripts/sync_vendor.sh <otel-root> <contracts-root>" >&2
    exit 1
  fi
done

rsync -a --delete --exclude __pycache__ \
  "$OTEL_ROOT/src/agentic_otel/" "$VENDOR_DIR/agentic_otel/"
rsync -a --delete --exclude __pycache__ \
  "$CONTRACTS_ROOT/src/agent_validator/" "$VENDOR_DIR/agent_validator/"
cp "$CONTRACTS_ROOT/LICENSE" "$VENDOR_DIR/LICENSE"
# Combined NOTICE for both vendored libraries
{
  echo "agentic-behavioral-contracts"
  echo "agentic-otel"
  echo "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
} > "$VENDOR_DIR/NOTICE"

echo "Vendored:"
echo "  agentic_otel     <- $OTEL_ROOT"
echo "  agent_validator  <- $CONTRACTS_ROOT"
find "$VENDOR_DIR" -name '*.py' | wc -l | xargs echo "  python files:"
