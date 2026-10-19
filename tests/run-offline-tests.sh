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
ruby "$infra/tests/verify-contracts.rb"
ruby "$infra/tests/verify-mesh-renders.rb"

for playbook in host-k3s.yml gitops-openbao.yml openbao-restore.yml; do
  ANSIBLE_CONFIG="$bootstrap/ansible/ansible.cfg" \
    ansible-playbook --syntax-check \
      -i "$bootstrap/ansible/inventory/hosts.yml" \
      "$bootstrap/ansible/$playbook"
done
