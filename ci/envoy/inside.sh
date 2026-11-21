#!/usr/bin/env bash
set -euo pipefail
# Use upstream regression coverage, including when a release already ships it.
# A conflict in either direction is not proof of a fix.
(cd /work/envoy && if git apply --check /work/regression.patch; then git apply /work/regression.patch; else git apply --reverse --check /work/regression.patch; fi)
baz=(bazel --output_user_root=/work/cache/bazel)
flags=(--config=release --jobs=4 --override_module=envoy=/work/envoy --override_module=envoy_api=/work/envoy/api)
pkg=@envoy//test/extensions/io_socket/user_space
tests=("$pkg:io_handle_impl_test" "$pkg:connection_compatibility_test")
if "${baz[@]}" test "${flags[@]}" --test_output=errors "${tests[@]}" >/work/stock.log 2>&1; then
  echo stock > /work/mode; exit 0
else rc=$?; fi
if [[ $rc != 3 ]] || ! grep -aqE '\[  FAILED  \] (ResetEmulation/)?InternalFlushWriteConnectionImplTest\.FlushWriteCompletesWhenBlockedPeerCloses(/(0|1))?([ ,]|$)' /work/stock.log; then tail -30 /work/stock.log; exit 1; fi
(cd /work/envoy && git apply --check /work/48409.patch && git apply /work/48409.patch)
"${baz[@]}" test "${flags[@]}" --test_output=errors "${tests[@]}"
"${baz[@]}" build "${flags[@]}" //:envoy
cp -L bazel-bin/envoy /work/context/envoy
echo patched > /work/mode
