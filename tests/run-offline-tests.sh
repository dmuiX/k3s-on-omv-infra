#!/bin/sh
set -eu

workspace=$(cd -- "$(dirname -- "$0")/../.." && pwd)
bootstrap="$workspace/k3s-on-omv-bootstrap"
infra="$workspace/k3s-on-omv-infra"
export PYTHONDONTWRITEBYTECODE=1

python=${PYTHON:-python3}
if ! "$python" -c 'import yaml' 2>/dev/null; then
  ansible_playbook=$(command -v ansible-playbook)
  python=$(head -n 1 "$ansible_playbook")
  python=${python#\#!}
fi
"$python" -c 'import yaml'

"$python" -m unittest discover -s "$bootstrap/ansible/tests" -p 'test_*.py' -v
"$python" -m unittest discover -s "$infra/tests" -p 'test_*.py' -v
ruby -EUTF-8 "$infra/tests/test_application_health.rb"
ruby -EUTF-8 "$infra/tests/verify-contracts.rb"
ruby -EUTF-8 "$infra/tests/verify-mesh-renders.rb"

for playbook in "$bootstrap"/ansible/*.yml "$bootstrap"/ansible/recovery/*.yml "$bootstrap"/ansible/maintenance/*.yml; do
  # Task includes are validated through their importing playbooks or fixture tests.
  case "$playbook" in */requirements.yml|*/maintenance/vault-withdrawal-*.yml|*/maintenance/openbao-provider-session.yml|*/maintenance/openbao-provider-review.yml) continue ;; esac
  ANSIBLE_CONFIG="$bootstrap/ansible/ansible.cfg" \
    ansible-playbook --syntax-check \
      -i "$bootstrap/ansible/inventory/hosts.yml" \
      "$playbook"
done
