"""Reconcile the narrow cert-manager/OpenBao PKI integration.

This process authenticates with a projected Kubernetes token. It only writes two
leaf-signing ACL policies, two PKI issuance roles and two Kubernetes auth roles.
PKI mounts, issuers, keys and CA chains are pre-existing prerequisites.
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
DURATION_SECONDS = {"90d": 90 * 24 * 60 * 60, "1h": 60 * 60}
EXPECTED_IDENTITIES = {
    "pki-services": {
        "policy": "cert-manager-pki-services-sign",
        "pki_role": "services",
        "auth_role": "cert-manager-pki-services",
        "service_account": "openbao-pki-services",
        "namespace": "cert-manager",
        "audience": "vault://openbao-pki-services",
        "allowed_domains": ["svc", "svc.cluster.local"],
        "server": True,
        "client": False,
    },
    "pki-clients": {
        "policy": "cert-manager-pki-clients-sign",
        "pki_role": "clients",
        "auth_role": "cert-manager-pki-clients",
        "service_account": "openbao-pki-clients",
        "namespace": "cert-manager",
        "audience": "vault://openbao-pki-clients",
        "allowed_domains": ["clients.cluster.local"],
        "server": False,
        "client": True,
    },
}


class RequestFailure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Tokens must never be sent through environment or operating-system proxies.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def request(path, *, payload=None, token=None, allow_missing=False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(
        ADDRESS + "/v1/" + path,
        data=data,
        headers=headers,
        method="PUT" if data is not None else "GET",
    )
    try:
        with OPENER.open(req, timeout=10) as response:
            body = response.read()
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        exc.close()  # The body can contain sensitive material; never include it.
        if allow_missing and exc.code == 404:
            return None
        raise RequestFailure(f"OpenBao {path}: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RequestFailure(
            f"OpenBao {path}: request or response failed ({type(exc).__name__})"
        ) from None


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        config = json.load(config_file)
    if config.get("login_role") != "openbao-pki-reconciler":
        raise RequestFailure("Unexpected reconciler login role")
    entries = config.get("mounts")
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_IDENTITIES):
        raise RequestFailure("Expected exactly two PKI profiles")
    actual = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("mount"), str):
            raise RequestFailure("Invalid PKI profile")
        mount = entry["mount"]
        if mount in actual:
            raise RequestFailure("Duplicate PKI mount")
        actual[mount] = {key: value for key, value in entry.items() if key != "mount"}
    if actual != EXPECTED_IDENTITIES:
        raise RequestFailure("PKI profiles differ from the constrained service/client identities")
    return config


def verify_prerequisites(token, entries):
    mounts_response = request("sys/mounts", token=token)
    mounts = mounts_response.get("data", mounts_response)
    for entry in entries:
        mount = entry["mount"]
        mounted = mounts.get(mount + "/")
        if not mounted or mounted.get("type") != "pki":
            raise RequestFailure(f"Expected pre-existing PKI mount is missing: {mount}")
        issuer = request(f"{mount}/issuer/default/json", token=token)
        data = issuer.get("data", {})
        certificate = data.get("certificate")
        chain = data.get("ca_chain")
        usage = data.get("usage")
        if isinstance(usage, str):
            usage = [item.strip() for item in usage.split(",") if item.strip()]
        if (not isinstance(certificate, str)
                or "-----BEGIN CERTIFICATE-----" not in certificate
                or not isinstance(chain, list)
                or not chain
                or not all(isinstance(item, str) and "-----BEGIN CERTIFICATE-----" in item
                           for item in chain)
                or all(item.strip() == certificate.strip() for item in chain)
                or not isinstance(data.get("key_id"), str)
                or not data["key_id"].strip()
                or not isinstance(usage, list)
                or "issuing-certificates" not in usage):
            raise RequestFailure(f"PKI default issuer is not a usable pre-signed CA chain: {mount}")


def reconcile_policy(token, entry):
    name = entry["policy"]
    with open("/config/" + name + ".hcl", encoding="utf-8") as policy_file:
        desired = policy_file.read().strip()
    exact = f'path "{entry["mount"]}/sign/{entry["pki_role"]}" {{\n  capabilities = ["update"]\n}}'
    if desired != exact:
        raise RequestFailure("Leaf-signing policy is not the exact constrained policy: " + name)
    endpoint = "sys/policies/acl/" + name
    current = request(endpoint, token=token, allow_missing=True)
    if current is not None and current.get("data", {}).get("policy", "").strip() == desired:
        print("Policy current: " + name)
        return
    request(endpoint, token=token, payload={"policy": desired})
    print("Policy reconciled: " + name)


def desired_pki_role(entry):
    return {
        "issuer_ref": "default",
        "allowed_domains": entry["allowed_domains"],
        "allowed_domains_template": False,
        "allow_subdomains": True,
        "allow_bare_domains": False,
        "allow_glob_domains": False,
        "allow_wildcard_certificates": False,
        "allow_any_name": False,
        "enforce_hostnames": True,
        "allow_ip_sans": False,
        "allowed_uri_sans": [],
        "allowed_uri_sans_template": False,
        "allowed_other_sans": [],
        "allowed_serial_numbers": [],
        "allowed_user_ids": [],
        "allow_localhost": False,
        "cn_validations": ["hostname"],
        "server_flag": entry["server"],
        "client_flag": entry["client"],
        "code_signing_flag": False,
        "email_protection_flag": False,
        "key_type": "rsa",
        "key_bits": 2048,
        "key_usage": ["DigitalSignature", "KeyEncipherment"],
        "ext_key_usage": [],
        "ext_key_usage_oids": [],
        "policy_identifiers": [],
        "ou": [],
        "organization": [],
        "country": [],
        "locality": [],
        "province": [],
        "street_address": [],
        "postal_code": [],
        "basic_constraints_valid_for_non_ca": False,
        "ttl": "90d",
        "max_ttl": "90d",
        "require_cn": False,
        "use_csr_common_name": True,
        "use_csr_sans": True,
        "use_pss": False,
        "signature_bits": 0,
        "generate_lease": False,
        "no_store": False,
        "no_store_metadata": False,
    }


def equivalent(current, desired):
    for key, expected in desired.items():
        actual = current.get(key)
        if key in ("ttl", "max_ttl", "token_ttl", "token_max_ttl"):
            actual = str(actual)
            expected = str(DURATION_SECONDS[expected])
        if actual != expected:
            return False
    return True


def reconcile_pki_role(token, entry):
    desired = desired_pki_role(entry)
    endpoint = f'{entry["mount"]}/roles/{entry["pki_role"]}'
    response = request(endpoint, token=token, allow_missing=True)
    current = response.get("data", {}) if response is not None else {}
    if response is not None and equivalent(current, desired):
        print("PKI role current: " + entry["pki_role"])
        return
    request(endpoint, token=token, payload=desired)
    print("PKI role reconciled: " + entry["pki_role"])


def normalized_list(value):
    if isinstance(value, str):
        return [item for item in value.split(",") if item]
    return value or []


def verify_existing_auth_boundary(token, entry):
    """Reject identity/policy widening before any managed write occurs."""
    endpoint = "auth/kubernetes/role/" + entry["auth_role"]
    response = request(endpoint, token=token, allow_missing=True)
    if response is None:
        return
    current = response.get("data") or {}
    if (normalized_list(current.get("bound_service_account_names"))
            != [entry["service_account"]]
            or normalized_list(current.get("bound_service_account_namespaces"))
            != [entry["namespace"]]
            or current.get("bound_service_account_namespace_selector")
            or current.get("audience") != entry["audience"]
            or normalized_list(current.get("token_policies") or current.get("policies"))
            != [entry["policy"]]
            or current.get("token_no_default_policy") is not True):
        raise RequestFailure(
            "Existing cert-manager Kubernetes role crosses its reviewed identity boundary: "
            + entry["auth_role"]
        )


def desired_auth_role(entry):
    return {
        "bound_service_account_names": [entry["service_account"]],
        "bound_service_account_namespaces": [entry["namespace"]],
        "audience": entry["audience"],
        "token_policies": [entry["policy"]],
        "token_ttl": "1h",
        "token_max_ttl": "1h",
        "token_explicit_max_ttl": 0,
        "token_period": 0,
        "token_num_uses": 0,
        "token_bound_cidrs": [],
        "token_type": "default",
        "alias_name_source": "serviceaccount_uid",
        "bound_service_account_namespace_selector": "",
        "token_no_default_policy": True,
    }


def reconcile_auth_role(token, entry):
    desired = desired_auth_role(entry)
    endpoint = "auth/kubernetes/role/" + entry["auth_role"]
    response = request(endpoint, token=token, allow_missing=True)
    current = response.get("data", {}) if response is not None else {}
    if response is not None and equivalent(current, desired):
        print("Kubernetes auth role current: " + entry["auth_role"])
        return
    request(endpoint, token=token, payload=desired)
    print("Kubernetes auth role reconciled: " + entry["auth_role"])


def main():
    config = load_config()
    with open(JWT_PATH, encoding="utf-8") as token_file:
        jwt = token_file.read().strip()
    if not jwt:
        raise RequestFailure("Projected ServiceAccount token is empty")
    login = request(
        "auth/kubernetes/login",
        payload={"role": config["login_role"], "jwt": jwt},
    )
    jwt = None
    token = login.get("auth", {}).get("client_token")
    if not token:
        raise RequestFailure("OpenBao Kubernetes auth did not return a client token")

    # Complete every read-only mount/chain and existing identity-boundary check
    # before the first managed write. A drifted second role must not permit a
    # partial first-profile reconciliation.
    verify_prerequisites(token, config["mounts"])
    for entry in config["mounts"]:
        verify_existing_auth_boundary(token, entry)
    for entry in config["mounts"]:
        reconcile_policy(token, entry)
        reconcile_pki_role(token, entry)
        reconcile_auth_role(token, entry)


if __name__ == "__main__":
    try:
        main()
    except (RequestFailure, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"OpenBao PKI reconciliation failed: {error}", file=sys.stderr)
        sys.exit(1)
