# kushalsankhe.cac_export — AAP bulk CaC export collection

A replacement for `infra.aap_configuration_extended.filetree_create` for large AAP
instances, built for one reason: `filetree_create` forks one process per *object*
(one credential, one job template, one user...) and on OpenShift that blows through
the namespace's `pids_limit` on anything but a small instance. This collection forks
once per *component type* instead (organizations, job_templates, credentials, ...),
and each of those does its own pagination and a single file write inside one Python
process. Same coverage goal, fundamentally different — and safer — process footprint.

It does **not** try to be a drop-in replacement for everything `filetree_create` does.
See [Known limitations](#known-limitations) for what's intentionally out of scope.

## What's in here

| Path | Purpose |
|---|---|
| `plugins/modules/aap_discover_components.py` | Walks the Controller and (optionally) Gateway/EDA/Hub API roots and returns every CaC-relevant endpoint found, minus a hardcoded runtime/meta blocklist. No hand-maintained object list — adapts automatically if AAP adds/renames an endpoint. |
| `plugins/modules/aap_build_id_maps.py` | Runs once, before export. Builds `{type: {id: name}}` lookup tables for organizations, credential_types, execution_environments, inventories, projects, credentials, labels, instance_groups, users, teams (plus EDA-scoped equivalents). |
| `plugins/modules/aap_export_bulk.py` | Given one component (name + API path), paginates through every object, strips server-generated fields, resolves known foreign-key fields from id to name using the maps above, and writes one YAML file. |
| `roles/cac_export/` | Wires the three modules together: discover → build id maps → export each discovered component → summarize → fail on real errors or unresolved FK references. This is the reusable unit — `include_role`/`import_role` it directly from your own playbooks. |
| `roles/apply_dispatch/` | Loads `dispatch_ready/` and hands it to `infra.aap_configuration.dispatch`. |
| `playbooks/export_bulk.yml` / `playbooks/apply_dispatch.yml` | Thin, ready-to-run wrappers around the two roles above, for when you just want to run this collection standalone rather than compose it into your own playbook. |

## Requirements

- `ansible-core` >= 2.15.
- A Controller and/or Gateway OAuth2 token, or username/password, with read access to
  whatever you're exporting.
- Install like any other collection: `ansible-galaxy collection install kushalsankhe.cac_export`
  (or point `collections_paths`/`requirements.yml` at this repo). No extra collections
  are required by the modules themselves (plain Python + `ansible.module_utils.urls`,
  not wrappers around `ansible.controller`/`awx.awx`); `infra.aap_configuration` is only
  needed for `roles/apply_dispatch` (the reimport side, not export).

## Role variables

All variables below apply to `roles/cac_export` and can be passed as role vars,
`-e` extra-vars (when using the wrapper playbook), or normal Ansible variable
precedence. Only `controller_host` plus one auth method, and `target_env` (or
`output_dir`), are required; everything else has a default.

| Variable | Default | Required | Description |
|---|---|---|---|
| `controller_host` | — | **yes** | Base URL of the AAP instance, e.g. `https://aap.example.com`. Used for both the Controller and Gateway API roots unless `gateway_host` is set. |
| `controller_oauthtoken` | — | one of token / username+password | Bearer token for auth. |
| `controller_username` | — | one of token / username+password | Basic-auth username, alternative to token. |
| `controller_password` | — | one of token / username+password | Basic-auth password. |
| `gateway_host` | same as `controller_host` | no | Override if the Gateway API is served from a different host/port than the Controller. Used both for discovery AND for the actual per-component export fetch (fixed - previously only affected discovery, and export always used `controller_host` regardless of which root a component came from). |
| `target_env` | — | yes, unless `output_dir` set | Used to build the default output path: `/cac/<target_env>`. |
| `output_dir` | `/cac/<target_env>` | no | Explicit output directory, overrides the `target_env`-derived path. |
| `validate_certs` | `true` | no | Whether to validate TLS certs on the AAP endpoint. Set `false` for self-signed/internal CAs. |
| `services` | `[controller, gateway, eda]` | no | Service-level scoping (milestone 10, 2026-07-13): one list instead of three separate booleans. Pass a subset like `services: [controller, hub]` to control which API roots get discovered/exported in one place. `controller` is accepted in the list but is a no-op — Controller can't be excluded. If any of `include_gateway`/`include_eda`/`include_hub` below is passed explicitly, it wins over `services` for that one root (so existing invocations that already set those booleans are unaffected); otherwise that root's inclusion is derived from whether it's in `services`. |
| `include_gateway` | `true` (or derived from `services` — see above) | no | Whether to also discover and export Gateway-owned objects (auth, RBAC, gateway settings). Set `false` for controller-only exports. |
| `eda_host` | same as `gateway_host` (falling back to `controller_host`) | no | Override if the EDA (Event-Driven Ansible) Controller API is served from a different host/port than the Gateway. Used both for discovery AND for the actual per-component export fetch, same fix as `gateway_host` above. |
| `include_eda` | `true` (or derived from `services` — see above) | no | Whether to also discover and export EDA-owned objects (credential types, decision environments, event streams, rulebook activations, EDA role assignments). Set `false` to skip EDA discovery entirely (e.g. pre-2.5 AAP, or a token without EDA access - though those cases are handled gracefully anyway). |
| `include_inventory_content` | `false` | no | Whether to discover and export static inventory content (individual hosts and groups within each inventory) as CaC components. Off by default - opt-IN, since many shops source hosts/groups dynamically and exporting that as static CaC would snapshot someone else's system of record rather than declare actual desired state. Set `true` if you hand-author inventory content and want it captured. |
| `hub_host` | same as `gateway_host` (falling back to `controller_host`) | no | Override if the Hub/Automation Hub (Galaxy) API is served from a different host/port than the Gateway. Used both for discovery AND for the actual per-component export fetch, same pattern as `eda_host`. |
| `include_hub` | `false` (or derived from `services` — see above) | no | Whether to also discover and export Hub-owned CONFIG objects: namespaces, remotes, and contentguards/access_policies (v1, deliberately config-only - see CLAUDE.md milestone 9). Defaults `false` (i.e. not in `services`' default list), unlike `include_gateway`/`include_eda` - new, not yet validated against a live instance. |
| `project_id` / `job_template_id` / `inventory_id` / `workflow_job_template_id` / `schedule_id` | none | no | Restrict the matching component to a single object by id. Each is a no-op unless set. Only meaningful to set for the component it names - e.g. `job_template_id` only affects the `job_templates` export. |
| `label_filter` | none | no | Restrict the `labels` component to a single label by name (labels are normally referenced by name, not id). |
| `organization_filter` | none (all orgs) | no | Restrict objects to one organization by name. Now applied to Controller-, Gateway-, and EDA-rooted objects alike (previously Controller-only — see CLAUDE.md milestone 4). Object types with no organization field at all (Organization, User, CredentialType, Schedule, WorkflowJobTemplateNode, RBAC objects, and any gateway/EDA object without an org field) are exported in full regardless, since there's no other way to scope them — this is automatic, not a bug. Note: this confirms filtering doesn't *error* on Gateway/EDA objects, not that Gateway/EDA's org-scoping semantics exactly match Controller's for types that do have an org field — still worth confirming against a live instance. |
| `page_size` | `200` | no | API page size used when paginating each component. |
| `retry_max_attempts` | `3` | no | Total attempts (including the first) per GET before failing it. Retries only transient failures — network errors (DNS/connection/timeout/TLS) and HTTP 429/500/502/503/504 — never 401/403/404/other 4xx. Set to `1` to disable retrying. |
| `retry_backoff_seconds` | `2` | no | Base delay before the first retry, doubling each subsequent attempt. Set to `0` for immediate retries. |
| `id_map_types` | auto: full set, minus `eda_*` if `include_eda: false` (see CLAUDE.md, 2026-07-13) | no | Trim which id→name lookup maps get built, if you know a given export doesn't need all of them. Pass as a YAML/JSON list to override the automatic set entirely - explicit always wins, no trimming applied on top of it. If left unset, `eda_`-prefixed map types (`eda_organizations`/`eda_credential_types`/`eda_decision_environments`/`eda_credentials`/`eda_projects`) are now skipped automatically whenever `include_eda: false`, so an EDA-less environment no longer logs 5 guaranteed `HTTP 503`s every run for maps nothing in that run needs. |
| `fail_on_unresolved_fk` | `true` | no | If any exported object has a foreign-key field that couldn't be resolved to a name, fail the play at the end rather than silently shipping an export with raw ids in it. Set `false` to export anyway and just get the warning in the summary. |
| `secrets_as_variables` | `true` | no | Rewrite every field AAP returns as the literal `$encrypted$` marker (credential inputs, tokens, encrypted settings, ...) to a `{{ vaulted_<type>_<name>_<field_path> }}` Jinja reference instead of exporting the marker string as-is. Set `false` to export the literal `$encrypted$` string (old behavior). |
| `secrets_as_variables_prefix` | `vaulted` | no | Prefix used when building the Jinja variable names described above. |
| `yaml_mark_unsafe_templates` | `true` | no | Tag Jinja-looking values inside `extra_vars`/`extra_data`/`source_vars`/`variables`/`survey_spec` with YAML's `!unsafe` tag so Ansible's own loader doesn't re-template them on reimport. Set `false` to write plain strings instead (old behavior) - only safe if you know reimport never templates these fields. Output files require Ansible's YAML loader to read when this is on; plain `yaml.safe_load` will error on the tag. |

## Usage examples

**Token auth, export everything, target env `dev`:**
```bash
ansible-playbook kushalsankhe.cac_export.export_bulk \
  -e "controller_host=https://aap.example.com" \
  -e "controller_oauthtoken=$TOKEN" \
  -e "target_env=dev"
```

**Username/password auth, controller-only (skip gateway), one org:**
```bash
ansible-playbook kushalsankhe.cac_export.export_bulk \
  -e "controller_host=https://aap.example.com" \
  -e "controller_username=svc_export" \
  -e "controller_password=$PASSWORD" \
  -e "include_gateway=false" \
  -e "organization_filter=Test-DEV" \
  -e "target_env=Test-dev"
```

**Export anyway even with unresolved FK references (e.g. quick backup, not for reimport):**
```bash
ansible-playbook kushalsankhe.cac_export.export_bulk \
  -e "controller_host=https://aap.example.com" \
  -e "controller_oauthtoken=$TOKEN" \
  -e "target_env=dev" \
  -e "fail_on_unresolved_fk=false"
```

## Output

**(2026-07-13, restructured — see CLAUDE.md changelog):** export now writes directly to
its final destination in one pass. There is no longer a flat, endpoint-named export
followed by a separate rename/copy into a second directory.

`vars/dispatch_component_map.yml` is consulted **before** each component is fetched, and
decides both whether it's fetched at all and where its one write lands:

- **`used_in_dispatch: true`** → fetched, written straight to
  `<output_dir>/dispatch_ready/<dispatch_var>.yml`, already keyed under the dispatch
  variable name (e.g. `job_templates` → `dispatch_ready/controller_templates.yml`,
  top-level key `controller_templates:`). This mirrors `filetree_create`'s own pattern —
  its per-component Jinja templates open with the dispatch variable name directly, no
  rename step either. Dispatching is unchanged: `include_vars: dir: dispatch_ready/` +
  `include_role: dispatch` — see `playbooks/apply_dispatch.yml`.
- **`used_in_dispatch: false`** → **not fetched at all** by default (no API call made
  for it). Set `export_excluded_components: true` to fetch these anyway, written to
  `<output_dir>/excluded_or_informational/<name>.yml` — kept out of `dispatch_ready/` on
  purpose, since dispatch has no role/var for them (see the `reason:` on each entry in
  `vars/dispatch_component_map.yml`).
- **Not in the map at all** (a new/renamed AAP endpoint `aap_discover_components.py`
  found that the map hasn't caught up to yet) → still fetched, written to
  `<output_dir>/unmapped/<name>.yml`, and called out in the "component split" debug
  output so it doesn't silently go missing while the map is updated.

Two exceptions to the "list of objects" shape: `controller_settings` and
`gateway_settings` are each a single flat dict (`{controller_settings: {KEY: value, ...}}`),
since the underlying `/settings/all/` endpoint isn't a paginated collection.

If `secrets_as_variables` is on (default), a `vaulted_vars_template.yml` is also written
to `<output_dir>` — see [Known limitations](#known-limitations) for what it contains and
what to do with it.

## Reimport via `infra.aap_configuration.dispatch`

`<output_dir>/dispatch_ready/` is produced directly by `export_bulk.yml` (see Output
above) — there's nothing further to build or transform. Dispatching just does
`include_vars: dir: dispatch_ready/` + `include_role: dispatch`, the same pattern
`filetree_read` + `dispatch` already use — see `playbooks/apply_dispatch.yml`.
`vars/dispatch_component_map.yml` remains the single source of truth for which
component maps to which dispatch variable.

**Included in `dispatch_ready/` (34 of 63 exported component names), source → dispatch variable:**

| Component | Dispatch variable | Component | Dispatch variable |
|---|---|---|---|
| `controller_settings` | `controller_settings` | `job_templates` | `controller_templates` |
| `credential_input_sources` | `controller_credential_input_sources` | `labels` | `controller_labels` |
| `credential_types` | `controller_credential_types` | `notification_templates` | `controller_notifications` |
| `credentials` | `controller_credentials` | `projects` | `controller_projects` |
| `execution_environments` | `controller_execution_environments` | `schedules` | `controller_schedules` |
| `groups` | `controller_groups` | `workflow_job_templates` | `controller_workflows` |
| `hosts` | `controller_hosts` | `instance_groups` | `controller_instance_groups` |
| `inventory` | `controller_inventories` | `inventory_sources` | `controller_inventory_sources` |
| `gateway_applications` | `aap_applications` | `gateway_organizations` | `aap_organizations` |
| `gateway_teams` | `aap_teams` | `gateway_users` | `aap_user_accounts` |
| `gateway_authenticator_maps` | `gateway_authenticator_maps` | `gateway_authenticators` | `gateway_authenticators` |
| `gateway_role_definitions` | `gateway_role_definitions` | `gateway_role_team_assignments` | `gateway_role_team_assignments` |
| `gateway_role_user_assignments` | `gateway_role_user_assignments` | `gateway_settings` | `gateway_settings` |
| `eda_project` | `eda_projects` | `eda_activation` | `eda_rulebook_activations` |
| `eda_credentialtype` | `eda_credential_types` | `eda_edacredential` | `eda_credentials` |
| `eda_decisionenvironment` | `eda_decision_environments` | `eda_eventstream` | `eda_event_streams` |
| `hub_namespaces_ansible` | `hub_namespaces` | `hub_remotes_ansible_collection` | `hub_collection_remotes` |

**Deliberately left out of `dispatch_ready/` (29 of 63)** — every name below has a
fuller explanation in `vars/dispatch_component_map.yml`; short version:

- *Duplicates of a component already listed above* (not merged in twice —
  Gateway is the canonical source): `organizations`, `users`, `teams`,
  `eda_organization`, `eda_team`.
- *`dispatch` role exists in `infra.aap_configuration` but isn't in its default
  dispatcher list* (would need an `aap_configuration_dispatcher_roles` override
  to use): `role_definitions`, `role_user_assignments`, `role_team_assignments`,
  `eda_user`, `eda_credentialinputsource`.
- *No `dispatch` role exists for this at all yet* — confirmed gaps, not
  oversights: all `hub_access_policies` / `hub_contentguards_*` (deliberately
  deferred, per direction to look at Hub later), `hub_remotes_ansible_git`,
  `hub_remotes_ansible_role`, `hub_remotes_file`, `hub_namespaces_container`,
  `hub_remotes_container`, `hub_remotes_container_pull_through`,
  `gateway_ca_certificates`, `gateway_authenticator_plugins`,
  `gateway_authenticator_users`.
- *Not real config, or needs a reshape dispatch can't do with a rename alone*:
  `settings` (category listing, not actual values — see `controller_settings`/
  `gateway_settings` instead), `constructed_inventory`,
  `workflow_job_template_nodes` (needs nesting under each workflow's own
  `workflow_nodes` key, not a flat top-level list).

## Applying an export to a target instance

```
ansible-playbook kushalsankhe.cac_export.export_bulk -e "controller_host=https://aap-source.example.com" -e "target_env=sbx" ...
ansible-playbook kushalsankhe.cac_export.apply_dispatch -e "output_dir=/cac/sbx" -e "aap_hostname=https://aap-target.example.com" -e "aap_username=admin" -e "aap_password=..."
```

`apply_dispatch.yml` does nothing but `include_vars: dir: "{{ output_dir }}/dispatch_ready"`
followed by `include_role: infra.aap_configuration.dispatch` — no transform logic lives
there at all; everything in `dispatch_ready/` is already shaped correctly by the time
that playbook runs. Requires `infra.aap_configuration` installed separately
(`ansible-galaxy collection install infra.aap_configuration`).

## Known limitations

- **FK resolution now uses `summary_fields` as the primary source, `id_maps` as
  fallback, and the fallback is now API-root-aware.** Every AWX/AAP API object
  response embeds a `summary_fields` block with already-resolved related-object names
  (e.g. `summary_fields.user.username`, `summary_fields.role_definition.name`) — this
  is standard across Controller, Gateway, AND EDA roots, at zero extra API cost, and
  can't drift out of sync the way a separately-built id map can. This is what fixed
  the `gateway_authenticator_users` / `gateway_role_user_assignments` failures seen in
  testing (a gateway-referenced user id wasn't present in the Controller-built `users`
  map). `aap_build_id_maps` is kept as a fallback for the handful of fields that don't
  carry a `summary_fields` entry — **but EDA has its own id space** for organizations,
  credential types, and decision environments (confirmed live: `eda_credentialtype`
  discovery returned 28 objects vs Controller's `credential_types` returning 33 —
  different objects, different ids). A Controller-scoped fallback map can only match
  an EDA object's id by coincidence. Fixed (2026-07-11, after the Test dev
  unresolved-FK failure on `eda_activation`/`eda_edacredential`/`eda_eventstream`):
  `aap_build_id_maps` now also builds `eda_organizations`, `eda_credential_types`,
  `eda_decision_environments`, and `eda_credentials` maps against `eda_host`, and
  `clean_object()` in `aap_export_bulk.py` picks the correct map based on the
  object's `api_root`. This mirrors the exact fix
  `infra.aap_configuration_extended.filetree_create` shipped for the same bug class
  in its own CHANGELOG ("Fix organization query in EDA decision environments
  template, organizations endpoint was incorrect"; "Change decision_environment_id to
  organization_id in eda_rulebook_activations.yml") — query the API root the object
  actually came from, not always Controller. Separately, `activation.eda_credentials`
  and `activation.event_streams` are lists of **fully embedded objects**, not raw
  ids — `clean_object()` now recurses into them (and into
  `event_streams[i].eda_credential`) so FK fields nested inside them get resolved
  and counted too, instead of silently passing through unresolved and uncounted.
  **Confirmed live, 2026-07-12**: the Test dev run with `eda_activation` (2
  objects) and `eda_edacredential` (5 objects) returned `unresolved_fk_count: 0` for
  every component, including the ones that previously failed on this exact bug -
  `secrets_replaced_count` on `eda_activation` even shows the nested-embedded-object
  path resolved correctly (e.g.
  `vaulted_eda_activation_service_down_remediation_eda_credentials_0_inputs_oauth_token`
  proves the recursion into `eda_credentials[i].inputs` worked, not just top-level
  fields). **One field missed by that fix, found and fixed 2026-07-13:**
  `activation.project` still fell back to the Controller-scoped `projects` map (EDA
  has its own project id space too, same as the three fields above) — `eda_projects`
  is now built the same way. **Not yet confirmed against a live `eda_activation`
  object with a real `project` value** — no run so far has exercised this path with
  `include_eda: true` since the fix landed.
- **Secrets abstraction (implemented and confirmed live, 2026-07-12).** Any field
  whose value AAP returns as `$encrypted$` — credential `inputs`, notification
  template tokens, gateway authenticator secrets, encrypted settings, etc., anywhere
  in the object — is rewritten to a `{{ vaulted_<type>_<name>_<field_path> }}` Jinja
  reference. Every variable name created is collected into
  `<output_dir>/vaulted_vars_template.yml` (values `"CHANGEME"`) for you to fill in
  and `ansible-vault encrypt`. Set `secrets_as_variables: false` to disable and get
  literal `$encrypted$` strings in the output instead (old behavior). Confirmed
  against a real AAP export: 25 vaulted vars generated across users, credentials,
  gateway applications/users, EDA activations/credentials/event-streams, and
  controller/gateway settings, with `vaulted_vars_template.yml` correctly listing all
  25 for fill-in.
- **Controller/Gateway settings export (implemented and confirmed live, 2026-07-12).**
  `controller_settings.yml` and `gateway_settings.yml` are exported automatically
  (from `/api/controller/v2/settings/all/` and `/api/gateway/v1/settings/all/`
  respectively) as a single flat-dict file each, not a list. Both paths confirmed to
  exist and return real data on the Test dev instance - the Gateway path was a
  best-effort guess and is no longer unconfirmed. Set `include_settings: false` on
  `aap_discover_components` (not yet exposed as a playbook var) to turn both off.
- **YAML safety for survey/extra_vars fields (implemented, emission confirmed live).**
  Numeric-looking strings (`123-123-123`, `0123`, `yes`, ...) were checked directly
  against this module's actual dump/load round-trip and are NOT corrupted - PyYAML's
  `SafeDumper` already quotes anything that would otherwise resolve to a non-str type.
  The real risk was Jinja-looking content (e.g. `{{ inventory_hostname }}`) inside
  `extra_vars`/`extra_data`/`source_vars`/`variables`/`survey_spec` getting
  re-evaluated by Ansible's own Jinja templar at reimport time. Any such value is now
  written with an explicit `!unsafe` YAML tag (same mechanism Ansible core uses for
  `no_log` data), so Ansible's own loader skips re-templating it. Toggle:
  `yaml_mark_unsafe_templates` (default `true`). **This makes the output files require
  Ansible's own YAML loader to read correctly** - plain `yaml.safe_load` will raise on
  the `!unsafe` tag; this is an intentional, documented tradeoff, not a bug. Confirmed
  live, 2026-07-12: `hosts.yml`'s one host had a genuinely Jinja-looking
  `ansible_python_interpreter` value, correctly detected and emitted as
  `variables: !unsafe '{"ansible_connection": "local", "ansible_python_interpreter":
  "''{{ ansible_playbook_python }}''"}'` in the actual output file. Still open: this
  confirms the tag is emitted correctly, not that `infra.aap_configuration`'s reimport
  roles honor `!unsafe` the way core `ansible-playbook` does - that depends on how that
  role loads var files and hasn't been tested through an actual reimport yet.
- **Applications (OAuth2 apps) export - already working via dynamic discovery.**
  Confirmed against a live run: `aap_discover_components.py`'s Gateway-root walk was
  never given a blocklist entry for `applications`, so it's exported automatically as
  `gateway_applications.yml`, secrets included. (The static `object_type=` table in
  `aap_export_bulk.py` still excludes plain `applications` under the Controller root,
  which is correct - it only ever existed there in older AAP versions.)
- **EDA discovery (implemented and live-validated against a real error) - Hub
  discovery still not started.** `/api/eda/v1/` (AAP 2.5+, served through the Gateway)
  is probed the same generic way as Controller/Gateway, discovered names prefixed
  `eda_` (`eda_activation.yml`, `eda_decisionenvironment.yml`, `eda_edacredential.yml`,
  ...). A first live run against Test's dev instance surfaced two real bugs, both
  fixed:
  1. EDA's root index uses a completely different key-naming convention than
     Controller/Gateway (DRF router-style `<basename>-list`, e.g.
     `decisionenvironment-list`, plus non-collection action endpoints like
     `session-login`/`config`/`openapi-json`) - not the plain-noun style
     (`job_templates`) the first-pass blocklist assumed. Fixed: discovery now keeps
     only `*-list` entries and un-suffixes them, so `EDA_BLOCKLIST_BASENAMES` lines up
     with real object names (also corrected: it's `activation`, not
     `rulebook_activation`; `edacredential`, not `credential`).
  2. A real, more serious latent bug the above exposed: `aap_export_bulk.py` only ever
     built request URLs from `controller_host`, ignoring which API root a component
     came from. This "worked" for Gateway by accident, but broke loudly once EDA
     returned genuinely absolute URLs with an unreliable internal scheme/host baked in
     - producing a garbled hostname and a DNS failure. Fixed with new `gateway_host`/
     `eda_host`/`api_root` params on `aap_export_bulk.py` so it picks the right
     base_url per component, plus normalization of any absolute-URL path (at discovery
     time, and defensively again in `aap_export_bulk.py` and its pagination handling).
  Both fixes are unit-tested against the exact failing data from that run, but **not
  yet re-confirmed with a fresh live run**. Org-filtering is now applied to
  `eda_*`/`gateway_*` objects too (see CLAUDE.md milestone 4, 2026-07-11) — the module's
  automatic "drop the filter and retry" fallback (`_is_org_field_error`) makes this safe
  for object types with no org field, but the actual filtering *semantics* on Gateway/EDA
  haven't been confirmed correct against a live instance yet.
- **Hub/Automation Hub/Galaxy discovery (v1 implemented 2026-07-12, NOT yet live-validated).**
  `/api/galaxy/pulp/api/v3/` (AAP 2.5+) is probed like the other roots, but filtered
  through an explicit `HUB_ALLOWLIST` instead of a blocklist (config-relevant content is
  the small fraction of this root, not the large one). v1 scope: `namespaces` (ansible +
  container), `remotes` (ansible collection/git/role, container/pull-through/container,
  file), and `contentguards`/`access_policies`, discovered names prefixed `hub_`
  (`hub_namespaces_ansible.yml`, `hub_remotes_ansible_collection.yml`, ...). Confirmed
  this root has the same self-reported-absolute-`http://`-URL problem EDA had; reused the
  existing `_normalize_path`/per-root `base_url` fix rather than writing new logic.
  Deliberately out of v1: actual collection/container content, repositories,
  distributions, publications, and Hub's own `users`/`groups`/`roles` (ambiguous overlap
  with Gateway RBAC, not resolved yet). `include_hub` defaults `false` - opt-in until
  run against a live instance.
- **Retry/backoff on transient failures (fixed, 2026-07-11).** `aap_export_bulk.py`'s
  `_do_get` now retries transient failures — network-level errors (DNS/connection/
  timeout/TLS) and HTTP 429/500/502/503/504 — with exponential backoff, configurable
  via `retry_max_attempts` (default 3 total attempts) and `retry_backoff_seconds`
  (default 2s base, doubling). 401/403/404/other 4xx are never retried. Static-checked
  and unit-tested against synthetic failures only — not yet exercised against a real
  transient failure on a live instance.
- **No per-object/global export override hooks.** `filetree_create` has
  `templates_overrides_resources` / `templates_overrides_global` to rewrite fields at
  export time (e.g. a different `scm_branch` per job template). No equivalent here.
- **No per-object output tree.** Unlike `filetree_create`'s structured mode, everything
  is flattened to one file per component type. This is intentional — it's the whole
  reason this collection avoids the `pids_limit` problem — but it means there's no
  equivalent of `filetree_create`'s per-organization directory layout.
- **Not directly reimportable via `filetree_read` → `dispatch` (`infra.aap_configuration`).**
  This collection's output is deliberately flat (one file per component type) and keyed
  by API endpoint name, not `filetree_create`'s nested per-object/per-org tree with
  import-role variable names. Treat this collection's output as backup/audit/diff
  material, not a drop-in `filetree_create` replacement for reimport, until the mapping
  layer scoped in CLAUDE.md milestone 14 is built.

See `CLAUDE.md`'s **Milestone tracking** section for the full, prioritized list of
these gaps and the reasoning behind the ordering.