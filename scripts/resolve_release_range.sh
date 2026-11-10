#!/usr/bin/env bash
# Resolve the previous release from tags that are ancestors of the selected end.
set -euo pipefail
version="$1"
input_from="${2:-}"
head_ref="${3:-HEAD}"
previous_release() {
  local end_ref="$1" tag
  while IFS= read -r tag; do
    if git merge-base --is-ancestor "$tag" "$end_ref"; then
      printf '%s' "$tag"
      return 0
    fi
  done < <(git tag --sort=-v:refname | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$')
  return 1
}
from_ref="$input_from"
to_ref="$head_ref"
if [ -z "$from_ref" ]; then
  from_ref="$(previous_release "$head_ref")"
elif [ "$from_ref" = "$version" ]; then
  to_ref="$version^"
  from_ref="$(previous_release "$to_ref")"
fi
printf 'from_ref=%s\nto_ref=%s\n' "$from_ref" "$to_ref"
