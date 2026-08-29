#!/usr/bin/env bash
# Generate a new CHANGELOG.md section and prepend it to the file.
#
# Usage:
#   generate-changelog.sh <NEW_VERSION> <CURRENT_VERSION> <REPO> <CHANGELOG_FILE>
#
# Arguments:
#   NEW_VERSION      - The version being released (e.g. 0.2.0)
#   CURRENT_VERSION  - The previous version (e.g. 0.1.0)
#   REPO             - GitHub repository in owner/name format (e.g. sanmibuh/trade-republic-bridge)
#   CHANGELOG_FILE   - Path to the CHANGELOG.md file to update
#
# The script expects a git tag "v${CURRENT_VERSION}" to exist. If it is missing
# (e.g. the very first release before any tag), the section is created with a
# placeholder for release notes instead of failing.

set -euo pipefail

NEW="$1"
CURRENT="$2"
REPO="$3"
CHANGELOG_FILE="$4"
BASE_URL="https://github.com/${REPO}"
TODAY=$(date -u +%Y-%m-%d)

ITEMS=""
if git tag --list "v${CURRENT}" | grep -q .; then
    # Build "* title — [#N](url)" lines from commits since the last tag.
    while IFS= read -r subject; do
        [ -z "$subject" ] && continue
        if echo "$subject" | grep -qE '\(#[0-9]+\)$'; then
            PR_NUM=$(echo "$subject" | grep -oE '#[0-9]+' | tail -1 | tr -d '#')
            TITLE=$(echo "$subject" | sed -E 's/ \(#[0-9]+\)$//')
            ITEMS="${ITEMS}* ${TITLE} — [#${PR_NUM}](${BASE_URL}/pull/${PR_NUM})\n"
        else
            ITEMS="${ITEMS}* ${subject}\n"
        fi
    done < <(git log "v${CURRENT}..HEAD" --pretty=format:"%s")
    COMPARE="${BASE_URL}/compare/v${CURRENT}...v${NEW}"
else
    echo "WARN: tag v${CURRENT} not found — emitting placeholder release notes" >&2
    COMPARE="${BASE_URL}/commits/v${NEW}"
fi

if [ -z "$ITEMS" ]; then
    ITEMS="<!-- add release notes here -->\n"
fi

FULL_CHANGELOG="**Full Changelog**: ${COMPARE}"

# Write the new section to a temp file, then splice it in right after the
# "The format is based on" line. Reading the section from a file (rather than
# passing it as an awk variable) keeps this portable across BSD and GNU awk.
SECTION_FILE=$(mktemp "${TMPDIR:-/tmp}/changelog.XXXXXX")
trap 'rm -f "$SECTION_FILE"' EXIT
{
    printf '## [%s] - %s\n\n### What'\''s Changed\n' "$NEW" "$TODAY"
    printf '%b' "$ITEMS"
    printf '%s\n\n' "$FULL_CHANGELOG"
} > "$SECTION_FILE"

# Fail fast if the anchor line is missing: otherwise awk would silently leave the
# changelog unchanged and still exit 0, publishing a release with no new section.
if ! grep -q '^The format is based on' "$CHANGELOG_FILE"; then
    echo "ERROR: anchor line 'The format is based on' not found in ${CHANGELOG_FILE}" >&2
    exit 1
fi

awk -v sectionfile="$SECTION_FILE" '
    /^The format is based on/ && !inserted {
        print
        print ""
        while ((getline line < sectionfile) > 0) print line
        inserted=1
        next
    }
    { print }
' "$CHANGELOG_FILE" > "${CHANGELOG_FILE}.tmp" && mv "${CHANGELOG_FILE}.tmp" "$CHANGELOG_FILE"
