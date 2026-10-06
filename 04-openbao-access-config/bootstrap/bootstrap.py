"""One-time, human-approved OpenBao bootstrap from a LOCAL port-forward.

Run only after reviewing the target cluster and source. An existing admin token
is entered at a hidden prompt; no token is saved to disk or passed as an arg.
Never run this script from Argo CD or put the admin token in Kubernetes.
"""
import getpass
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "workload"
os.environ["OPENBAO_ADDR"] = "http://127.0.0.1:18200"
spec = importlib.util.spec_from_file_location("reconcile", WORKLOAD / "reconcile.py")
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)


def read_secret(prompt):
    # getpass otherwise falls back to reading with echo enabled.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise reconcile.RequestFailure("Hidden input unavailable; run from an interactive terminal") from None


def create_human_user(admin_token, mount):
    username = input("Personal userpass admin username (blank to skip): ").strip()
    if not username:
        print("Personal user creation skipped")
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
        raise reconcile.RequestFailure("Username must contain only letters, digits, underscores or hyphens")
    endpoint = "auth/" + mount + "/users/" + username
    existing = reconcile.request(endpoint, token=admin_token, allow_missing=True)
    if existing is not None:
        policies = existing.get("data", {}).get("token_policies") or existing.get("data", {}).get("policies") or []
        if isinstance(policies, str):
            policies = policies.split(",")
        if "human-admin" not in policies:
            raise reconcile.RequestFailure("Existing user lacks human-admin; refusing to change password or policies")
        print("Existing personal user preserved (password unchanged)")
        return
    password = read_secret("New userpass password: ")
    confirm = read_secret("Repeat userpass password: ")
    if password != confirm or len(password) < 12:
        raise reconcile.RequestFailure("Passwords differ or are shorter than 12 characters")
    reconcile.request(endpoint, token=admin_token, payload={"password": password, "policies": "human-admin"})
    password = confirm = None
    print("Personal user created; verify login before configuring MFA")


def main():
    admin_token = read_secret("Existing OpenBao admin token (localhost only): ")
    if not admin_token:
        raise reconcile.RequestFailure("No admin token provided")
    config = json.loads((WORKLOAD / "config.json").read_text(encoding="utf-8"))
    policy = (WORKLOAD / "policies/vault-secrets-webhook-read.hcl").read_text(encoding="utf-8")
    human_policy = (WORKLOAD / "policies/human-admin.hcl").read_text(encoding="utf-8")
    writer = (ROOT / "bootstrap/openbao-access-config-writer.hcl").read_text(encoding="utf-8")

    mounts = reconcile.request("sys/mounts", token=admin_token)
    kv = mounts.get("data", mounts).get(config["kv_mount"] + "/")
    if kv is None:
        reconcile.request("sys/mounts/" + config["kv_mount"], token=admin_token,
                          payload={"type": "kv", "options": {"version": "2"}})
        print("KV v2 mount created")
    else:
        reconcile.verify_mount(admin_token, config["kv_mount"])

    auths = reconcile.request("sys/auth", token=admin_token)
    kubernetes = auths.get("data", auths).get("kubernetes/")
    if kubernetes is None:
        reconcile.request("sys/auth/kubernetes", token=admin_token, payload={"type": "kubernetes"})
        reconcile.request("auth/kubernetes/config", token=admin_token,
                          payload={"kubernetes_host": "https://kubernetes.default.svc:443"})
        print("Kubernetes auth mount configured (TokenReview RBAC still needs verification)")
    elif kubernetes.get("type") != "kubernetes":
        raise reconcile.RequestFailure("Existing Kubernetes auth mount is not kubernetes")
    else:
        auth_config = reconcile.request("auth/kubernetes/config", token=admin_token, allow_missing=True)
        if auth_config is None or not (auth_config.get("data") or {}).get("kubernetes_host"):
            raise reconcile.RequestFailure(
                "Existing Kubernetes auth mount is unconfigured; review and configure it before retrying")
        print("Existing Kubernetes auth mount preserved")
    userpass = auths.get("data", auths).get(config["human_auth_mount"] + "/")
    if userpass is None:
        reconcile.request("sys/auth/" + config["human_auth_mount"], token=admin_token,
                          payload={"type": "userpass"})
        print("Userpass auth mount created")
    elif userpass.get("type") != "userpass":
        raise reconcile.RequestFailure("Existing human auth mount is not userpass")
    else:
        print("Existing userpass auth mount preserved")

    # Check both identities before granting policies to any existing role.
    writer_role = reconcile.request("auth/kubernetes/role/openbao-access-config",
                                    token=admin_token, allow_missing=True)
    writer_settings = (writer_role.get("data") or {}) if writer_role is not None else {}
    if writer_role is not None:
        reconcile.verify_role_bindings(writer_settings, "openbao-access-config", "openbao")
    reconcile.reconcile_role(admin_token, config["webhook_role"])
    reconcile.request("sys/policies/acl/openbao-access-config-writer", token=admin_token,
                      payload={"policy": writer})
    reconcile.request("auth/kubernetes/role/openbao-access-config", token=admin_token,
                      payload={"bound_service_account_names": ["openbao-access-config"],
                               "bound_service_account_namespaces": ["openbao"],
                               "token_policies": ["openbao-access-config-writer"], "token_ttl": "10m",
                               **reconcile.preserved_role_settings(writer_settings)})
    reconcile.request("sys/policies/acl/vault-secrets-webhook-read", token=admin_token,
                      payload={"policy": policy})
    reconcile.request("sys/policies/acl/human-admin", token=admin_token,
                      payload={"policy": human_policy})
    create_human_user(admin_token, config["human_auth_mount"])
    print("OpenBao access bootstrap complete; admin token not stored")


if __name__ == "__main__":
    try:
        main()
    except (reconcile.RequestFailure, OSError, KeyError, ValueError) as error:
        print(f"OpenBao bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
