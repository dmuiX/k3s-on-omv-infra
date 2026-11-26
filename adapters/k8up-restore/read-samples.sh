#!/bin/sh
# POSIX sh: runs inside the minimal Prometheus image (promtool/sh, no Python).
# Only run after validate-archive on the private scratch PVC. Never source scratch.
set -eu
server=
cleanup() {
    result=$?
    if [ -n "$server" ]; then
        kill "$server" 2>/dev/null || :
        for attempt in 1 2 3 4 5; do
            kill -0 "$server" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$server" 2>/dev/null; then kill -KILL "$server" 2>/dev/null || :; fi
        wait "$server" 2>/dev/null || :
    fi
    if [ "$result" -eq 0 ]; then
        echo 'stage=read-samples result=PASS'
    else
        echo 'stage=read-samples result=FAIL' >&2
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
umask 077
cd /scratch
read -r seconds <snapshot-seconds
case $seconds in ''|0*|*[!0-9]*|???????????*) exit 1 ;; esac

# No scrape, discovery, rules, or remote endpoints. Disable retention and
# limit compaction on this disposable writable TSDB.
printf 'storage: {tsdb: {retention: {time: 0s}}}\n' >prometheus.yml
listen=127.0.0.1:9090
/bin/prometheus --config.file=./prometheus.yml --storage.tsdb.path=./tsdb \
    --storage.tsdb.max-block-duration=2h --web.listen-address="$listen" \
    --query.timeout=30s >/dev/null 2>&1 &
server=$!
url=http://$listen
attempt=0
until timeout -k 1 2 /bin/promtool check ready --url="$url" >/dev/null 2>&1; do
    kill -0 "$server" 2>/dev/null || exit 1
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || exit 1
    sleep 1
done
kill -0 "$server" 2>/dev/null

# Inspect actual stored samples, not resampled points or just the last value.
# The 1ms extends the left-open range to include the old dump's lower bound.
# promtool parses the API response and emits a compact JSON matrix. Match a
# float sample pair in a values array, never text inside a quoted label.
{ timeout -k 1 45 /bin/promtool query --format=json instant --time="$seconds" "$url" \
      'kube_node_info{node!=""}[24h1ms]' 2>/dev/null && echo query-complete; } |
    awk -F ',"values":\\[' '
        NR == 1 && /^\[\{"metric":/ && /\}\]$/ {
            for (i=2; i<=NF; i++)
                if (match($i, /^(\[[0-9]+([.][0-9]+)?,"[^"]+"\],)*\[[0-9]+([.][0-9]+)?,"[^"]+"\]\]/) &&
                    substr($i, 1, RLENGTH) ~ /,"1"\]/)
                    sample=1
        }
        NR == 2 && $0 == "query-complete" { complete=1 }
        END { exit !(NR == 2 && sample && complete) }'
kill -0 "$server" 2>/dev/null
