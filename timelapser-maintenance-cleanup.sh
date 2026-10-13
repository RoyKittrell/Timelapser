#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
TEST_SHOT_DIRS=(
    "$PROJECT_ROOT/control_v6/test_shots"
    "$PROJECT_ROOT/director_test_shots"
)

removed=0
for directory in "${TEST_SHOT_DIRS[@]}"; do
    [[ -d "$directory" ]] || continue
    count="$(find "$directory" -mindepth 1 -print | wc -l)"
    find "$directory" -mindepth 1 -delete
    removed=$((removed + count))
done

printf '%s  CLEANUP: removed %d transient test-shot entries\n' \
    "$(date --iso-8601=seconds)" "$removed"
