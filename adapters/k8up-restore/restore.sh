#!/bin/sh
# Run via sh in ghcr.io/k8up-io/k8up:v2.16.0@sha256:29458113b8b676b5b48ebf25b21be5194ee9dccc887b92b8fa27f7d3dfca6f50
# sh restore.sh backup-validation|prometheus-stream MAX_AGE_SECONDS
# Read-only local repository PVC, Secret via RESTIC_PASSWORD[_FILE], fresh private scratch.
set -eu
umask 077
trap '[ "$?" -eq 0 ] || echo "stage=restore result=FAIL" >&2' EXIT
die() { echo "$1" >&2; exit 1; }
[ "$#" = 2 ] || die 'expected source and maximum age in seconds'
source=$1
max_age=$2
case $max_age in ''|0*|*[!0-9]*|??????????*) die 'invalid maximum age' ;; esac
case $source in
    backup-validation)
        host=backup-validation; path=/data/k8up-validation-canary
        run_tag=${K8UP_FIXTURE_RUN_TAG:-}
        jq -en --arg tag "$run_tag" '
            $tag | test("\\Abackup-run-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\\z")
        ' >/dev/null || die 'invalid or missing fixture backup run tag'
        set -- --tag "$run_tag" ;;
    prometheus-stream)
        # K8up v2.16 backupAnnotatedPod + StdinBackup: /<namespace>-<container><fileExtension>.
        # The PreBackupPod specifies container snapshot and literal extension tar (no dot).
        host=kube-prometheus-stack; path=/kube-prometheus-stack-snapshottar
        run_tag=${K8UP_PROMETHEUS_RUN_TAG:-}
        expected=${PROMETHEUS_EXPECTED_SNAPSHOT:-}
        # Missing Pod annotations resolve to empty strings. Require both pins
        # before touching scratch or the repository; there is no latest fallback.
        jq -en --arg tag "$run_tag" --arg name "$expected" '
            ($tag | test("\\Abackup-run-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\\z")) and
            ($name | test("\\A[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\\z"))
        ' >/dev/null || die 'invalid or incomplete Prometheus backup pin'
        set -- --tag "$run_tag" ;;
    *) die 'unknown restore source' ;;
esac
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
# Bound the listing and do not truncate exact-tag matches: a duplicate must
# fail even when the records have different times. Restic filters can still
# match records with additional paths or tags; verify the full metadata below.
(ulimit -f 128 && repo snapshots --json --host "$host" --path "$path" "$@" >.stage/list 2>.stage/warnings) || die 'snapshot listing failed'
[ ! -s .stage/warnings ] || die 'incomplete snapshot listing'
selected=$(jq -ers --arg host "$host" --arg path "$path" --arg tag "$run_tag" --argjson age "$max_age" '
    # Go timestamps may have fractional seconds and a numeric timezone offset.
    def epoch:
        (capture("\\A(?<dt>[0-9-]{10}T[0-9:]{8})(?:\\.(?<frac>[0-9]{1,9}))?(?:Z|(?<sign>[+-])(?<h>[01][0-9]|2[0-3]):(?<m>[0-5][0-9]))\\z") // error("time")) as $t
        | ($t.dt + "Z" | fromdateiso8601) as $sec
        | if ($sec | todateiso8601) != ($t.dt + "Z") then error("date") else $sec end
        + ("0." + ($t.frac // "0") | tonumber)
        - (if $t.sign then (($t.h|tonumber)*3600 + ($t.m|tonumber)*60) * (if $t.sign=="+" then 1 else -1 end) else 0 end);
    select(length == 1) | .[0] | select(type == "array")
    | select(length == 1) | first
    | select(.hostname == $host and .paths == [$path] and (.id | test("\\A[0-9a-f]{64}\\z")))
    | select(if $host == "backup-validation" then (.tags | sort) == ([$tag, "k8up-validation-canary"] | sort)
             else (.tags | sort) == ([$tag, "prometheus-replica-0", "prometheus-tsdb"] | sort)
             end)
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
case $source in
    backup-validation) [ ! -L .stage/tree/data ] && [ ! -L "$target" ] || die 'invalid fixture directory'
        target=$target/marker.txt ;;
esac
[ ! -L "$target" ] && [ -f "$target" ] && [ -s "$target" ] || die 'empty or invalid restored source'
mv .stage/tree restored
# Trusted scalar handoff for promtool's native time filter, never shell code.
if [ "$source" = prometheus-stream ]; then printf '%s\n' "$seconds" >snapshot-seconds; fi
rmdir .stage
