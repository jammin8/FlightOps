#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-.build/flightops-cli}"
mkdir -p "$OUT"
swiftc -O -swift-version 5 -module-cache-path "$OUT/cache" \
  Sources/FlightOps/Models.swift Sources/FlightOps/Logic.swift Sources/FlightOps/StatusExport.swift Sources/FlightOps/Ingest.swift Sources/FlightOps/PublishedStatus.swift Sources/FlightOps/Supervised.swift Sources/FlightOps/LiveBest.swift Sources/FlightOps/ModelDecision.swift Sources/FlightOps/DecisionJSON.swift Sources/FlightOps/DecisionIdentity.swift \
  cli/main.swift -o "$OUT/flightops"
echo "$OUT/flightops"
