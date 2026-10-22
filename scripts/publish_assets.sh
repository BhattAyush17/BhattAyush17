#!/usr/bin/env bash
#
# publish_assets.sh
#
# Commit generated assets and publish them on top of the newest main.
#
# Usage:
#   scripts/publish_assets.sh "<commit message>" [file ...]
#
# Why this exists:
#
# The asset workflows (snake, stats, streak) all push to the same
# branch and share one concurrency group, so a push can race with a
# commit made by another workflow.
#
# The old sequence was:
#
#   1. generate files          -> working tree is dirty
#   2. git pull --rebase       -> ABORTS:
#        error: cannot pull with rebase: You have unstaged changes.
#   3. commit + push           -> never reached
#
# That is why the snake animation and the streak card stayed stale for
# weeks: the workflow failed before it could commit anything.
#
# The fix is to commit FIRST and rebase AFTERWARDS, when the working
# tree is clean again.
#
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <commit message> [file ...]" >&2
  exit 2
fi

message="$1"
shift

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Drop paths that do not exist (e.g. the cache on a cold run), otherwise
# `git add` aborts with "pathspec did not match any files".
paths=()

for path in "$@"; do
  if [ -e "$path" ]; then
    paths+=("$path")
  else
    echo "Skipping missing path: ${path}"
  fi
done

if [ "${#paths[@]}" -gt 0 ]; then
  git add -- "${paths[@]}"
else
  git add -A
fi

# Stage anything else the run generated.
#
# The path list above is a convenience, not a contract: a workflow that
# forgets to list a generated file leaves it unstaged, and the rebase
# further down then aborts with
# "cannot rebase: You have unstaged changes".
git add -A

if git diff --cached --quiet; then
  echo "No changes to publish."
  exit 0
fi

git commit -m "$message"

# Publish on top of the newest main.
git fetch origin main

if ! git rebase origin/main; then
  echo "::warning::Rebase conflict - keeping the freshly generated files"
  git rebase --abort 2>/dev/null || true
  git reset --soft origin/main
  if [ "${#paths[@]}" -gt 0 ]; then
    git add -- "${paths[@]}"
  else
    git add -A
  fi
  git add -A
  git commit -m "$message"
fi

git push origin HEAD:main

echo "Published: ${message}"
