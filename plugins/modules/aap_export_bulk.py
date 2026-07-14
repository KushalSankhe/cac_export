#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r'''
---
module: aap_export_bulk
short_description: Bulk export AAP objects via direct API calls (no filetree_create forking)
description:
  - Replaces infra.aap_configuration_extended's filetree_create for large exports.
  - Fetches paginated data from the Controller/Gateway API in a single Python process,
    accumulates in memory, and writes ONE file per object type at the end.
  - Avoids the per-object fork/write pattern that exhausts OCP pids_limit on large exports.
options:
  controller_host:
    type: str
    required: true
    description: >
      Base URL of the AAP Controller, e.g. https://aap.example.com. Used as the
      fallback base_url for gateway_host/eda_host if those aren't set, AND as the
      base_url whenever api_root is "controller" or unset (object_type legacy mode is
      always controller-rooted).
  gateway_host:
    type: str
    required: false
    description: >
      Base URL to use when api_root is "gateway". Defaults to controller_host. Needed
      because explicit_path values from aap_discover_components can be relative paths
      (Controller/Gateway) that must be joined against the RIGHT host - joining a
      gateway-rooted path against controller_host silently fetches the wrong thing (or
      nothing) whenever gateway_host and controller_host genuinely differ.
  eda_host:
    type: str
    required: false
    description: >
      Base URL to use when api_root is "eda". Defaults to gateway_host (falling back to
      controller_host). Also see api_root - this only matters when explicit_path is a
      relative path; if aap_discover_components returned an absolute URL (confirmed
      behavior of the live EDA root, with an unreliable internal scheme/host baked in)
      it's normalized to a relative path before it ever reaches this module, so this
      host is what actually gets used to reach it, not whatever host the API
      self-reported.
  hub_host:
    type: str
    required: false
    description: >
      Base URL to use when api_root is "hub". Defaults to gateway_host (falling back
      to controller_host). Same reasoning as eda_host above - aap_discover_components
      already normalizes any absolute-URL path from the Hub/Galaxy Pulp root before it
      reaches this module, so this host is what actually gets used to reach it.
  api_root:
    type: str
    required: false
    default: controller
    choices: [controller, gateway, eda, hub]
    description: >
      Which host param (controller_host/gateway_host/eda_host/hub_host) explicit_path
      should be joined against. Pass item.api_root straight through when using
      dynamic-discovery mode. Irrelevant (and ignored) in legacy object_type mode -
      that's always controller-rooted.
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
  object_type:
    type: str
    required: false
    choices: [organizations, teams, users, credential_types, credentials, credential_input_sources,
              execution_environments, instance_groups, projects, inventories, constructed_inventories,
              inventory_sources, job_templates, workflow_job_templates, workflow_job_template_nodes,
              schedules, notification_templates, labels, role_definitions, role_user_assignments,
              role_team_assignments]
    description: Use this OR (component_name + explicit_path). Kept for backward compatibility / manual runs.
  component_name:
    type: str
    required: false
    description: Friendly name used for the output filename, when using explicit_path (dynamic discovery mode).
  explicit_path:
    type: str
    required: false
    description: >
      Full API path (e.g. from aap_discover_components' output) to use instead of the
      built-in object_type table. Enables exporting endpoints not hardcoded in this module.
  skip_on_404:
    type: bool
    default: true
    description: If true, a 404 on the object's endpoint is treated as "not applicable to this AAP version" and returns count=0 instead of failing the task.
  output_path:
    type: str
    required: true
    description: Full path to the YAML file to write, e.g. /cac/dev/job_templates.yml
  output_var_name:
    type: str
    required: false
    description: >
      Top-level YAML key to write the exported data under. Defaults to object_type
      (the raw discovered/component name) when omitted, matching the historical
      behavior. Pass this to write directly under a different variable name at
      export time - e.g. the dispatch-role variable name (aap_organizations,
      controller_templates, ...) from vars/dispatch_component_map.yml - so the
      file is already dispatch-ready with no separate rename/copy pass needed.
  page_size:
    type: int
    default: 200
    description: API page size used when paginating this component's endpoint.
  retry_max_attempts:
    type: int
    default: 3
    description: >
      Total attempts (including the first) for any single GET before giving up on it.
      Only retried for transient failures - network-level errors (DNS/connection/
      timeout/TLS, no HTTP response at all) and HTTP 429/500/502/503/504. A 401/403/404
      or any other 4xx is never retried, since a retry can't change a deterministic
      outcome. Set to 1 to disable retrying entirely (original single-attempt behavior).
  retry_backoff_seconds:
    type: float
    default: 2
    description: >
      Base delay before the first retry; doubles each subsequent attempt (2, 4, 8, ...
      by default). Set to 0 for immediate retries with no delay.
  organization_filter:
    type: str
    required: false
    description: Optional org name to filter results by (reduces payload for multi-tenant AAP)
  extra_query_params:
    type: dict
    required: false
    default: {}
    description: >
      Ad hoc extra query-string filters ANDed onto the request, e.g. {"id": 42}
      to restrict a component to a single object by id, or {"name": "..."} for
      name-keyed lookups like labels.
  id_maps:
    type: dict
    required: false
    default: {}
    description: >
      Output of aap_build_id_maps' id_maps return value, e.g. {"organizations":
      {"1": "Default"}, "credential_types": {"5": "Machine"}, ...}. Used as a FALLBACK
      only - foreign-key fields are resolved primarily from each object's own
      summary_fields (present on every AWX/AAP API response and always in sync with
      whichever api_root - controller or gateway - the object came from). id_maps is
      only consulted when summary_fields doesn't carry an entry for that field. Omit
      or leave empty to rely on summary_fields alone.
  object_shape:
    type: str
    choices: [list, dict]
    default: list
    description: >
      "list" (default) is the normal paginated-collection export (organizations,
      credentials, job_templates, ...). "dict" is for single-object endpoints like
      /settings/all/ that return one flat JSON object, not a paginated {"results": [...]}
      collection - no pagination loop is run, and the output YAML's top-level key maps
      to a dict instead of a list. Used by the controller_settings/gateway_settings
      components.
  secrets_as_variables:
    type: bool
    default: true
    description: >
      AAP masks any encrypted field's real value with the literal string "$encrypted$"
      in every API GET response (it never returns real secret values over the API,
      by design). Exporting that literal string as-is would mean re-importing this
      YAML sets the field to the 4-character string "$encrypted$", not a real secret.
      When true (default), every field found equal to that marker - anywhere in the
      object, including nested dicts like a credential's "inputs" - is rewritten to a
      Jinja reference {{ <secrets_as_variables_prefix>_<object_type>_<object_name>_<field_path> }}
      instead, so the exported YAML is reimport-safe as long as that variable is
      supplied (e.g. from an ansible-vault-encrypted vars file) at apply time. Set to
      false to leave "$encrypted$" markers as literal strings (matches old behavior).
  secrets_as_variables_prefix:
    type: str
    default: vaulted
    description: Prefix used when building the Jinja variable names described under secrets_as_variables.
  yaml_mark_unsafe_templates:
    type: bool
    default: true
    description: >
      Fields that legitimately carry AAP-runtime Jinja content (extra_vars, extra_data,
      source_vars, variables, survey_spec) can contain literal text like
      "{{ inventory_hostname }}" that AAP itself evaluates at job-run time, not Ansible.
      When true (default), any such Jinja-looking string inside those fields is tagged
      with YAML's !unsafe tag on write, so Ansible's own loader (vars_files/
      include_vars/ansible-playbook) never re-templates it on reimport. This makes the
      output file require Ansible's YAML loader to read correctly - plain PyYAML
      (yaml.safe_load) will raise on the !unsafe tag. Set false to write plain strings
      instead (old behavior; only safe if you know reimport never templates these
      fields).
  skip_managed_objects:
    type: bool
    default: true
    description: >
      When true (default), objects returned by the API with "managed": true are
      dropped from the export before write. These are Red Hat-shipped built-ins
      (Platform Auditor / Organization Admin role definitions, Machine / AWS /
      Vault credential types, Default organization, rh-certified / community
      collection remotes, Local Database Authenticator, ...) that exist
      identically on every target AAP install; the write endpoints reject
      re-application with errors like "Local custom roles can only include view
      permission for shared models" or "The authenticator you have picked is
      already configured to auto migrate users...". References to these objects
      by name still resolve on the target because the same name exists there,
      so dropping them from the export loses nothing downstream. Set to false
      for a full audit-style dump you don't intend to dispatch.
  skip_system_usernames:
    type: list
    elements: str
    default: []
    description: >
      Usernames to drop from the export. Intended for accounts created
      automatically by the AAP operator on OpenShift (_token_service_user,
      aap_operator_service_account) that don't carry managed=true but are
      equally not user-managed. Only checked on objects that carry a
      'username' field, so passing a list here on non-user components is a
      safe no-op.
author: Kushal / CitiusCloud
'''

EXAMPLES = r'''
- name: Bulk export job templates
  aap_export_bulk:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
    object_type: job_templates
    output_path: "/cac/dev/job_templates.yml"
    organization_filter: "Test-DEV"

- name: Bulk export with a more patient retry policy for a flaky link
  aap_export_bulk:
    controller_host: "https://aap.Test.example.com"
    controller_oauthtoken: "{{ aap_token }}"
    validate_certs: false
    object_type: credentials
    output_path: "/cac/dev/credentials.yml"
    retry_max_attempts: 5
    retry_backoff_seconds: 3
'''

RETURN = r'''
count:
  description: Number of objects exported
  type: int
  returned: always
output_path:
  description: Path the YAML was written to
  type: str
  returned: always
unresolved_fk_count:
  description: >
    Number of foreign-key values that were eligible for resolution (field name matched
    a known FK field) but had no matching entry in the supplied id_maps - these were
    left as raw ids in the output. A non-zero count usually means id_maps was built
    with a trimmed map_types list, or the referenced object is outside the exported
    scope (e.g. a credential in an org you filtered out).
  type: int
  returned: always
secret_vars:
  description: >
    List of Jinja variable names created by secrets_as_variables (e.g.
    "vaulted_credentials_My_Cred_inputs_password"). Empty if secrets_as_variables was
    false or nothing in this component's export had an encrypted field. The playbook
    aggregates this across every component into one vaulted-vars template file.
  type: list
  returned: always
secrets_replaced_count:
  description: Number of "$encrypted$" markers rewritten to Jinja variable references. Same length as secret_vars.
  type: int
  returned: always
unsafe_templates_tagged:
  description: >
    Number of Jinja-looking string values (inside extra_vars/extra_data/source_vars/
    variables/survey_spec fields) tagged !unsafe on write. 0 if
    yaml_mark_unsafe_templates was false or nothing matched.
  type: int
  returned: always
skipped_managed_count:
  description: >
    Number of objects dropped from the export because they carried
    "managed": true. Always 0 when skip_managed_objects is false or the
    component's endpoint doesn't return managed objects (e.g. teams).
  type: int
  returned: always
skipped_username_count:
  description: >
    Number of objects dropped from the export because their 'username' matched
    an entry in skip_system_usernames. Always 0 when skip_system_usernames is
    empty or the component isn't a user endpoint.
  type: int
  returned: always
'''

import json
import re
import socket
import time
import yaml

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import open_url
from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError
from ansible.module_utils.six.moves.urllib.parse import urlencode, urlparse

# HTTP status codes worth retrying - transient server-side/proxy trouble, not something
# a retry would ever fix on 401/403/404 (those are deterministic given the same token/path).
RETRYABLE_HTTP_CODES = frozenset([429, 500, 502, 503, 504])


# Map friendly object_type -> API endpoint path.
# Verified against actual /api/controller/v2/ root discovery on Test AAP instance.
# NOTE: "applications" (OAuth2 apps) is intentionally excluded from THIS static table -
# it does not appear in the Controller API root at all (moved under the Gateway's own
# API in the 2.5+ gateway split, different base path/auth). This only affects the legacy
# object_type= static-table mode; the normal explicit_path dynamic-discovery mode (what
# export_bulk.yml actually uses) picks up Gateway-rooted "applications" automatically via
# aap_discover_components' generic root-index walk as "gateway_applications", since
# "applications" was never added to that module's GATEWAY_BLOCKLIST. Confirmed working
# against a live instance (gateway_applications.yml + a vaultized client_secret var were
# produced in a real export run) - see CLAUDE.md milestone 5.
#
# Only CONFIG objects are included here (desired-state, CaC-relevant). Runtime/history
# objects (jobs, workflow_jobs, project_updates, inventory_updates, notifications,
# activity_stream, unified_jobs, ad_hoc_commands, metrics, etc.) are deliberately excluded -
# those are execution artifacts, not configuration, and exporting them as "CaC" doesn't make sense.
API_PATHS = {
    "organizations": "/api/controller/v2/organizations/",
    "teams": "/api/controller/v2/teams/",
    "users": "/api/controller/v2/users/",
    "credential_types": "/api/controller/v2/credential_types/",
    "credentials": "/api/controller/v2/credentials/",
    "credential_input_sources": "/api/controller/v2/credential_input_sources/",
    "execution_environments": "/api/controller/v2/execution_environments/",
    "instance_groups": "/api/controller/v2/instance_groups/",
    "projects": "/api/controller/v2/projects/",
    "inventories": "/api/controller/v2/inventories/",
    "constructed_inventories": "/api/controller/v2/constructed_inventories/",
    "inventory_sources": "/api/controller/v2/inventory_sources/",
    "job_templates": "/api/controller/v2/job_templates/",
    "workflow_job_templates": "/api/controller/v2/workflow_job_templates/",
    "workflow_job_template_nodes": "/api/controller/v2/workflow_job_template_nodes/",
    "schedules": "/api/controller/v2/schedules/",
    "notification_templates": "/api/controller/v2/notification_templates/",
    "labels": "/api/controller/v2/labels/",
    "role_definitions": "/api/controller/v2/role_definitions/",
    "role_user_assignments": "/api/controller/v2/role_user_assignments/",
    "role_team_assignments": "/api/controller/v2/role_team_assignments/",
}

# Fields to strip from each object before writing to CaC YAML. Everything here
# is either server-generated (audit metadata, timestamps, primary keys), runtime
# state (last_login, status, counters), or a duplicate of a name-resolved field
# left over from FK resolution. Grouped for readability, but this is one flat
# blocklist applied globally to every exported object at every nesting level
# clean_object touches. Add here first when a new "read-only by setting" or
# "cannot be modified" error surfaces on dispatch - almost always the fix.
STRIP_FIELDS = [
    # --- server-assigned primary key + related links ---
    "id", "url", "related", "summary_fields", "type", "prn", "pulp_href",

    # --- AAP 2.6+ audit metadata (older 'created'/'modified' kept for pre-2.6) ---
    "created", "modified",
    "created_at", "modified_at", "edited_at", "last_synced_at",
    "created_by", "modified_by", "edited_by",
    "pulp_created", "pulp_last_updated",

    # --- runtime state (jobs, projects, activations, inventories, IGs) ---
    "last_login", "last_login_from", "last_login_results",
    "last_job_run", "last_job_failed", "last_update_failed", "last_updated",
    "next_job_run", "status", "status_message",
    "scm_revision", "git_hash", "import_state", "import_error", "last_sync_task",
    "restart_count", "rules_count", "rules_fired_count", "current_job_id",
    "events_received", "last_event_received_at", "log_tracking_id",
    "has_active_failures", "has_inventory_sources",
    "hosts_with_active_failures", "inventory_sources_with_failures",
    "total_groups", "total_hosts", "total_inventory_sources", "pending_deletion",
    "capacity", "consumed_capacity", "instances",
    "jobs_running", "jobs_total", "percent_capacity_remaining",
    "hidden_fields", "references",

    # --- source-env DB IDs that duplicate a name-resolved field ---
    # clean_object writes the resolved name under the bare field name; these
    # '_id' shadows are only useful during resolution and become cross-env-
    # invalid junk once written to YAML.
    "organization_id", "project_id", "credential_type_id",
    "decision_environment_id", "rulebook_id", "eda_credential_id",
    "signature_validation_credential_id", "rule_engine_credential_id",
    "awx_token_id",

    # --- deprecated user fields (superseded by associated_authenticators) ---
    "authenticator_uid", "authenticators",

    # --- 'managed' flag itself: stripped AFTER the skip_managed_objects filter
    #     in main() has already used it, so it doesn't leak into the output
    #     (dispatch treats it as an unknown field on some object types). ---
    "managed",

    # --- controller/gateway SETTINGS keys that /settings/all/ returns on GET
    #     but the write endpoint rejects (server-computed or install-fixed).
    #     Only settings ever have these keys, so a global entry is safe. ---
    "jwt_public_key", "jwt_private_key",
    "INSTALL_UUID", "LICENSE", "IS_K8S",
    "AUTOMATION_ANALYTICS_LAST_GATHER", "AUTOMATION_ANALYTICS_LAST_ENTRIES",
    "CLEANUP_HOST_METRICS_LAST_TS", "HOST_METRIC_SUMMARY_TASK_LAST_TS",
    "NAMED_URL_FORMATS", "NAMED_URL_GRAPH_NODES",

    # --- legacy noise from earlier list ---
    "custom_virtualenv", "job_tags_no_ui", "skip_tags_no_ui",
]

# Scalar FK fields: field name -> which id_maps key resolves it AS A FALLBACK.
# These hold a single raw id in the API response and get rewritten to a single name.
# Primary resolution is via the object's own summary_fields (see clean_object) - the
# map_type here only matters if summary_fields doesn't have an entry for that field.
# It's fine for a map_type to have no corresponding aap_build_id_maps entry (e.g.
# role_definitions, authenticators below) - the fallback lookup just safely misses
# and resolution relies entirely on summary_fields for that field.
FK_SCALAR_FIELDS = {
    "organization": "organizations",
    "credential_type": "credential_types",
    "execution_environment": "execution_environments",
    "default_environment": "execution_environments",
    "inventory": "inventories",
    "project": "projects",
    "source_project": "projects",
    "credential": "credentials",
    "vault_credential": "credentials",
    "user": "users",
    "team": "teams",
    "role_definition": "role_definitions",
    "authenticator": "authenticators",
    # EDA (rulebook_activation.decision_environment, ...). No aap_build_id_maps entry
    # exists for this map_type - same "summary_fields does the real work, this is only
    # a safety-net fallback" situation as role_definition/authenticator above.
    "decision_environment": "decision_environments",
}

# List FK fields: field name -> which id_maps key resolves each element.
# These hold a list of raw ids and get rewritten to a list of names.
FK_LIST_FIELDS = {
    "instance_groups": "instance_groups",
    "credentials": "credentials",
    "labels": "labels",
}

# When resolving a field on an EDA-rooted object (api_root == "eda"), the id_maps
# fallback must check the EDA-scoped map (built by aap_build_id_maps against
# eda_host), not the Controller-scoped map of the same name - EDA has its own id
# space for these (see aap_build_id_maps.py's MAP_TYPE_CONFIG comment for the full
# story / the live bug this fixes). Only fields where a same-named-but-wrong-root
# map previously existed need an entry here.
EDA_FK_MAP_OVERRIDE = {
    "organizations": "eda_organizations",
    "credential_types": "eda_credential_types",
    "decision_environments": "eda_decision_environments",
    # activation.project - missed in the original 2026-07-11 EDA-scoped-maps fix.
    # Same root cause as the other three: EDA has its own project id space, not
    # shared with Controller's /api/controller/v2/projects/.
    "projects": "eda_projects",
}

# Fields whose value is a list of FULLY EMBEDDED related objects, not raw ids -
# specific to EDA's rulebook_activation API shape (activation.eda_credentials,
# activation.event_streams). These are NOT covered by FK_LIST_FIELDS (that's for
# lists of raw ids) - previously they weren't touched by clean_object() at all, so
# any FK fields *inside* these embedded objects (their own organization,
# credential_type, ...) stayed as raw ids AND never incremented unresolved_fk_count,
# a silent gap the fail-fast check in the playbook couldn't see. Each item gets the
# same scalar-FK resolution treatment as a top-level object, recursively.
EDA_EMBEDDED_LIST_FIELDS = {
    "eda_credentials": "eda",
    "event_streams": "eda",
}

# A field name that shows up INSIDE one item of an EDA_EMBEDDED_LIST_FIELDS list and
# is itself another fully embedded single object needing the same treatment again -
# concretely: event_streams[i].eda_credential. Resolved against the "eda_credentials"
# id_maps entry (aap_build_id_maps.py) as its fallback map.
EDA_NESTED_SINGLE_OBJECT_FIELDS = {
    "eda_credential": "eda_credentials",
}

# The literal string AAP substitutes for ANY encrypted field's real value on every API
# GET response (credential inputs, user passwords, notification template tokens, gateway
# authenticator client secrets, settings like SOCIAL_AUTH_*_SECRET, ...). It's not
# specific to one object type or one field name, so detection is done by VALUE, not by a
# hand-maintained field-name list - this is deliberately the same "don't hand-maintain
# what the API already tells you" philosophy as aap_discover_components.
ENCRYPTED_MARKER = "$encrypted$"

# --- Milestone 4: YAML safety for survey/extra_vars fields -----------------------------
#
# Investigated two distinct failure modes filetree_create's CHANGELOG attributes to this
# bug class, and they turned out to need two different (or no) fixes:
#
#   1. "Numeric-looking strings get corrupted" (e.g. "123-123-123", "0123", "yes") -
#      TESTED against this module's actual yaml.safe_dump/safe_load round-trip and this
#      is NOT a live bug here. PyYAML's SafeDumper already quotes any plain scalar that
#      would otherwise resolve to a non-str implicit type on load (int/float/bool/null/
#      timestamp), and safe_load reads it back as the identical string. No fix needed;
#      documented here so this isn't silently "forgotten" as unresolved.
#   2. "Jinja-looking strings get re-interpreted as templates on reimport" - THIS is a
#      real, distinct risk: a field like extra_vars can legitimately contain literal
#      text like "{{ inventory_hostname }}" that AAP itself evaluates at job-run time.
#      YAML round-trips that string correctly, but if the CaC reimport playbook ever
#      passes it through Ansible's OWN Jinja templar (e.g. a templated module arg, or a
#      vars_files/include_vars load), Ansible will try to render it too - wrong value or
#      an "undefined variable" failure, and no yaml.safe_dump/load option can express
#      "don't template this" since that's an Ansible-layer concept, not a YAML one.
#      Fixed below by emitting an explicit `!unsafe` YAML tag (the same mechanism
#      Ansible core itself uses for no_log/unsafe data) on Jinja-looking values inside
#      fields that legitimately carry AAP-runtime-evaluated content. Ansible's own YAML
#      loader (AnsibleLoader, used by vars_files/include_vars/ansible-playbook) turns
#      `!unsafe`-tagged scalars into AnsibleUnsafeText, which its Jinja templar skips
#      unconditionally. Plain PyYAML (yaml.safe_load) does NOT know this tag and will
#      raise a ConstructorError on it - that's an intentional tradeoff: these output
#      files are Ansible CaC artifacts meant to be consumed by Ansible, not generic YAML.
#
# Fields that legitimately carry AAP-runtime Jinja content, per AAP's own object model
# (job template / workflow job template / schedule run-time variables, workflow node
# per-node variable overrides, inventory/inventory-source variable blobs, and survey
# question defaults - the last of these isn't exported by this collection today, since
# survey_spec lives on a separate sub-endpoint this module doesn't fetch yet, but is
# listed here so tagging kicks in for free the moment that's added).
UNSAFE_TAG_FIELDS = {"extra_vars", "extra_data", "source_vars", "variables", "survey_spec"}

JINJA_MARKERS = ("{{", "{%")


class _UnsafeStr(str):
    """Marker subclass. Any value of this type is emitted with an explicit !unsafe YAML
    tag by _CaCDumper below, instead of the default plain string tag."""
    pass


def _represent_unsafe_str(dumper, data):
    return dumper.represent_scalar("!unsafe", str(data))


class _CaCDumper(yaml.SafeDumper):
    """SafeDumper plus one extra representer for _UnsafeStr. Behaves identically to
    yaml.safe_dump for every other Python type - this is not a general-purpose Dumper
    swap, just SafeDumper with one additional tag registered."""
    pass


_CaCDumper.add_representer(_UnsafeStr, _represent_unsafe_str)


def _looks_like_template(value):
    return isinstance(value, str) and any(marker in value for marker in JINJA_MARKERS)


def _wrap_templated_strings(value, counter):
    """Recursively walk value (dict/list/scalar); any string containing Jinja-looking
    syntax becomes an _UnsafeStr so the dumper tags it !unsafe, and counter[0] is
    incremented. Only called on the handful of fields in UNSAFE_TAG_FIELDS - NOT applied
    blindly to every string in the object, since most fields (names, descriptions,
    hostnames, ...) were never at risk of being re-templated on reimport and don't need
    the tag's plain-PyYAML-incompatible tradeoff. Must run AFTER vaultize_secrets() - the
    {{ vaulted_..._password }} Jinja references that secrets_as_variables creates are
    INTENTIONALLY meant to be templated by Ansible at reimport time (that's the whole
    point of that feature), and none of UNSAFE_TAG_FIELDS overlaps with where
    vaultize_secrets writes those references, so there's no conflict as long as this
    ordering is preserved."""
    if isinstance(value, dict):
        return {k: _wrap_templated_strings(v, counter) for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap_templated_strings(v, counter) for v in value]
    if _looks_like_template(value):
        counter[0] += 1
        return _UnsafeStr(value)
    return value


def mark_unsafe_templates(cleaned_obj, enabled=True):
    """Entry point: for each field in UNSAFE_TAG_FIELDS present on cleaned_obj, wrap any
    Jinja-looking string found anywhere inside that field's value (however nested) as
    _UnsafeStr. Returns (cleaned_obj, tagged_count); mutates cleaned_obj in place too."""
    counter = [0]
    if not enabled or not isinstance(cleaned_obj, dict):
        return cleaned_obj, 0
    for field in UNSAFE_TAG_FIELDS:
        if field in cleaned_obj:
            cleaned_obj[field] = _wrap_templated_strings(cleaned_obj[field], counter)
    return cleaned_obj, counter[0]


def _safe_var_token(value):
    """Lowercase, alnum-and-underscore-only token safe for use inside a Jinja/Python variable name."""
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_").lower()
    return token or "field"


def vaultize_secrets(value, name_parts):
    """
    Recursively walk `value` (dict / list / scalar). Any scalar found exactly equal to
    ENCRYPTED_MARKER is replaced with a Jinja reference "{{ <var_name> }}", where
    var_name is built from name_parts (e.g. [prefix, object_type, object_name]) plus
    the field's path within the object (e.g. ["inputs", "password"]).

    Returns (new_value, [var_names_created]). Order of var_names_created matches
    encounter order; duplicates are possible if the same path repeats across list
    elements and are left in (the playbook dedupes before writing the vault template).
    """
    created = []

    def walk(v, path_parts):
        if isinstance(v, dict):
            return {k: walk(sub_v, path_parts + [k]) for k, sub_v in v.items()}
        if isinstance(v, list):
            return [walk(sub_v, path_parts + [str(i)]) for i, sub_v in enumerate(v)]
        if v == ENCRYPTED_MARKER:
            var_name = "_".join([_safe_var_token(p) for p in (name_parts + path_parts)])
            created.append(var_name)
            return "{{ %s }}" % var_name
        return v

    return walk(value, []), created


def blank_encrypted_markers(value):
    """Recursively walk value; every scalar equal to ENCRYPTED_MARKER becomes ''.
    Used when secrets_as_variables=False, so the output stays reimport-safe
    (matches infra.aap_configuration_extended.filetree_create's behavior of
    emptying encrypted fields rather than emitting the literal marker string).

    Previously, secrets_as_variables=False was a no-op on encrypted fields -
    the "$encrypted$" markers were left as-is in the output YAML, which meant
    reimporting that YAML would set every previously-encrypted field's value
    to the literal 12-character string "$encrypted$", silently corrupting
    credentials/passwords/tokens on the target. That's not a valid state for
    ANY user; kept as ''-fill on this path with no opt-out."""
    if isinstance(value, dict):
        return {k: blank_encrypted_markers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [blank_encrypted_markers(v) for v in value]
    if value == ENCRYPTED_MARKER:
        return ""
    return value


def build_auth_headers(module):
    token = module.params.get("controller_oauthtoken")
    if token:
        return {"Authorization": "Bearer %s" % token}
    # Fallback to basic auth handled via open_url's url_username/url_password args
    return {}


def _do_get_once(module, url, headers):
    """Single GET attempt, no retry. Returns (data_dict, None) on success,
    (None, (code, body)) on HTTPError, or (None, (None, message)) on a network-level
    failure (URLError/timeout/socket error - no HTTP status at all, since the request
    never got a response)."""
    try:
        resp = open_url(
            url,
            headers=headers,
            url_username=module.params.get("controller_username"),
            url_password=module.params.get("controller_password"),
            force_basic_auth=bool(module.params.get("controller_username")),
            validate_certs=module.params.get("validate_certs"),
            method="GET",
            timeout=60,
        )
        return json.loads(resp.read()), None
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = "<no body>"
        return None, (e.code, body)
    except (URLError, socket.timeout) as e:
        # No HTTP response at all - DNS failure, connection refused, TLS handshake
        # failure, read timeout, etc. code=None distinguishes this from a real HTTP
        # error code so the retry/fail logic below can treat it as always-retryable.
        return None, (None, str(getattr(e, "reason", e)))


def _do_get(module, url, headers):
    """GET with retry/backoff for transient failures. Returns the same shape as
    _do_get_once: (data_dict, None) on success, (None, (code, body)) on a failure that
    either isn't retryable or that exhausted all attempts.

    Retries on: network-level failures (code is None - DNS/connection/timeout/TLS), and
    HTTP responses in RETRYABLE_HTTP_CODES (429/500/502/503/504) - i.e. exactly the
    class of failure a second attempt might plausibly succeed at. Does NOT retry on
    401/403/404 or any other 4xx - those are deterministic given the same token/path
    and a retry would just waste the backoff window before failing the same way.

    Backoff is exponential with the configurable base (retry_backoff_seconds), doubling
    each attempt: base, base*2, base*4, ... up to retry_max_attempts total tries (the
    original attempt plus retry_max_attempts-1 retries). A single 47-component paginated
    export making dozens of page requests per component is exactly the scenario where
    one transient 5xx/network blip used to fail the whole play with no resume - this
    absorbs that without needing operator intervention or a play restart.
    """
    max_attempts = max(1, module.params.get("retry_max_attempts") or 1)
    backoff_base = module.params.get("retry_backoff_seconds") or 0

    attempt = 1
    while True:
        data, err = _do_get_once(module, url, headers)
        if err is None:
            return data, None

        code, body = err
        retryable = (code is None) or (code in RETRYABLE_HTTP_CODES)

        if not retryable or attempt >= max_attempts:
            if code is None:
                # Normalize the network-failure case to look like an HTTP error to
                # every downstream caller, which only branches on 404/403/401/else.
                # A network failure should land in the "else" (real, non-skippable
                # failure) branch, not silently disappear.
                return None, (599, "Network error after %d attempt(s): %s" % (attempt, body))
            return None, err

        sleep_for = backoff_base * (2 ** (attempt - 1))
        if sleep_for > 0:
            time.sleep(sleep_for)
        attempt += 1


def fetch_single_object(module, base_url, path, headers):
    """
    For object_shape='dict' endpoints (e.g. /settings/all/) that return ONE flat JSON
    object, not a paginated {"results": [...]} collection. No pagination loop - a
    single GET. Status dict shape matches fetch_all_pages' so main() can share the
    same skip/fail branching for both shapes.
    """
    url = "%s%s" % (base_url, path)
    data, err = _do_get(module, url, headers)

    if err is None:
        return data, {"ok": True}

    code, body = err
    if code == 404:
        return None, {
            "ok": False, "skipped": True,
            "reason": "404 Not Found - endpoint %s does not exist on this AAP instance (component not applicable)" % path,
        }
    elif code == 403:
        return None, {
            "ok": False, "skipped": False, "http_code": 403,
            "reason": "403 Forbidden - token/user lacks permission to read %s. Body: %s" % (path, body),
        }
    elif code == 401:
        return None, {
            "ok": False, "skipped": False, "http_code": 401,
            "reason": "401 Unauthorized - token invalid/expired for %s. Body: %s" % (path, body),
        }
    else:
        return None, {
            "ok": False, "skipped": False, "http_code": code,
            "reason": "HTTP %s error on %s. Body: %s" % (code, path, body),
        }


def _is_org_field_error(code, body):
    """
    Detects AAP's specific 400 response when a model has no 'organization' field to filter
    on (Organization, User, CredentialType, Schedule, WorkflowJobTemplateNode, RBAC objects,
    etc). Not every object type is org-scoped - this lets us drop the filter for THOSE
    endpoints instead of hard-failing the whole export.
    """
    if code != 400:
        return False
    body_lower = body.lower()
    return "has no field named" in body_lower and "organization" in body_lower


def fetch_all_pages(module, base_url, path, headers, page_size, org_filter, extra_params):
    """
    Single-process pagination loop. No Ansible looping, no forking.
    Accumulates every page's results into one list in memory.

    Automatically retries WITHOUT the organization filter if the model doesn't support
    org-scoping at all (AAP returns a specific 400 for that) - this is normal for objects
    like Organization/User/CredentialType/Schedule/RBAC assignments, not a real failure.

    Returns a tuple: (results_list, status_dict)
    status_dict = {"ok": True, "org_filter_applied": bool} on success, or
                  {"ok": False, "skipped": True, "reason": "..."} for 404 (component not
                  applicable to this AAP version/instance - not a real failure), or
                  {"ok": False, "skipped": False, "reason": "...", "http_code": N} for
                  real failures (401/403/500/etc) that the caller should still fail on.
    """
    results = []
    org_filter_applied = bool(org_filter)

    def build_params():
        p = {"page_size": page_size}
        if org_filter and org_filter_applied:
            p["organization__name"] = org_filter
        if extra_params:
            p.update(extra_params)
        return p

    next_url = "%s%s?%s" % (base_url, path, urlencode(build_params()))
    first_request = True

    while next_url:
        data, err = _do_get(module, next_url, headers)

        if err is not None:
            code, body = err

            if first_request and org_filter_applied and _is_org_field_error(code, body):
                # This model has no 'organization' field - drop the filter and retry from
                # the start rather than failing the whole component.
                org_filter_applied = False
                next_url = "%s%s?%s" % (base_url, path, urlencode(build_params()))
                continue

            if code == 404:
                return [], {
                    "ok": False, "skipped": True,
                    "reason": "404 Not Found - endpoint %s does not exist on this AAP instance (component not applicable)" % path,
                }
            elif code == 403:
                return [], {
                    "ok": False, "skipped": False, "http_code": 403,
                    "reason": "403 Forbidden - token/user lacks permission to list %s. Body: %s" % (path, body),
                }
            elif code == 401:
                return [], {
                    "ok": False, "skipped": False, "http_code": 401,
                    "reason": "401 Unauthorized - token invalid/expired for %s. Body: %s" % (path, body),
                }
            else:
                return [], {
                    "ok": False, "skipped": False, "http_code": code,
                    "reason": "HTTP %s error on %s. Body: %s" % (code, path, body),
                }

        first_request = False
        results.extend(data.get("results", []))

        next_page = data.get("next")
        if next_page:
            # 'next' may be relative or absolute depending on AAP version/proxy config -
            # AND, confirmed on a live EDA root, "absolute" doesn't mean "correct":
            # DRF builds these via request.build_absolute_uri(), which can bake in an
            # unreliable internally-observed scheme/host behind a TLS-terminating
            # OCP route (exactly the http://-instead-of-https:// bug that broke initial
            # EDA discovery). Always rebuild against OUR known-reachable base_url rather
            # than trust the response's self-reported host - only the path+query survive.
            next_path = urlparse(next_page).path
            next_query = urlparse(next_page).query
            next_url = "%s%s%s" % (base_url, next_path, ("?%s" % next_query) if next_query else "")
        else:
            next_url = None

    return results, {"ok": True, "org_filter_applied": org_filter_applied}


def _name_from_summary_entry(entry):
    """A summary_fields entry is a dict like {"id":1,"username":"admin"} or
    {"id":4,"name":"Default"}. Users/teams use 'username', everything else uses 'name'."""
    if not isinstance(entry, dict):
        return None
    return entry.get("name") or entry.get("username")


def _lookup_map(id_maps, map_type, api_root):
    """Pick the right id_maps entry for this field given which API root the object
    came from. EDA-rooted objects must check the EDA-scoped map (if one exists for
    this map_type via EDA_FK_MAP_OVERRIDE) rather than the Controller-scoped map of
    the same name - see EDA_FK_MAP_OVERRIDE's docstring for why."""
    if api_root == "eda" and map_type in EDA_FK_MAP_OVERRIDE:
        map_type = EDA_FK_MAP_OVERRIDE[map_type]
    return id_maps.get(map_type) or {}


def _resolve_fk_value(raw_value, summary_entry, id_maps, map_type, api_root):
    """Resolve a single FK value to a name, tolerating TWO different API shapes for
    the same field:

      1. Controller/Gateway shape: `field` holds a bare scalar id (int/str), and the
         name lives separately in `summary_fields.<field>`.
      2. EDA shape (confirmed present on at least some /api/eda/v1/ endpoints, e.g.
         eda-credentials' `organization` / `credential_type`, activations'
         `organization` / `project` / `decision_environment` / `user`): `field`
         itself IS the embedded object - `{"id": 3, "name": "..."}` - with no
         separate summary_fields entry at all, because EDA-server's serializers
         don't follow the AWX summary_fields convention uniformly.

    This is the root-cause fix for the 2026-07-11 re-run of the
    eda_activation/eda_edacredential/eda_eventstream unresolved-FK failure: the
    previous fix (EDA-scoped id_maps + EDA_FK_MAP_OVERRIDE) was necessary but not
    sufficient, because it still assumed `raw_id = cleaned[field]` was always a bare
    scalar. When it's actually an embedded dict, `str(raw_id)` produces something
    like `"{'id': 3, 'name': 'X'}"`, which can never match a dict key in id_maps.
    That's a systematic miss (every object, every occurrence of that field), which
    matches the exactly-divisible unresolved counts observed (10 unresolved / 5
    eda_edacredential objects = 2 per object; 8 / 2 eda_activation objects = 4 per
    object) far better than "some ids happen to be stale/missing".

    Returns (resolved_name_or_None, cleaned_value_to_store). cleaned_value_to_store
    matters for the embedded-dict case: even when we CAN'T resolve a name, we still
    want to store something better than a raw Python dict repr, so we fall back to
    the embedded dict's own "id" (matches what Controller-style output would have
    left behind: a raw id, not a raw dict).
    """
    if isinstance(raw_value, dict):
        # EDA embedded-object shape. Try the object's own name/username first
        # (cheapest, always in sync with whatever this object actually IS).
        name = _name_from_summary_entry(raw_value)
        if name is None and raw_value.get("id") is not None:
            # Object embedded without a usable name field (e.g. just {"id": 3}) -
            # fall back to the id_maps lookup keyed by its embedded id.
            name = _lookup_map(id_maps, map_type, api_root).get(str(raw_value["id"]))
        fallback_value = raw_value.get("id", raw_value)
        return name, fallback_value

    # Controller/Gateway scalar-id shape (previous behavior, unchanged).
    name = _name_from_summary_entry(summary_entry)
    if name is None:
        name = _lookup_map(id_maps, map_type, api_root).get(str(raw_value))
    return name, raw_value


def clean_object(obj, id_maps, api_root="controller"):
    """
    Strips server-generated fields, then resolves known foreign-key fields to names.

    Resolution order:
      1. The object's OWN `summary_fields` (present on every AWX/AAP API response,
         e.g. summary_fields.organization.name, summary_fields.user.username). This
         costs nothing extra - it's already in the payload we fetched - and is
         guaranteed to be resolved against the SAME api_root (controller, gateway, or
         eda) the object came from, so it can't drift out of sync the way a
         separately built id_maps lookup can (this is exactly what filetree_create
         relies on, and exactly what we were throwing away by stripping
         summary_fields outright).
      2. id_maps (built once up front by aap_build_id_maps) as a fallback, for the
         few fields that don't carry a summary_fields entry. Which id_maps entry is
         checked now depends on `api_root` (see _lookup_map / EDA_FK_MAP_OVERRIDE) -
         this is the fix for the 2026-07-11 unresolved-FK failure on
         eda_activation/eda_edacredential/eda_eventstream, where EDA-rooted ids were
         being checked against Controller-only maps and could only match by
         coincidence.

    Also recurses into known EDA embedded-object list fields (eda_credentials,
    event_streams on rulebook_activation) - these hold FULLY EMBEDDED related
    objects, not raw ids, so they were previously invisible to this function
    entirely: any FK inside them stayed raw AND never counted toward
    unresolved_fk_count. Each embedded item now gets this same treatment recursively.

    Returns (cleaned_dict, unresolved_count). A field is only touched if a name was
    actually found by one of the two methods above - otherwise it's left exactly as
    the API returned it, so unresolved data never gets scrambled to a wrong value;
    worst case it stays as an id and unresolved_count goes up.
    """
    summary_fields = obj.get("summary_fields") or {}
    cleaned = {k: v for k, v in obj.items() if k not in STRIP_FIELDS}
    unresolved = 0

    for field, map_type in FK_SCALAR_FIELDS.items():
        # Some EDA-server endpoints expose ONLY a '<field>_id' scalar key (e.g.
        # rulebook_activation's decision_environment_id/organization_id, and the
        # organization_id/credential_type_id inside an embedded eda_credential) with
        # no separate bare '<field>' key at all - unlike Controller/Gateway and unlike
        # EDA's OWN embedded-dict shape handled by _resolve_fk_value. Confirmed against
        # the live 2026-07-12 Test export: these three fields sat in the output as
        # raw ids with no name AND unresolved_fk_count stayed 0, because this loop only
        # ever checked for the bare field name and silently never saw the '_id' form at
        # all - a gap the fail-fast check in export_bulk.yml can't see either, since the
        # module itself never counted it. Checking the bare name first preserves every
        # existing behavior (including the project/project_id case, where 'project'
        # already arrives as an embedded dict resolved via the bare key and 'project_id'
        # is left alone as a harmless raw duplicate); the '_id' form is only consulted
        # when the bare key isn't present at all.
        id_field = field + "_id"
        if field in cleaned and cleaned[field] is not None:
            raw_id = cleaned[field]
            write_field = field
        elif id_field in cleaned and cleaned[id_field] is not None:
            raw_id = cleaned[id_field]
            write_field = field
        else:
            continue

        name, fallback_value = _resolve_fk_value(
            raw_id, summary_fields.get(field), id_maps, map_type, api_root
        )

        if name is not None:
            # Write the resolved name under the bare field name. When we arrived via
            # the '_id' branch this ADDS a new key alongside the existing '<field>_id'
            # (mirroring the project/project_id shape EDA already uses natively)
            # rather than replacing anything - the raw id stays available too.
            cleaned[write_field] = name
        elif write_field in cleaned:
            # Bare-field case, unresolved: preserve prior behavior exactly.
            cleaned[write_field] = fallback_value
            unresolved += 1
        else:
            # '_id'-only case, unresolved: don't fabricate a bare-name field that was
            # never there - just make sure it's counted, which is the whole point of
            # this fix. The raw '<field>_id' value stays untouched in cleaned.
            unresolved += 1

    for field, map_type in FK_LIST_FIELDS.items():
        if field not in cleaned or not isinstance(cleaned[field], list):
            continue

        # summary_fields for list relations comes as either a plain list of
        # {"id":..,"name":..} dicts, or {"results": [...]} depending on the endpoint.
        summary_list = summary_fields.get(field)
        if isinstance(summary_list, dict):
            summary_list = summary_list.get("results")
        summary_by_id = {}
        if isinstance(summary_list, list):
            for entry in summary_list:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    summary_by_id[str(entry["id"])] = _name_from_summary_entry(entry)

        resolved_list = []
        for raw_id in cleaned[field]:
            # List elements can ALSO arrive as embedded dicts (same EDA quirk as the
            # scalar case above) rather than bare ids - reuse the same resolver so
            # both shapes are handled identically instead of duplicating the logic.
            summary_entry = summary_by_id.get(str(raw_id)) if not isinstance(raw_id, dict) else None
            name, fallback_value = _resolve_fk_value(
                raw_id,
                {"name": summary_entry} if summary_entry else None,
                id_maps, map_type, api_root,
            )
            if name is not None:
                resolved_list.append(name)
            else:
                resolved_list.append(fallback_value)
                unresolved += 1
        cleaned[field] = resolved_list

    # --- Recurse into EDA embedded-object list fields (see docstring above) ---
    for field, embedded_root in EDA_EMBEDDED_LIST_FIELDS.items():
        if field not in cleaned or not isinstance(cleaned[field], list):
            continue

        resolved_items = []
        for item in cleaned[field]:
            if not isinstance(item, dict):
                # Older AAP versions / a field that turns out to hold plain ids
                # after all - leave untouched rather than guess.
                resolved_items.append(item)
                continue

            item_cleaned, item_unresolved = clean_object(item, id_maps, embedded_root)
            unresolved += item_unresolved

            # A nested single-embedded-object field inside this item, e.g.
            # event_streams[i].eda_credential - same treatment, one level deeper.
            for nested_field, nested_map_type in EDA_NESTED_SINGLE_OBJECT_FIELDS.items():
                nested_obj = item_cleaned.get(nested_field)
                if isinstance(nested_obj, dict):
                    nested_cleaned, nested_unresolved = clean_object(
                        nested_obj, id_maps, embedded_root
                    )
                    item_cleaned[nested_field] = nested_cleaned
                    unresolved += nested_unresolved

            resolved_items.append(item_cleaned)
        cleaned[field] = resolved_items

    return cleaned, unresolved


def _object_name_for_vars(obj):
    """Best-effort human identifier for an object, used only to build vaulted var names
    readable enough to hand-edit. Falls back to 'unnamed' rather than failing - a var
    name collision here is a cosmetic annoyance (the playbook lists all created names),
    not a data-loss risk."""
    return obj.get("name") or obj.get("username") or "unnamed"


def main():
    module_args = dict(
        controller_host=dict(type="str", required=True),
        gateway_host=dict(type="str", required=False),
        eda_host=dict(type="str", required=False),
        hub_host=dict(type="str", required=False),
        api_root=dict(type="str", default="controller", choices=["controller", "gateway", "eda", "hub"]),
        controller_username=dict(type="str", required=False),
        controller_password=dict(type="str", required=False, no_log=True),
        controller_oauthtoken=dict(type="str", required=False, no_log=True),
        validate_certs=dict(type="bool", default=True),
        object_type=dict(type="str", required=False, choices=list(API_PATHS.keys())),
        component_name=dict(type="str", required=False),
        explicit_path=dict(type="str", required=False),
        object_shape=dict(type="str", choices=["list", "dict"], default="list"),
        output_path=dict(type="str", required=True),
        output_var_name=dict(type="str", required=False),
        page_size=dict(type="int", default=200),
        retry_max_attempts=dict(type="int", default=3),
        retry_backoff_seconds=dict(type="float", default=2),
        organization_filter=dict(type="str", required=False),
        extra_query_params=dict(type="dict", required=False, default={}),
        skip_on_404=dict(type="bool", default=True),
        id_maps=dict(type="dict", required=False, default={}),
        secrets_as_variables=dict(type="bool", default=True),
        secrets_as_variables_prefix=dict(type="str", default="vaulted"),
        yaml_mark_unsafe_templates=dict(type="bool", default=True),
        skip_managed_objects=dict(type="bool", default=True),
        skip_system_usernames=dict(type="list", elements="str", default=[]),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True,
        required_one_of=[["object_type", "explicit_path"]],
        required_by={"explicit_path": ["component_name"]},
    )

    controller_base_url = module.params["controller_host"].rstrip("/")
    gateway_base_url = (module.params.get("gateway_host") or module.params["controller_host"]).rstrip("/")
    eda_base_url = (
        module.params.get("eda_host")
        or module.params.get("gateway_host")
        or module.params["controller_host"]
    ).rstrip("/")
    hub_base_url = (
        module.params.get("hub_host")
        or module.params.get("gateway_host")
        or module.params["controller_host"]
    ).rstrip("/")
    api_root = module.params.get("api_root") or "controller"
    base_url = {
        "controller": controller_base_url,
        "gateway": gateway_base_url,
        "eda": eda_base_url,
        "hub": hub_base_url,
    }[api_root]

    if module.params.get("explicit_path"):
        # Dynamic-discovery mode: path came from aap_discover_components, not our static table.
        object_type = module.params["component_name"]
        path = module.params["explicit_path"]
        # Defensive, not just belt-and-suspenders: aap_discover_components already
        # normalizes absolute-URL paths (confirmed live behavior of the EDA root - see
        # its _normalize_path) before handing them to this module, but if explicit_path
        # is ever supplied directly (manual runs, a future discovery source that isn't
        # normalized) an absolute URL here would silently corrupt into
        # "<base_url><absolute-url>" when concatenated below. Strip to just the path if
        # so, rather than trust a self-reported host/scheme that may not be reachable.
        if path.startswith("http://") or path.startswith("https://"):
            path = urlparse(path).path
    else:
        object_type = module.params["object_type"]
        path = API_PATHS[object_type]

    headers = build_auth_headers(module)
    object_shape = module.params["object_shape"]
    # Defaults to object_type (old behavior: raw discovered/component name) unless the
    # caller passed output_var_name - e.g. the dispatch-role variable name looked up
    # from vars/dispatch_component_map.yml, so this write lands directly under the
    # name dispatch's `include_vars: dir:` step expects, with no rename pass after.
    write_key = module.params.get("output_var_name") or object_type
    secrets_on = module.params["secrets_as_variables"]
    secrets_prefix = module.params["secrets_as_variables_prefix"]
    unsafe_templates_on = module.params["yaml_mark_unsafe_templates"]

    # ---- object_shape == "dict": single-object endpoints (e.g. /settings/all/) ----
    if object_shape == "dict":
        raw_obj, status = fetch_single_object(module, base_url, path, headers)

        if not status["ok"]:
            if status.get("skipped") and module.params["skip_on_404"]:
                module.exit_json(
                    changed=False, count=0, output_path=module.params["output_path"],
                    skipped=True, reason=status["reason"], secret_vars=[], secrets_replaced_count=0,
                    unsafe_templates_tagged=0,
                )
            else:
                module.fail_json(
                    msg=status["reason"], http_code=status.get("http_code"),
                    object_type=object_type,
                )

        cleaned = {k: v for k, v in (raw_obj or {}).items() if k not in STRIP_FIELDS}
        secret_vars = []
        if secrets_on:
            # No per-object name for a settings dict - there's only one of it per
            # component, so the var name is just prefix_objecttype_field(s).
            cleaned, secret_vars = vaultize_secrets(cleaned, [secrets_prefix, object_type])
        else:
            # Empty out $encrypted$ markers rather than leave them literal -
            # see blank_encrypted_markers docstring for why.
            cleaned = blank_encrypted_markers(cleaned)
        cleaned, unsafe_tagged = mark_unsafe_templates(cleaned, enabled=unsafe_templates_on)

        if module.check_mode:
            module.exit_json(
                changed=True, count=1, output_path=module.params["output_path"],
                unresolved_fk_count=0, secret_vars=secret_vars,
                secrets_replaced_count=len(secret_vars),
                unsafe_templates_tagged=unsafe_tagged, simulated=True,
            )

        try:
            with open(module.params["output_path"], "w") as f:
                yaml.dump(
                    {write_key: cleaned}, f, Dumper=_CaCDumper,
                    default_flow_style=False, sort_keys=False,
                )
        except IOError as e:
            module.fail_json(msg="Failed to write output file: %s" % str(e))

        module.exit_json(
            changed=True, count=1, output_path=module.params["output_path"],
            unresolved_fk_count=0, secret_vars=secret_vars,
            secrets_replaced_count=len(secret_vars),
            unsafe_templates_tagged=unsafe_tagged,
        )

    # ---- object_shape == "list": normal paginated-collection export ----
    raw_results, status = fetch_all_pages(
        module,
        base_url,
        path,
        headers,
        module.params["page_size"],
        module.params.get("organization_filter"),
        module.params.get("extra_query_params"),
    )

    if not status["ok"]:
        if status.get("skipped") and module.params["skip_on_404"]:
            # Component genuinely doesn't exist on this AAP version/instance.
            # Not a failure - exit cleanly with count=0 so the playbook summary shows
            # "skipped" rather than crashing the whole export run.
            module.exit_json(
                changed=False, count=0, output_path=module.params["output_path"],
                skipped=True, reason=status["reason"], secret_vars=[], secrets_replaced_count=0,
                unsafe_templates_tagged=0,
            )
        else:
            # Real error (401/403/500/network) - surface it loudly. The playbook's
            # ignore_errors + summary task will show this clearly per-component
            # without killing the other 17 exports.
            module.fail_json(
                msg=status["reason"], http_code=status.get("http_code"),
                object_type=object_type,
            )

    # --- Drop objects the target AAP will refuse to re-apply ---
    # 'managed: true' == Red-Hat-shipped built-in (Platform Auditor role, Machine
    # credential type, Default org, rh-certified remote, Local Database
    # Authenticator, ...). Every one of these exists identically on the target
    # AAP already; every write endpoint rejects re-applying them. References to
    # them BY NAME (organization: "Default", credential_type: "Machine") still
    # resolve on the target because the name is still there, so nothing
    # downstream breaks - only the redundant create/update attempts do.
    #
    # skip_system_usernames catches things the AAP operator on OpenShift creates
    # automatically (_token_service_user, aap_operator_service_account) which
    # don't carry managed=true but are equally not ours to manage.
    skip_managed = module.params["skip_managed_objects"]
    skip_users = set(module.params.get("skip_system_usernames") or [])
    skipped_managed_count = 0
    skipped_username_count = 0
    filtered_results = []
    for o in raw_results:
        if skip_managed and o.get("managed") is True:
            skipped_managed_count += 1
            continue
        if skip_users and o.get("username") in skip_users:
            skipped_username_count += 1
            continue
        filtered_results.append(o)
    raw_results = filtered_results

    id_maps = module.params.get("id_maps") or {}
    cleaned_results = []
    unresolved_total = 0
    all_secret_vars = []
    unsafe_tagged_total = 0
    for o in raw_results:
        cleaned, unresolved = clean_object(o, id_maps, api_root)
        if secrets_on:
            obj_name = _object_name_for_vars(cleaned)
            cleaned, secret_vars = vaultize_secrets(cleaned, [secrets_prefix, object_type, obj_name])
            all_secret_vars.extend(secret_vars)
        else:
            # Empty out $encrypted$ markers rather than leave them literal -
            # see blank_encrypted_markers docstring for why.
            cleaned = blank_encrypted_markers(cleaned)
        cleaned, unsafe_tagged = mark_unsafe_templates(cleaned, enabled=unsafe_templates_on)
        unsafe_tagged_total += unsafe_tagged
        cleaned_results.append(cleaned)
        unresolved_total += unresolved

    if module.check_mode:
        module.exit_json(
            changed=True, count=len(cleaned_results), output_path=module.params["output_path"],
            unresolved_fk_count=unresolved_total, secret_vars=all_secret_vars,
            secrets_replaced_count=len(all_secret_vars),
            unsafe_templates_tagged=unsafe_tagged_total, simulated=True,
        )

    # ONE write, not one-per-object. This is the key difference from filetree_create.
    try:
        with open(module.params["output_path"], "w") as f:
            yaml.dump(
                {write_key: cleaned_results},
                f,
                Dumper=_CaCDumper,
                default_flow_style=False,
                sort_keys=False,
            )
    except IOError as e:
        module.fail_json(msg="Failed to write output file: %s" % str(e))

    # count=0 here is a VALID result (e.g. no teams defined in this org) - not an error.
    # org_filter_applied=False means this model has no organization field - the export
    # contains ALL organizations' data regardless of what organization_filter was requested.
    module.exit_json(
        changed=True, count=len(cleaned_results), output_path=module.params["output_path"],
        org_filter_applied=status.get("org_filter_applied", False),
        unresolved_fk_count=unresolved_total,
        secret_vars=all_secret_vars,
        secrets_replaced_count=len(all_secret_vars),
        unsafe_templates_tagged=unsafe_tagged_total,
        skipped_managed_count=skipped_managed_count,
        skipped_username_count=skipped_username_count,
    )


if __name__ == "__main__":
    main()