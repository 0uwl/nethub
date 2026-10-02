#!/usr/bin/env bash
# Report a Trivy SARIF file's findings without failing the job.
#
#   scripts/trivy_report.sh <report.sarif> <label>
#
# Called by .github/actions/trivy-scan after the scan. CI scans every image it
# builds, but nearly every finding is in the Debian base image, which NetHub
# cannot patch and which reaches it only through Dependabot's base-digest
# bump. Failing the build on those blocked every merge and release on someone
# else's fix, so the scans report instead: the SARIF
# goes to GitHub code scanning, and this script writes the findings to the job
# summary and raises a warning annotation on the run.
#
# Everything is read from the one SARIF file, the copy the Security tab shows,
# so the warning, the summary and the tab cannot disagree. Trivy writes each
# result's message as "Key: value" lines (Package, Installed Version, Fixed
# Version, Severity); a line that does not parse leaves its cell blank rather
# than failing the report. A missing or unreadable file does fail: a scanner
# that did not run must not read as zero findings. SCAN_SEVERITY comes from
# the workflow's env block.
set -euo pipefail

if [ $# -ne 2 ]; then
    echo "usage: $0 <report.sarif> <label>" >&2
    exit 2
fi
sarif=$1
label=$2
severity=${SCAN_SEVERITY:-}
summary=${GITHUB_STEP_SUMMARY:-/dev/stdout}

count=$(jq '[.runs[].results[]] | length' "$sarif")
rows=$(jq -r '
    .runs[] | .results[]
    | (.message.text // "" | split("\n")
       | map(capture("^(?<key>[^:]+): (?<value>.*)$")?) | from_entries) as $m
    | "| \(.ruleId) | \($m.Package // "") | \($m["Installed Version"] // "")"
      + " | \($m["Fixed Version"] // "") | \($m.Severity // "") |"
' "$sarif")

{
    echo "### ${label}: ${count} fixable ${severity} finding(s)"
    echo
    if [ "$count" -gt 0 ]; then
        echo "| Vulnerability | Package | Installed | Fixed in | Severity |"
        echo "|---|---|---|---|---|"
        echo "$rows"
        echo
        echo "Advisory only: this does not fail the build. A finding in the"
        echo "Debian base is fixed by Dependabot's base-image digest bump; one in"
        echo "a pinned Python package is fixed by bumping the pin."
    fi
} >> "$summary"

if [ "$count" -gt 0 ]; then
    echo "$rows"
    echo "::warning title=${label}::${count} fixable ${severity} vulnerabilities found. Advisory only: see the job summary and Security > Code scanning."
else
    echo "No fixable ${severity} vulnerabilities."
fi
