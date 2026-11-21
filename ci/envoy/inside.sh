#!/usr/bin/env bash
set -euo pipefail
# Regression coverage from upstream PR 48409, adapted to the locked source tree.
# Unknown layouts stop for review; never assume a failed patch means stock is fixed.
(cd /work/envoy && git apply --check /work/regression.patch && git apply /work/regression.patch)
baz=(bazel --output_user_root=/work/cache/bazel)
flags=(--config=release --jobs=4 --override_repository=envoy=/work/envoy)
pkg=@envoy//test/extensions/io_socket/user_space
tests=("$pkg:io_handle_impl_test" "$pkg:connection_compatbility_test")
if "${baz[@]}" test "${flags[@]}" --test_output=errors "${tests[@]}" >/work/stock.log 2>&1; then
  echo stock > /work/mode; exit 0
else rc=$?; fi
if [[ $rc != 3 ]] || ! grep -aqF '[  FAILED  ] InternalFlushWriteConnectionImplTest.FlushWriteCompletesWhenBlockedPeerCloses' /work/stock.log; then tail -30 /work/stock.log; exit 1; fi
(cd /work/envoy && git apply --check /work/48409.patch && git apply /work/48409.patch)
"${baz[@]}" test "${flags[@]}" --test_output=errors "${tests[@]}"
"${baz[@]}" build "${flags[@]}" //:envoy
cp -L bazel-bin/envoy /work/context/envoy
echo patched > /work/mode
