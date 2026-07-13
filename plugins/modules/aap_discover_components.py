#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r'''
---
module: aap_discover_components
short_description: Auto-discover exportable CaC component endpoints from AAP's own API root(s)
description:
  - Hits the Controller API root (/api/controller/v2/) and, if reachable, the Gateway API
    root (/api/gateway/v1/) and the EDA API root (/api/eda/v1/, AAP 2.5+), reads the
    endpoint index each returns, and filters it down to CONFIG objects worth exporting
    for CaC.
  - This replaces a hand-maintained list of object_types - if AAP adds/removes/renames an
    endpoint in a future version, this module picks it up automatically instead of silently
    missing it.
options:
  controller_host:
    type: str
    required: true
    description: Base URL used for BOTH controller and gateway roots unless gateway_host is set.
  gateway_host:
    type: str
    required: false
    description: Override if the Gateway API is served from a different host/port than the Controller.
  controller_oauthtoken:
    type: str
    required: false
    no_log: true
    description: OAuth2 bearer token. One of this or controller_username/controller_password is required.
  controller_username:
    type: str
    required: false
    description: Basic-auth username, alternative to controller_oauthtoken.
  controller_password:
    type: str
    required: false
    no_log: true
    description: Basic-auth password, used with controller_username.
  validate_certs:
    type: bool
    default: true
    description: Whether to validate TLS certificates on the AAP endpoint(s).
  include_gateway:
    type: bool
    default: true
    description: Also probe the /api/gateway/v1/ root for gateway-owned objects (auth, settings, RBAC).
  eda_host:
    type: str
    required: false
    description: >
      Override if the EDA (Event-Driven Ansible) Controller API is served from a
      different host/port than the Gateway. Defaults to gateway_host (falling back to
      controller_host) since AAP 2.5+ serves /api/eda/v1/ through the same Gateway
      front door as everything else - EDA does not typically have its own separate
      public endpoint the way it could pre-2.5.
  include_eda:
    type: bool
    default: true
    description: >
      Also probe the /api/eda/v1/ root for EDA-owned CONFIG objects (credential_type,
      decision_environment, event_stream, project, rulebook_activation, role
      assignments, ...). Set false to skip EDA discovery entirely. A failure to reach
      this root (older AAP without EDA, or a token without EDA access) is surfaced in
      `excluded`, not a hard failure - same treatment as the Gateway root.
  include_settings:
    type: bool
    default: true
    description: >
      Add controller_settings (and gateway_settings, if include_gateway) as explicit
      components pointing at the well-known /settings/all/ single-object endpoint.
      These are NOT sourced from the root index walk like everything else here - the
      root index's own key names for the settings sub-API weren't reliable enough to
      detect programmatically, so this is one hardcoded pair of paths, same spirit as
      aap_build_id_maps' MAP_TYPE_CONFIG. aap_export_bulk's skip_on_404 handles it
      gracefully if the path doesn't exist on a given AAP version/topology.
  include_inventory_content:
    type: bool
    default: false
    description: >
      Whether to discover and export static inventory content - individual hosts and
      groups within each AAP inventory - as CaC components. Off by default because many
      shops source hosts/groups dynamically (cloud inventory plugins, SCM-sourced
      inventory), and exporting a dynamically-sourced inventory as static CaC would just
      snapshot someone else's system of record rather than declare actual desired state.
      Set true if you genuinely hand-author inventory content and want it captured.
      Equivalent to filetree_create's skip_inventory_hosts / skip_inventory_groups,
      inverted (this is opt-IN, those are opt-OUT).
  hub_host:
    type: str
    required: false
    description: >
      Override if the Hub/Automation Hub (Galaxy) API is served from a different
      host/port than the Gateway. Defaults to gateway_host (falling back to
      controller_host), same fallback chain eda_host uses - AAP 2.5+ serves
      /api/galaxy/ through the same Gateway front door.
  include_hub:
    type: bool
    default: false
    description: >
      Also probe the Hub/Galaxy Pulp API root (/api/galaxy/pulp/api/v3/) for
      Hub-owned CONFIG objects. Defaults to false (unlike include_gateway/include_eda,
      which default true) because Hub discovery is new and deliberately narrow for v1 -
      see HUB_ALLOWLIST below. Hub's object model (collections, repositories, remotes,
      namespaces) is a content-management system, not desired-state config the same way
      Controller/Gateway/EDA objects are: most of what its API root exposes is actual
      content or generated runtime artifacts (collection versions, container blobs,
      publications, tasks) rather than something to declare. v1 scope is deliberately
      config-only - namespaces, remotes, and contentguards/access_policies - via an
      explicit ALLOWLIST (opposite approach from the blocklists used for the other three
      roots, because the config-relevant fraction of Hub's API root is the small side of
      that split, not the large one). A failure to reach this root is surfaced in
      `excluded`, not a hard failure - same treatment as the Gateway/EDA roots.
author: Kushal / CitiusCloud
'''

EXAMPLES = r'''
- name: Discover every exportable component on this AAP instance
  aap_discover_components:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
  register: discovery

- name: Discover Controller-only, skipping Gateway/EDA/settings probing
  aap_discover_components:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
    include_gateway: false
    include_eda: false
    include_settings: false
  register: discovery

- name: Discover including hand-authored static inventory content (hosts/groups)
  aap_discover_components:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
    include_inventory_content: true
  register: discovery

- name: Discover Controller + Gateway + EDA + Hub (v1 config-only Hub scope)
  aap_discover_components:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
    include_hub: true
  register: discovery
'''

RETURN = r'''
components:
  description: >
    List of dicts, each {name, api_root (controller|gateway|eda), path}, representing
    every CONFIG endpoint discovered and not excluded by the runtime/noise blocklist.
    The two settings components (see include_settings) additionally carry
    kind="settings" so the playbook knows to export them as object_shape=dict rather
    than list.
  type: list
  returned: always
excluded:
  description: Endpoint names that were found but filtered out, with the reason.
  type: dict
  returned: always
'''

import json

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import open_url
from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError
from ansible.module_utils.six.moves.urllib.parse import urlparse

# Endpoints that exist in the API root index but are NOT configuration - they're runtime
# state, telemetry, history, or meta-endpoints about the API itself. Exporting these as
# "CaC" makes no sense (there's nothing to declare/apply - they're generated by the system).
RUNTIME_OR_META_BLOCKLIST = {
    # meta / discovery / non-object
    "ping", "config", "settings_all", "me", "dashboard", "service-index", "service_index",
    "mesh_visualizer", "bulk", "analytics", "swagger", "docs",
    # job/run history & telemetry - execution artifacts, not desired-state config
    "jobs", "project_updates", "inventory_updates", "ad_hoc_commands",
    "system_job_templates", "system_jobs", "notifications", "unified_job_templates",
    "unified_jobs", "activity_stream", "workflow_jobs", "workflow_approvals",
    "workflow_job_nodes", "metrics", "host_metrics", "host_metric_summary_monthly",
    "instances", "receptor_addresses",
    # legacy/derivable - superseded by role_definitions/role_*_assignments in 2.5+ gateway RBAC;
    # kept out to avoid double-exporting the same permission data in two formats
    "roles",
}

# Static inventory content (individual hosts and groups within an inventory) - kept
# separate from RUNTIME_OR_META_BLOCKLIST above because, unlike everything else in that
# set, hosts/groups genuinely ARE configuration for shops that manage them as CaC. They're
# excluded by default because many shops source them dynamically (cloud inventory plugins,
# SCM-sourced inventory) rather than hand-author them, and exporting a dynamically-sourced
# inventory as static CaC would just be re-exporting a snapshot of someone else's system of
# record. Controlled by the include_inventory_content param (default false, matching the
# old hardcoded-blocklist behavior) - see main() below for how it's merged into the
# effective blocklist. filetree_create's equivalent toggles are skip_inventory_hosts /
# skip_inventory_groups.
INVENTORY_CONTENT_NAMES = {"hosts", "groups"}

# Gateway root has its own meta/runtime noise to strip.
# IMPORTANT SECURITY NOTE: "tokens" is deliberately excluded - exporting OAuth/PAT tokens
# to a plaintext CaC YAML file is a credential-leak risk (these are LIVE, usable tokens,
# not just references). Never add "tokens" back to the exportable set.
# Also excluded: internal gateway service-mesh/topology objects (service_clusters,
# service_nodes, service_types, services, routes, http_ports, app_urls, ui_auth,
# ui_plugin_routes) - these describe the gateway's own running topology (which pods/nodes
# are registered, internal routing), not user-authored configuration. There's nothing to
# "declare" here; it's generated by the platform itself as it runs.
GATEWAY_BLOCKLIST = {
    "ping", "status", "me", "login", "logout", "swagger", "docs", "service-index",
    "service_index", "metrics", "activity_stream", "activitystream", "sessions", "session",
    "tokens",  # SECURITY: never export live OAuth/PAT tokens to disk
    "app_urls", "http_ports", "routes", "service_clusters", "service_keys",
    "service_nodes", "service_types", "services", "ui_auth", "ui_plugin_routes",
    "trigger_definition",  # internal gateway trigger plumbing, not user config
}

# EDA (Event-Driven Ansible) controller root, /api/eda/v1/ - added in AAP 2.5+, served
# through the same Gateway front door.
#
# CONFIRMED against a live instance (2026-07-11, Test dev) - and it uses a
# COMPLETELY DIFFERENT root-index naming convention than Controller/Gateway. Those two
# key the index by plain resource name ("job_templates", "applications"). EDA keys it by
# DRF's default router view-name convention instead: "<basename>-list" for every
# paginated collection, plus a scatter of one-off action names for things that aren't
# collections at all - "config", "session-login", "session-logout", "token-refresh",
# "current-user", "openapi-json", "openapi-yaml", "openapi-docs", "openapi-redoc". Only
# the "-list" entries are real paginated collections aap_export_bulk's list-shape mode
# can actually consume; the walk below drops anything that doesn't end in "-list" before
# the blocklist is even consulted, and un-suffixes what's left (e.g. "project-list" ->
# "project") so EDA_BLOCKLIST_BASENAMES lines up with the OBJECT, not the DRF view name.
#
# CONFIG kept (confirmed real endpoints): organization, team, user, project,
# credentialtype, edacredential (EDA's own credential objects - note the real name is
# "edacredential", NOT "credential"), credentialinputsource, decisionenvironment,
# eventstream, activation (the desired-state "should this activation exist" record -
# note the real name is "activation", NOT "rulebook_activation" as a first guess from
# the ansible.eda collection's module names might suggest).
EDA_BLOCKLIST_BASENAMES = {
    # execution/runtime artifacts - an activation's history/state, not its config
    "activationinstance", "auditrule",
    # SECURITY: live, usable AWX/Controller tokens a user has linked to their EDA
    # account - same treatment as GATEWAY_BLOCKLIST's "tokens". Never export these.
    "controller-token",
    # read-only/derived from a project sync (rulebook YAML lives IN the project repo),
    # not independently authored config - the "activation" list above is what actually
    # captures desired state.
    "rulebook",
}

# Hub/Automation Hub (Galaxy) Pulp API root, /api/galaxy/pulp/api/v3/ - AAP 2.5+.
#
# CONFIRMED against a live root index (2026-07-12, Test): this root also
# self-reports absolute http:// URLs with an unreliable internal host baked in - the
# exact same failure mode _normalize_path() below already handles for EDA, so no new
# normalization logic is needed, just calling it here too.
#
# UNLIKE the other three roots, Hub gets an ALLOWLIST instead of a blocklist, because
# the split is the other way round: this is raw Pulp (the generic content-storage
# engine underneath Galaxy NG), and most of what its root index exposes is actual
# CONTENT (collection versions, container blobs/manifests/tags, openpgp keys/
# signatures) or generated runtime artifacts (publications, tasks, task-groups,
# task-schedules, workers, uploads) - not configuration. Trying to blocklist our way
# down to the config-relevant handful from ~50 keys would be far more fragile than
# just naming the handful we do want.
#
# v1 scope (deliberately narrow, "config only" decision, 2026-07-12): namespaces,
# remotes, and contentguards/access_policies. Maps each root-index key (Hub's raw
# key names are path-like, e.g. "remotes/ansible/collection", not plain nouns) to the
# friendly name suffix used for this component's output filename (hub_<suffix>.yml).
# Deliberately NOT included in v1, even though present in the root index:
#   - bare "remotes" / "contentguards" - polymorphic aggregate listings across every
#     subtype in one endpoint, redundant with (and less precise than) the
#     subtype-specific endpoints below.
#   - "content/*", "artifacts", "publications", "repositories", "distributions" -
#     actual content or repository *content state*, not declarable config the same
#     way a remote/namespace/access-policy is.
#   - "tasks", "task-groups", "task-schedules", "workers", "uploads",
#     "upstream-pulps", "exporters/*", "importers/*" - runtime/action endpoints, same
#     class as Controller's "jobs"/"activity_stream".
#   - "domains", "signing-services", "acs/file/file" - plausible future v2 candidates,
#     just not confirmed/prioritized for v1.
#   - "users"/"groups"/"roles" under this root - ambiguous whether Hub still has its
#     own separate user/RBAC space distinct from Gateway's in this topology, or
#     whether it's fully delegated (in which case exporting it here would double-export
#     what gateway_users/gateway_role_definitions already capture). Left out of v1
#     rather than guessed; revisit once that's confirmed against a live instance.
HUB_ALLOWLIST = {
    "pulp_ansible/namespaces": "namespaces_ansible",
    "pulp_container/namespaces": "namespaces_container",
    "remotes/ansible/collection": "remotes_ansible_collection",
    "remotes/ansible/git": "remotes_ansible_git",
    "remotes/ansible/role": "remotes_ansible_role",
    "remotes/container/container": "remotes_container",
    "remotes/container/pull-through": "remotes_container_pull_through",
    "remotes/file/file": "remotes_file",
    "contentguards/core/composite": "contentguards_composite",
    "contentguards/core/content_redirect": "contentguards_content_redirect",
    "contentguards/core/header": "contentguards_header",
    "contentguards/core/rbac": "contentguards_rbac",
    "contentguards/certguard/rhsm": "contentguards_rhsm",
    "contentguards/certguard/x509": "contentguards_x509",
    "access_policies": "access_policies",
}


def _normalize_path(path):
    """Some API roots (confirmed: EDA's /api/eda/v1/ index) self-report ABSOLUTE URLs
    instead of relative paths - and the scheme/host in them isn't reliable (observed:
    plain http:// pointing at what's presumably EDA's internal view of its own hostname
    behind an OCP route, not the externally-reachable https:// one the discovery request
    itself was just made against). Strip down to just the path portion so downstream
    consumers always reconstruct the URL against a base_url we know is actually
    reachable, rather than trusting the API's self-reported host. A no-op for the
    relative paths Controller/Gateway already return."""
    if path.startswith("http://") or path.startswith("https://"):
        return urlparse(path).path
    return path


def fetch_root_index(module, base_url, path, headers):
    url = "%s%s" % (base_url, path)
    try:
        resp = open_url(
            url, headers=headers,
            url_username=module.params.get("controller_username"),
            url_password=module.params.get("controller_password"),
            force_basic_auth=bool(module.params.get("controller_username")),
            validate_certs=module.params.get("validate_certs"),
            method="GET", timeout=30,
        )
        return json.loads(resp.read()), None
    except HTTPError as e:
        return None, "HTTP %s fetching %s" % (e.code, url)
    except URLError as e:
        return None, "Unreachable: %s (%s)" % (url, str(e))
    except Exception as e:
        return None, "Unexpected error fetching %s: %s" % (url, str(e))


def build_auth_headers(module):
    token = module.params.get("controller_oauthtoken")
    if token:
        return {"Authorization": "Bearer %s" % token}
    return {}


def main():
    module_args = dict(
        controller_host=dict(type="str", required=True),
        gateway_host=dict(type="str", required=False),
        controller_username=dict(type="str", required=False),
        controller_password=dict(type="str", required=False, no_log=True),
        controller_oauthtoken=dict(type="str", required=False, no_log=True),
        validate_certs=dict(type="bool", default=True),
        include_gateway=dict(type="bool", default=True),
        include_settings=dict(type="bool", default=True),
        eda_host=dict(type="str", required=False),
        include_eda=dict(type="bool", default=True),
        include_inventory_content=dict(type="bool", default=False),
        hub_host=dict(type="str", required=False),
        include_hub=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)

    base_url = module.params["controller_host"].rstrip("/")
    gw_base_url = (module.params.get("gateway_host") or module.params["controller_host"]).rstrip("/")
    # EDA is served through the Gateway front door in AAP 2.5+ - default to gateway_host
    # (falling back through to controller_host), same fallback chain gateway_host itself uses.
    eda_base_url = (
        module.params.get("eda_host")
        or module.params.get("gateway_host")
        or module.params["controller_host"]
    ).rstrip("/")
    # Hub is served through the Gateway front door in AAP 2.5+ too - same fallback
    # chain as eda_base_url above.
    hub_base_url = (
        module.params.get("hub_host")
        or module.params.get("gateway_host")
        or module.params["controller_host"]
    ).rstrip("/")
    headers = build_auth_headers(module)

    components = []
    excluded = {}

    # hosts/groups are excluded by default (see INVENTORY_CONTENT_NAMES docstring above) -
    # only merged into the effective blocklist when include_inventory_content is false.
    # Built once here rather than checked inline per-endpoint so the "why excluded" reason
    # below can distinguish "genuinely not CaC" from "inventory content, opted out".
    effective_blocklist = RUNTIME_OR_META_BLOCKLIST | (
        set() if module.params["include_inventory_content"] else INVENTORY_CONTENT_NAMES
    )

    # --- Controller API root ---
    ctrl_index, ctrl_err = fetch_root_index(module, base_url, "/api/controller/v2/", headers)
    if ctrl_index:
        for name, path in ctrl_index.items():
            if name in effective_blocklist:
                reason = (
                    "controller: static inventory content, excluded by default "
                    "(set include_inventory_content: true to include)"
                    if name in INVENTORY_CONTENT_NAMES
                    else "controller: runtime/meta, not CaC-relevant"
                )
                excluded[name] = reason
                continue
            components.append({"name": name, "api_root": "controller", "path": _normalize_path(path)})
    else:
        excluded["_controller_root"] = ctrl_err

    # --- Gateway API root (auth, settings, RBAC assignments live here in 2.5+) ---
    if module.params["include_gateway"]:
        gw_index, gw_err = fetch_root_index(module, gw_base_url, "/api/gateway/v1/", headers)
        if gw_index:
            for name, path in gw_index.items():
                if name in GATEWAY_BLOCKLIST:
                    excluded["gateway:%s" % name] = "gateway: runtime/meta, not CaC-relevant"
                    continue
                components.append({"name": "gateway_%s" % name, "api_root": "gateway", "path": _normalize_path(path)})
        else:
            # Not fatal - some AAP versions/topologies may not expose /api/gateway/v1/ to this
            # token, or this AAP version predates the gateway split. Surface it, don't fail.
            excluded["_gateway_root"] = gw_err

    # --- EDA API root (Event-Driven Ansible controller, AAP 2.5+) ---
    if module.params["include_eda"]:
        eda_index, eda_err = fetch_root_index(module, eda_base_url, "/api/eda/v1/", headers)
        if eda_index:
            for name, path in eda_index.items():
                # Only "-list" entries are real paginated collections (see the long
                # comment above EDA_BLOCKLIST_BASENAMES) - everything else here
                # (config, session-login/logout, token-refresh, current-user,
                # openapi-*) is a single-action or meta endpoint, not a CaC object.
                if not name.endswith("-list"):
                    excluded["eda:%s" % name] = "eda: not a paginated collection (single-action/meta endpoint)"
                    continue
                base_name = name[: -len("-list")]
                if base_name in EDA_BLOCKLIST_BASENAMES:
                    excluded["eda:%s" % name] = "eda: runtime/meta, not CaC-relevant"
                    continue
                components.append({"name": "eda_%s" % base_name, "api_root": "eda", "path": _normalize_path(path)})
        else:
            # Not fatal - pre-2.5 AAP has no EDA at all, or this token lacks EDA access.
            # Same "surface it, don't fail" treatment as the Gateway root above.
            excluded["_eda_root"] = eda_err

    # --- Hub/Galaxy Pulp API root (content management, AAP 2.5+) ---
    if module.params["include_hub"]:
        hub_index, hub_err = fetch_root_index(module, hub_base_url, "/api/galaxy/pulp/api/v3/", headers)
        if hub_index:
            for name, path in hub_index.items():
                suffix = HUB_ALLOWLIST.get(name)
                if suffix is None:
                    excluded["hub:%s" % name] = (
                        "hub: out of v1 config-only scope (namespaces/remotes/"
                        "contentguards/access_policies only) - content, runtime, or "
                        "deferred pending further design (see HUB_ALLOWLIST comment)"
                    )
                    continue
                components.append({
                    "name": "hub_%s" % suffix, "api_root": "hub",
                    "path": _normalize_path(path),
                })
        else:
            # Not fatal - pre-2.5 AAP has no Hub API root, or this token lacks Hub
            # access. Same "surface it, don't fail" treatment as Gateway/EDA above.
            excluded["_hub_root"] = hub_err

    # --- Settings: hardcoded, not discovered from the root index walk (see include_settings
    # doc above for why) - kind="settings" tells the playbook to pass object_shape=dict to
    # aap_export_bulk instead of the normal paginated-list shape. skip_on_404 in
    # aap_export_bulk handles it cleanly if a given AAP version/topology doesn't have it.
    if module.params["include_settings"]:
        if ctrl_index:
            components.append({
                "name": "controller_settings", "api_root": "controller",
                "path": "/api/controller/v2/settings/all/", "kind": "settings",
            })
        if module.params["include_gateway"]:
            components.append({
                "name": "gateway_settings", "api_root": "gateway",
                "path": "/api/gateway/v1/settings/all/", "kind": "settings",
            })

    module.exit_json(changed=False, components=components, excluded=excluded)


if __name__ == "__main__":
    main()
