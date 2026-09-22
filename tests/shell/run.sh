#!/usr/bin/env bash
# Runs every test_*.sh next to this file. No dependencies beyond bash.
#
#   tests/shell/run.sh            # all
#   tests/shell/run.sh resolution # only matching files
set -u

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
pattern=${1:-}
total_failed=0
total_files=0

for file in "$HERE"/test_*.sh; do
    [[ -f $file ]] || continue
    [[ -n $pattern && $file != *"$pattern"* ]] && continue
    total_files=$((total_files + 1))
    printf '\n\033[1m%s\033[0m\n' "$(basename "$file")"
    if ! bash "$file"; then
        total_failed=$((total_failed + 1))
    fi
done

echo
if (( total_files == 0 )); then
    echo "Keine Testdateien gefunden${pattern:+ für '$pattern'}."
    exit 1
fi
if (( total_failed )); then
    printf '\033[31m%d von %d Testdateien fehlgeschlagen\033[0m\n' "$total_failed" "$total_files"
    exit 1
fi
printf '\033[32malle %d Testdateien bestanden\033[0m\n' "$total_files"
