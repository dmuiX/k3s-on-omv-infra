"""Reconcile Git-managed ACLs/webhook role; verify the KV and userpass mounts.

The Kubernetes auth role for this Job is a one-time bootstrap prerequisite.
Never log API bodies: the login response contains a token.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

ADDRESS = os.environ["OPENBAO_ADDR"].rstrip("/")
CONFIG_PATH = "/config/config.json"
JWT_PATH = "/run/identity/token"


class RequestFailure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Keep admin tokens on the local port-forward and Job tokens on the cluster network.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def request(path, *, payload=None, token=None, allow_missing=False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(ADDRESS + "/v1/" + path, data=data, headers=headers,
                                 method="PUT" if data is not None else "GET")
    try:
        with OPENER.open(req, timeout=10) as response:
            body = response.read()
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        # Never print the response body; auth errors may contain sensitive data.
        exc.close()
        if allow_missing and exc.code == 404:
            return None
        raise RequestFailure(f"OpenBao {path}: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RequestFailure(f"OpenBao {path}: request or response failed ({type(exc).__name__})") from None


def verify_mount(token, mount):
    mounts = request("sys/mounts", token=token)
    entry = mounts.get("data", mounts).get(mount + "/")
    if not entry or entry.get("type") != "kv" or str(entry.get("options", {}).get("version")) != "2":
        raise RequestFailure("Expected pre-existing KV v2 mount is missing or has the wrong version")


def verify_userpass(token, mount):
    auths = request("sys/auth", token=token)
    entry = auths.get("data", auths).get(mount + "/")
    if not entry or entry.get("type") != "userpass":
        raise RequestFailure("Expected pre-existing userpass auth mount is missing or has the wrong type")


def reconcile_policy(token, name):
    with open("/config/" + name + ".hcl", encoding="utf-8") as policy_file:
        desired = policy_file.read().strip()
    if not desired:
        raise RequestFailure("Git-managed policy file is empty: " + name)
    endpoint = "sys/policies/acl/" + name
    current = request(endpoint, token=token, allow_missing=True)
    if current is not None and current.get("data", {}).get("policy", "").strip() == desired:
        print("Policy current: " + name)
    else:
        request(endpoint, token=token, payload={"policy": desired})
        print("Policy reconciled: " + name)


def verify_role_bindings(existing, service_account, namespace):
    names = existing.get("bound_service_account_names")
    namespaces = existing.get("bound_service_account_namespaces")
    if isinstance(names, str):
        names = names.split(",")
    if isinstance(namespaces, str):
        namespaces = namespaces.split(",")
    if (names != [service_account] or namespaces != [namespace]
            or existing.get("bound_service_account_namespace_selector")):
        raise RequestFailure("Kubernetes role bindings differ; refusing to overwrite")


def preserved_role_settings(existing):
    return {key: existing[key] for key in (
        "audience", "alias_name_source", "token_max_ttl", "token_explicit_max_ttl", "token_period",
        "token_bound_cidrs", "token_no_default_policy", "token_num_uses", "token_type"
    ) if key in existing and existing[key] is not None}


def reconcile_role(token, role):
    ttl = str(role["token_ttl"])
    match = re.fullmatch(r"([0-9]+)([smh])", ttl)
    if not match or int(match[1]) == 0:
        raise RequestFailure("Expected a positive token TTL in seconds, minutes or hours")
    expected_seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]
    endpoint = "auth/kubernetes/role/" + role["name"]
    response = request(endpoint, token=token, allow_missing=True)
    existing = (response.get("data") or {}) if response is not None else {}
    if response is not None:
        verify_role_bindings(existing, role["service_account"], role["namespace"])
    policies = existing.get("token_policies") or existing.get("policies") or []
    if isinstance(policies, str):
        policies = policies.split(",")
    desired = role["policies"]
    allowed_predecessors = set(role.get("allowed_predecessor_policies", []))
    if set(policies) - set(desired) - allowed_predecessors:
        raise RequestFailure(f"Kubernetes role {role['name']} contains unexpected policies; refusing to overwrite")
    current_ttl = str(existing.get("token_ttl", existing.get("ttl", 0)))
    desired_no_default = role.get("token_no_default_policy")
    no_default_current = (desired_no_default is None or
                          existing.get("token_no_default_policy") is desired_no_default)
    if (response is not None and set(policies) == set(desired) and
            current_ttl == str(expected_seconds) and no_default_current):
        print(f"Kubernetes role current: {role['name']}")
        return
    # Retain existing non-policy role settings when adopting the prior role.
    payload = {
        "bound_service_account_names": [role["service_account"]],
        "bound_service_account_namespaces": [role["namespace"]],
        "token_policies": desired,
        "token_ttl": role["token_ttl"],
    }
    payload.update(preserved_role_settings(existing))
    if desired_no_default is not None:
        payload["token_no_default_policy"] = desired_no_default
    request(endpoint, token=token, payload=payload)
    print(f"Kubernetes role reconciled: {role['name']}")


def main():
    with open(JWT_PATH, encoding="utf-8") as token_file:
        jwt = token_file.read().strip()
    if not jwt:
        raise RequestFailure("Projected ServiceAccount token is empty")
    login = request("auth/kubernetes/login", payload={"role": "openbao-access-config", "jwt": jwt})
    jwt = None
    token = login.get("auth", {}).get("client_token")
    if not token:
        raise RequestFailure("OpenBao Kubernetes auth did not return a client token")

    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        config = json.load(config_file)
    verify_mount(token, config["kv_mount"])
    verify_userpass(token, config["human_auth_mount"])
    # Reject every unexpected role binding before widening any policy a role may use.
    for role in config["roles"]:
        reconcile_role(token, role)
    for policy in config["managed_policies"]:
        reconcile_policy(token, policy)


if __name__ == "__main__":
    try:
        main()
    except (RequestFailure, OSError, KeyError) as error:
        # OSError includes file paths, never file content or token values.
        print(f"Webhook policy reconciliation failed: {error}", file=sys.stderr)
        sys.exit(1)
