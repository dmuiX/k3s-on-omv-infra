#!/bin/sh
# Run via sh in ghcr.io/k8up-io/k8up:v2.16.0@sha256:29458113b8b676b5b48ebf25b21be5194ee9dccc887b92b8fa27f7d3dfca6f50
# sh restore.sh canary|prometheus-stream MAX_AGE_SECONDS
# Read-only local repository PVC, Secret via RESTIC_PASSWORD[_FILE], fresh private scratch.
set -eu
umask 077
trap '[ "$?" -eq 0 ] || echo "stage=restore result=FAIL" >&2' EXIT
die() { echo "$1" >&2; exit 1; }
[ "$#" = 2 ] || die 'expected source and maximum age in seconds'
case $1 in
    canary) host=backup-validation; path=/data/k8up-validation-canary ;;
    # K8up v2.16 backupAnnotatedPod + StdinBackup: /<namespace>-<container><fileExtension>.
    # The PreBackupPod specifies container snapshot and literal extension tar (no dot).
    prometheus-stream) host=kube-prometheus-stack; path=/kube-prometheus-stack-snapshottar ;;
    *) die 'unknown restore source' ;;
esac
case $2 in ''|0*|*[!0-9]*|??????????*) die 'invalid maximum age' ;; esac
repository=${RESTIC_REPOSITORY:-/repository}
case $repository in /*) ;; *) die 'local repository mount required' ;; esac
unset RESTIC_PASSWORD_COMMAND RESTIC_REPOSITORY_FILE
repo() { restic --repo "local:$repository" --no-lock --no-cache "$@" </dev/null; }
cd "${SCRATCH_DIR:-/scratch}"
# Never merge with an old restore or follow an old handoff symlink.
for entry in * .[!.]* ..?*; do
    # Fresh ext4 PVCs may already contain the filesystem's lost+found directory.
    if [ "$entry" = lost+found ] && [ -d "$entry" ] && [ ! -L "$entry" ]; then continue; fi
    [ ! -e "$entry" ] && [ ! -L "$entry" ] || die 'scratch is not empty'
done
mkdir .stage
export TMPDIR="$PWD/.stage" TZ=UTC0
# Native host/path filtering, bounded JSON, no grouping. A path match can still
# include other PVCs. Do not fall back to an older snapshot if the newest is mixed.
(ulimit -f 128 && repo snapshots --json --host "$host" --path "$path" --latest 2 >.stage/list 2>.stage/warnings) || die 'snapshot listing failed'
[ ! -s .stage/warnings ] || die 'incomplete snapshot listing'
selected=$(jq -ers --arg host "$host" --arg path "$path" --argjson age "$2" '
    # Go timestamps may have fractional seconds and a numeric timezone offset.
    def epoch:
        (capture("\\A(?<dt>[0-9-]{10}T[0-9:]{8})(?:\\.(?<frac>[0-9]{1,9}))?(?:Z|(?<sign>[+-])(?<h>[01][0-9]|2[0-3]):(?<m>[0-5][0-9]))\\z") // error("time")) as $t
        | ($t.dt + "Z" | fromdateiso8601) as $sec
        | if ($sec | todateiso8601) != ($t.dt + "Z") then error("date") else $sec end
        + ("0." + ($t.frac // "0") | tonumber)
        - (if $t.sign then (($t.h|tonumber)*3600 + ($t.m|tonumber)*60) * (if $t.sign=="+" then 1 else -1 end) else 0 end);
    select(length == 1) | .[0] | select(type == "array" and (length == 1 or length == 2))
    | sort_by(.time | epoch) | select(length == 1 or (.[0].time|epoch) < (.[1].time|epoch)) | last
    | select(.hostname == $host and .paths == [$path] and (.id | test("\\A[0-9a-f]{64}\\z")))
    | select($host == "backup-validation" or (.tags | sort) == ["prometheus-replica-0", "prometheus-tsdb"])
    | (now - (.time | epoch)) as $elapsed | select($elapsed >= 0 and $elapsed <= $age)
    | [.id, .time, (.time | epoch | floor)] | @tsv
' .stage/list 2>/dev/null) || die 'no unambiguous fresh snapshot of expected source'
read -r id time seconds <<EOF
$selected
EOF
# These two fields passed the strict id/date checks above. Keep them in Pod
# logs even if restore or a later container fails; selection is not a PASS.
printf 'snapshot_id=%s time=%s\n' "$id" "$time"
rm .stage/list .stage/warnings
repo restore --verify --include "$path" --target .stage/tree "$id" >/dev/null 2>&1 || die 'verified restore failed'
target=.stage/tree$path
case $1 in
    canary) [ ! -L .stage/tree/data ] && [ ! -L "$target" ] || die 'invalid canary directory'
        target=$target/marker.txt ;;
esac
[ ! -L "$target" ] && [ -f "$target" ] && [ -s "$target" ] || die 'empty or invalid restored source'
mv .stage/tree restored
# Trusted scalar handoff for promtool's native time filter, never shell code.
if [ "$1" = prometheus-stream ]; then printf '%s\n' "$seconds" >snapshot-seconds; fi
rmdir .stage
