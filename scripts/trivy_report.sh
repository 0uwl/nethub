#!/usr/bin/env bash
# Report a Trivy scan's findings without failing the job.
#
#   scripts/trivy_report.sh <report.sarif> <report.txt> <label>
#
# CI scans every image it builds, but a finding there is almost always in the
# Debian base image, which NetHub cannot patch and which reaches it only
# through Dependabot's base-digest bump. Failing the build on those blocked
# every merge and release on someone else's fix, so the scans report instead
# (maintainer, 2026-10-01): the full list goes to GitHub code scanning as
# SARIF, the table goes to the job log and its summary page, and any finding
# raises a warning annotation on the run. This script is the last two of those.
#
# It counts SARIF results rather than parsing the table: the SARIF is the copy
# the Security tab shows, so the warning and the tab cannot disagree.
# SCAN_SEVERITY comes from the workflow's env block.
set -euo pipefail

if [ $# -ne 3 ]; then
    echo "usage: $0 <report.sarif> <report.txt> <label>" >&2
    exit 2
fi
sarif=$1
table=$2
label=$3
severity=${SCAN_SEVERITY:-}
summary=${GITHUB_STEP_SUMMARY:-/dev/stdout}

count=$(jq '[.runs[].results[]] | length' "$sarif")

cat "$table"

{
    echo "### ${label}: ${count} fixable ${severity} finding(s)"
    echo
    if [ "$count" -gt 0 ]; then
        echo '```'
        cat "$table"
        echo '```'
        echo
        echo "Advisory only: this does not fail the build. Each finding is also"
        echo "listed under Security > Code scanning when this run uploads SARIF."
    fi
} >> "$summary"

if [ "$count" -gt 0 ]; then
    echo "::warning title=${label}::${count} fixable ${severity} vulnerabilities found. Advisory only: see the job summary and Security > Code scanning."
fi
