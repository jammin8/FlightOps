#!/usr/bin/env bash


set -euo pipefail
cd "$(dirname "$0")/.."
export FLIGHTOPS_CONFIG="$PWD/tests/fixtures/site-config.json"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
swiftc -swift-version 5 -module-cache-path "$OUT/cache" \
  Sources/FlightOps/Models.swift Sources/FlightOps/Logic.swift Sources/FlightOps/StatusExport.swift Sources/FlightOps/Ingest.swift Sources/FlightOps/PublishedStatus.swift Sources/FlightOps/Supervised.swift Sources/FlightOps/LiveBest.swift Sources/FlightOps/ModelDecision.swift Sources/FlightOps/DecisionJSON.swift Sources/FlightOps/DecisionIdentity.swift Sources/FlightOps/PRStory.swift Sources/FlightOps/Summary.swift tests/logic/main.swift -o "$OUT/check"
python3 tests/best_publishable.py "$OUT/best-publishable.json"
BEST_PUBLISHABLE="$OUT/best-publishable.json" "$OUT/check"
swiftc -swift-version 5 -module-cache-path "$OUT/cache" \
  Sources/FlightOps/ModelDecision.swift Sources/FlightOps/DecisionJSON.swift Sources/FlightOps/DecisionIdentity.swift tests/decision/main.swift -o "$OUT/decision-check"
python3 tests/decision/fixtures.py "$OUT/decision" > /dev/null
"$OUT/decision-check" "$OUT/decision"
