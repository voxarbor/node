#!/bin/sh
# changed.sh <before> <head> <regex>: exit 0 when a path matching <regex> changed anywhere in <before>..<head>, i.e.
# in any commit of the push (not only the last one: audit #19). An unknown <before> (new branch, force push, all
# zeros, "Run workflow") counts as changed, so nothing is skipped by accident.
before=$1 head=$2 re=$3
case "$before" in ""|*[!0]*) ;; *) exit 0 ;; esac
[ -n "$before" ] && git cat-file -e "$before^{commit}" 2>/dev/null || exit 0
git diff --name-only "$before" "$head" | grep -qE "$re"
