#!/bin/sh
# POSIX sh: runs inside the minimal Prometheus image (promtool/sh, no Python).
# Only run after validate-archive on the private scratch PVC. Never source scratch.
set -eu
trap '[ "$?" -eq 0 ] || echo "stage=read-samples result=FAIL" >&2' EXIT
cd /scratch
read -r seconds <snapshot-seconds
case $seconds in ''|0*|*[!0-9]*|???????????*) exit 1 ;; esac
# kube-state-metrics node info is an info gauge (=1), used by this stack's
# inventory alerts. Require a real sample within 24h before Restic snapshot
# time, not an exact comparison against an independently captured source value.
# Drain the dump; a sample followed by a read error must not pass (POSIX sh).
{ /bin/promtool tsdb dump --match='kube_node_info{node!=""}' \
      --min-time="$((seconds - 86400))000" --max-time="${seconds}000" ./tsdb 2>/dev/null && echo dump-complete; } |
    awk '/} 1([.]0+)? [0-9]+$/ { sample=1 }
         { complete=($0 == "dump-complete") } END { exit !(sample && complete) }'
echo 'stage=read-samples result=PASS'
