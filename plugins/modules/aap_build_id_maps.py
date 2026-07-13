#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r'''
---
module: aap_build_id_maps
short_description: Build id -> name lookup maps for CaC-relevant reference types
description:
  - Fetches a minimal (id, name) listing for a fixed set of object types that other
    objects commonly reference by id (organization, credential_type, execution_environment,
    inventory, project, credential, label, instance_group, user, team).
  - Runs ONCE per export, before the main per-component export loop. One process, one
    paginated GET per map_type - not one per object being resolved. This is the piece
    aap_export_bulk needs so it can rewrite foreign-key ids into names, making the
    exported YAML portable across AAP instances (source and target never share ids).
  - Does not fork per object and does not write any files - pure in-memory lookup building.
options:
  controller_host:
    type: str
    required: true
    description: Base URL of the AAP instance, used for the Controller API root.
  gateway_host:
    type: str
    required: false
    description: >
      Override if the Gateway API is served from a different host/port than the
      Controller. Same purpose as aap_export_bulk's gateway_host - kept independent
      per module rather than assumed equal, per the lesson learned from the original
      gateway-users id gap.
  eda_host:
    type: str
    required: false
    description: >
      Override if the EDA API is served from a different host/port than the Gateway.
      Required so this module can build id maps FROM the EDA API root for map_types
      that are EDA-scoped (eda_organizations, eda_credential_types,
      eda_decision_environments, eda_credentials) - see the "why this exists" note
      on MAP_TYPE_CONFIG below. Falls back to gateway_host, then controller_host.
  controller_username:
    type: str
    required: false
    description: Basic-auth username, alternative to controller_oauthtoken.
  controller_password:
    type: str
    required: false
    no_log: true
    description: Basic-auth password, used with controller_username.
  controller_oauthtoken:
    type: str
    required: false
    no_log: true
    description: OAuth2 bearer token. One of this or controller_username/controller_password is required.
  validate_certs:
    type: bool
    default: true
    description: Whether to validate TLS certificates on the AAP endpoint(s).
  map_types:
    type: list
    elements: str
    required: false
    description: >
      Which reference types to build maps for. Defaults to the full set this module
      knows how to resolve. Trim this list if you know a particular export doesn't
      need, say, users/teams resolved, to save a couple of API calls.
    default:
      - organizations
      - credential_types
      - execution_environments
      - inventories
      - projects
      - credentials
      - labels
      - instance_groups
      - users
      - teams
      - eda_organizations
      - eda_credential_types
      - eda_decision_environments
      - eda_credentials
      - eda_projects
  page_size:
    type: int
    default: 200
    description: API page size used when paginating each map-building request.
author: Kushal / CitiusCloud
'''

EXAMPLES = r'''
- name: Build the full default set of id->name maps
  aap_build_id_maps:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
  register: id_map_result

- name: Build only the maps this export actually needs (skip users/teams)
  aap_build_id_maps:
    controller_host: "https://aap.Test.example.com"
    gateway_host: "https://gateway.Test.example.com"
    eda_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
    map_types:
      - organizations
      - credential_types
      - execution_environments
      - inventories
      - projects
      - credentials
      - labels
      - instance_groups
  register: id_map_result
'''

RETURN = r'''
id_maps:
  description: >
    Dict keyed by map_type, each value a dict of {"<id>": "<name>"} (ids as strings,
    since they arrive as dict keys and Ansible facts stringify them anyway). Includes
    both Controller-scoped map_types (organizations, credential_types, ...) and
    EDA-scoped ones (eda_organizations, eda_credential_types,
    eda_decision_environments, eda_credentials) - these are DISTINCT id spaces even
    when the names look similar, since EDA is served from its own API root and is
    not guaranteed to share ids with Controller. aap_export_bulk.py's clean_object()
    picks the right one automatically based on which api_root the object being
    resolved came from.
  type: dict
  returned: always
errors:
  description: Any map_type that failed to build, with the reason. Non-fatal - export still proceeds with whatever maps DID succeed.
  type: dict
  returned: always
'''

import json

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import open_url
from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError
from ansible.module_utils.six.moves.urllib.parse import urlencode

# type -> (endpoint path, field to use as the "name", api_root)
# "users" uses username as its display/reference field, not "name" - AAP has no
# separate display name field on the user object that's used for FK purposes.
#
# WHY THE eda_* ENTRIES EXIST (root cause of the 2026-07-11 Test unresolved-FK
# failure on eda_activation/eda_edacredential/eda_eventstream): EDA is served through
# its own /api/eda/v1/ root and has its OWN id space for organizations,
# credential-types, decision-environments and credentials - it is NOT guaranteed to
# share ids with Controller's /api/controller/v2/ equivalents (confirmed live: this
# instance's eda_credentialtype discovery returned 28 objects vs Controller's
# credential_types returning 33 - different objects, different ids). Every
# aap_export_bulk fallback lookup for an EDA object's organization/credential_type/
# decision_environment field was checking id_maps["organizations"] /
# id_maps["credential_types"], which were built EXCLUSIVELY from Controller - so the
# fallback could only ever match by coincidence. This is the exact same class of bug
# `infra.aap_configuration_extended.filetree_create` hit and fixed for its EDA
# templates ("Fix organization query in EDA decision environments template,
# organizations endpoint was incorrect" / "Change decision_environment_id to
# organization_id in eda_rulebook_activations.yml" - both in its CHANGELOG): the fix
# in both cases is the same - query the API root the object actually came from, not
# always Controller. `summary_fields` on the object itself is still tried FIRST (see
# clean_object() in aap_export_bulk.py) - these eda_* maps are the fallback for
# whichever EDA fields don't carry a usable summary_fields entry, same "fallback
# only" relationship the original Controller-scoped maps have.
MAP_TYPE_CONFIG = {
    "organizations": ("/api/controller/v2/organizations/", "name", "controller"),
    "credential_types": ("/api/controller/v2/credential_types/", "name", "controller"),
    "execution_environments": ("/api/controller/v2/execution_environments/", "name", "controller"),
    "inventories": ("/api/controller/v2/inventories/", "name", "controller"),
    "projects": ("/api/controller/v2/projects/", "name", "controller"),
    "credentials": ("/api/controller/v2/credentials/", "name", "controller"),
    "labels": ("/api/controller/v2/labels/", "name", "controller"),
    "instance_groups": ("/api/controller/v2/instance_groups/", "name", "controller"),
    "users": ("/api/controller/v2/users/", "username", "controller"),
    "teams": ("/api/controller/v2/teams/", "name", "controller"),
    # EDA-scoped maps - queried against eda_host, not controller_host. See the note
    # above for why these are separate from their Controller-rooted namesakes.
    "eda_organizations": ("/api/eda/v1/organizations/", "name", "eda"),
    "eda_credential_types": ("/api/eda/v1/credential-types/", "name", "eda"),
    "eda_decision_environments": ("/api/eda/v1/decision-environments/", "name", "eda"),
    # Resolves the nested activation.event_streams[i].eda_credential embedded-object
    # id (see aap_export_bulk.py's EDA_EMBEDDED_LIST_FIELDS handling).
    "eda_credentials": ("/api/eda/v1/eda-credentials/", "name", "eda"),
    # EDA's project id space is separate from Controller's, exactly like
    # organizations/credential_types/decision_environments above - EDA syncs its own
    # project record (see aap_discover_components's eda_project at
    # /api/eda/v1/projects/) rather than sharing Controller's project ids. Missing
    # entry that let activation.project fall through to the Controller-scoped
    # "projects" map (matching only by coincidence) - the one field the 2026-07-11
    # eda_activation/eda_edacredential/eda_eventstream fix didn't cover.
    "eda_projects": ("/api/eda/v1/projects/", "name", "eda"),
}


def build_auth_headers(module):
    token = module.params.get("controller_oauthtoken")
    if token:
        return {"Authorization": "Bearer %s" % token}
    return {}


def fetch_id_name_pairs(module, base_url, path, name_field, headers, page_size):
    """Single-process pagination loop, same pattern as aap_export_bulk's fetch_all_pages,
    just requesting only id + the name field to keep payloads small."""
    result = {}
    params = {"page_size": page_size}
    next_url = "%s%s?%s" % (base_url, path, urlencode(params))

    while next_url:
        try:
            resp = open_url(
                next_url, headers=headers,
                url_username=module.params.get("controller_username"),
                url_password=module.params.get("controller_password"),
                force_basic_auth=bool(module.params.get("controller_username")),
                validate_certs=module.params.get("validate_certs"),
                method="GET", timeout=60,
            )
            data = json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError("HTTP %s fetching %s" % (e.code, next_url))
        except URLError as e:
            raise RuntimeError("Unreachable: %s (%s)" % (next_url, str(e)))

        for obj in data.get("results", []):
            obj_id = obj.get("id")
            name_val = obj.get(name_field)
            if obj_id is not None and name_val is not None:
                result[str(obj_id)] = name_val

        next_page = data.get("next")
        next_url = next_page if (next_page and next_page.startswith("http")) else (
            "%s%s" % (base_url, next_page) if next_page else None
        )

    return result


def main():
    module_args = dict(
        controller_host=dict(type="str", required=True),
        gateway_host=dict(type="str", required=False),
        eda_host=dict(type="str", required=False),
        controller_username=dict(type="str", required=False),
        controller_password=dict(type="str", required=False, no_log=True),
        controller_oauthtoken=dict(type="str", required=False, no_log=True),
        validate_certs=dict(type="bool", default=True),
        map_types=dict(type="list", elements="str", required=False,
                        default=list(MAP_TYPE_CONFIG.keys())),
        page_size=dict(type="int", default=200),
    )
    module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)

    controller_base_url = module.params["controller_host"].rstrip("/")
    gateway_base_url = (module.params.get("gateway_host") or module.params["controller_host"]).rstrip("/")
    eda_base_url = (
        module.params.get("eda_host")
        or module.params.get("gateway_host")
        or module.params["controller_host"]
    ).rstrip("/")
    base_url_by_root = {
        "controller": controller_base_url,
        "gateway": gateway_base_url,
        "eda": eda_base_url,
    }
    headers = build_auth_headers(module)
    page_size = module.params["page_size"]

    id_maps = {}
    errors = {}

    for map_type in module.params["map_types"]:
        if map_type not in MAP_TYPE_CONFIG:
            errors[map_type] = "Unknown map_type - not in MAP_TYPE_CONFIG, skipped."
            continue
        path, name_field, api_root = MAP_TYPE_CONFIG[map_type]
        base_url = base_url_by_root[api_root]
        try:
            id_maps[map_type] = fetch_id_name_pairs(
                module, base_url, path, name_field, headers, page_size
            )
        except RuntimeError as e:
            # Non-fatal: e.g. this AAP version/token may not have access to one type
            # (older AAP without EDA, or a token without EDA scope - handled the same
            # way an unreachable Controller endpoint always was). Export should still
            # proceed - fields that would've resolved via this map just stay as raw
            # ids, which aap_export_bulk should flag rather than hide.
            errors[map_type] = str(e)
            id_maps[map_type] = {}

    module.exit_json(changed=False, id_maps=id_maps, errors=errors)


if __name__ == "__main__":
    main()