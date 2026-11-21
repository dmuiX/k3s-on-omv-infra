#!/usr/bin/env bash
# Build a candidate only; no cluster access or registry credentials inside the builder.
set -euo pipefail
root=$(pwd); lock=$root/ci/envoy/lock.json
get(){ jq -er --arg k "$1" '.[$k]' "$lock"; }
for input in stock toolchain; do
  ref=$(get "$input"); tag=$(get "${input}_tag")
  [[ $tag =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]
  actual=$(docker buildx imagetools inspect "${ref%@*}:$tag" --format '{{.Manifest.Digest}}')
  [[ $actual == "${ref##*@}" ]] || { echo "Tag/digest mismatch: $input" >&2; exit 1; }
done
work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/envoy-build.XXXXXXXX")
mkdir "$work"/{envoy,proxy,cache,context}; cp ci/envoy/{48409.patch,regression.patch,inside.sh} "$work/"
for component in proxy envoy; do
  org=istio; [[ $component != envoy ]] || org=envoyproxy
  rev=$(get "$component"); [[ $rev =~ ^[a-f0-9]{40}$ ]]
  curl --fail --silent --show-error --location --retry 2 "https://codeload.github.com/$org/$component/tar.gz/$rev" -o "$work/$component.tgz"
  printf '%s  %s\n' "$(get "${component}_tar_sha256")" "$work/$component.tgz" | sha256sum -c -
  tar -xzf "$work/$component.tgz" --strip-components=1 -C "$work/$component"
done
# Verify the patched dependency is the same one the pinned proxy is meant to build.
grep -Fx "ENVOY_SHA = \"$(get envoy)\"" "$work/proxy/MODULE.bazel"
export BUILD_SCM_REVISION; BUILD_SCM_REVISION=$(get proxy)
docker run --rm --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges -v "$work:/work" -w /work/proxy -e HOME=/work/cache -e BUILD_SCM_REVISION -e BUILD_SCM_STATUS=Modified --entrypoint /bin/bash "$(get toolchain)" /work/inside.sh
mode=$(<"$work/mode"); case "$mode" in
  patched) printf '# Official upstream tag: %s\nFROM %s\nCOPY --chmod=0755 envoy /usr/local/bin/envoy\n' "$(get stock_tag)" "$(get stock)" > "$work/context/Dockerfile"
    docker build --network=none -t envoy-candidate:local "$work/context";;
  stock) docker pull "$(get stock)"; docker tag "$(get stock)" envoy-candidate:local;;
  *) exit 1;;
esac
docker run --rm --network none --read-only --cap-drop ALL --entrypoint /usr/local/bin/envoy envoy-candidate:local --version
printf 'mode=%s\n' "$mode" >> "${GITHUB_OUTPUT:?}"
