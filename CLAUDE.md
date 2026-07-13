# CLAUDE.md — standards for the sankhek AAP CaC collection

This file exists so that any future work on this collection — by Claude or by anyone
else — stays consistent with the decisions already made. Read this before adding or
changing a module or playbook here.

## Why this collection exists

`infra.aap_configuration_extended.filetree_create` forks one Ansible process per
*object* it exports (one credential = one fork, one job template = one fork). On
OpenShift-hosted execution environments this exhausts the namespace's `pids_limit` on
anything but a small AAP instance. That's the entire reason this collection exists.
Every design decision here should be checked against that constraint.

## The one rule that must never be violated

**Never fork per object.** Forking per *component type* (organizations, job_templates,
credentials, ...) is fine — that's bounded by how many object types AAP has (~30-45),
not by how much data is in them. Forking per *object within* a type is exactly the bug
this collection was built to avoid. If a change introduces an Ansible `loop:` over
individual objects (not component types) calling out to the API or writing a file per
iteration, it's wrong — the pagination and the write both belong inside a single module
invocation's Python process, using `open_url` pagination loops like the existing
modules do.

If you're not sure whether a proposed change violates this, ask: "does the number of
forked processes scale with the number of *records* in AAP, or with the number of
*object types*?" It must always be the latter.

## Module conventions

- Every module needs `DOCUMENTATION`, `RETURN`, and (for anything with a non-obvious
  call signature) `EXAMPLES` blocks, in the same style as the existing three modules.
- Auth params are always `controller_host` (required), plus optional
  `controller_username` / `controller_password` / `controller_oauthtoken`
  (`no_log: true` on password and token). Don't invent a different auth param shape
  for a new module — match `aap_export_bulk.py`'s pattern exactly.
- `validate_certs` defaults to `true` at the module level. Playbooks may default it to
  `false` for convenience against self-signed internal instances, but the module
  itself should never silently assume insecure.
- A 404 on a component's endpoint means "not applicable to this AAP version" and
  should `exit_json` with `skipped: true`, not `fail_json`. Only fail hard on real
  errors (401/403/5xx/network). See `_do_get` / `fetch_all_pages` in
  `aap_export_bulk.py` for the pattern.
- Non-fatal partial failures (e.g. one of ten id-map types couldn't be built) should
  be collected into an `errors`/`excluded` dict in the return value and surfaced via a
  `debug` task in the playbook — never silently swallowed, never a hard failure that
  kills the whole run over one degraded piece.

## Naming conventions

- A discovered component's `name` is used as **both** the output filename
  (`<name>.yml`) and the top-level YAML key inside that file. Keep these identical —
  don't let them drift, since that's the one thing keeping the output predictable.
- Gateway-root objects are prefixed `gateway_` (e.g. `gateway_users`,
  `gateway_role_definitions`) to disambiguate from their Controller-root counterparts
  where names would otherwise collide (`users`, `role_definitions`). Apply the same
  pattern if EDA (`eda_`) or Hub (`hub_`) discovery is added later.
- This naming is NOT the same as what `infra.aap_configuration`'s import roles expect
  as variable names (e.g. it expects `aap_user_accounts`, not `users` or
  `gateway_users`, for the users import). If direct reimport compatibility becomes a
  goal, that requires an explicit mapping layer — don't assume matching the endpoint
  name is enough. See README.md's Known Limitations.

## Foreign-key resolution rules

- **Resolution order matters: `summary_fields` first, `id_maps` second.** Every
  AWX/AAP API object embeds a `summary_fields` block with already-resolved names for
  its FK relations (`summary_fields.user.username`, `summary_fields.organization.name`,
  etc.) — this is a standard AWX/DAB serializer convention, present on Controller AND
  Gateway roots alike, at zero extra API cost, and it's generated from the SAME
  identity space the object itself came from, so it can never drift out of sync the
  way a separately-built lookup table can. `clean_object()` in `aap_export_bulk.py`
  checks `summary_fields` first and only falls back to `id_maps` if that field has no
  entry there. Don't strip `summary_fields` before extracting from it — it's in
  `STRIP_FIELDS` only so it doesn't end up in the final output, not so it gets ignored.
- `aap_build_id_maps.py`'s `MAP_TYPE_CONFIG` and `aap_export_bulk.py`'s
  `FK_SCALAR_FIELDS` / `FK_LIST_FIELDS` don't need to be extended together anymore -
  a `map_type` with no corresponding entry in `MAP_TYPE_CONFIG` (e.g. `role_definitions`,
  `authenticators`) is fine, since resolution for those fields happens via
  `summary_fields` and the fallback lookup just safely misses.
- Only resolve a field if a name is actually found (by either method). Never guess,
  never resolve against a "closest match" — an unresolved FK must stay as a raw id
  and increment `unresolved_fk_count`, so the playbook's fail-fast check can catch it.
  A wrong silent resolution is worse than a loud missing one.
- **Root cause of the eda_activation/eda_edacredential/eda_eventstream unresolved-FK
  failure (fixed, 2026-07-11, static-checked only):** `aap_build_id_maps.py`'s
  `MAP_TYPE_CONFIG` only ever queried Controller endpoints. EDA has its own id space
  for organizations/credential_types/decision_environments (confirmed: EDA's
  `credential-types` returned 28 objects vs Controller's 33), so the id_maps fallback
  could only match an EDA object's id by coincidence, and `summary_fields` alone
  wasn't enough to cover every case (unconfirmed whether EDA's list-serializer
  reliably includes it for every field - worth checking against a live payload).
  Fixed by adding EDA-scoped map_types (`eda_organizations`, `eda_credential_types`,
  `eda_decision_environments`, `eda_credentials`) built against `eda_host`, and
  making `clean_object()` api_root-aware via `EDA_FK_MAP_OVERRIDE` so it checks the
  right map. Same fix class `filetree_create` shipped for its EDA templates
  (see its CHANGELOG: "Fix organization query in EDA decision environments
  template..."). Separately fixed: `activation.eda_credentials` / `.event_streams`
  are lists of embedded objects, not raw ids - they weren't in `FK_LIST_FIELDS` and
  wouldn't have worked there anyway (that path assumes scalar ids), so any FK inside
  them was silently unresolved and never counted. `clean_object()` now recurses into
  `EDA_EMBEDDED_LIST_FIELDS` (and one level deeper for `event_streams[i].eda_credential`
  via `EDA_NESTED_SINGLE_OBJECT_FIELDS`). **Re-run against the live Test instance
  on 2026-07-11 (`export_bulk.yml -e target_env=dev`): still failed**, with the exact
  same three components (`eda_activation` 8/2 objects, `eda_edacredential` 10/5,
  `eda_eventstream` 1/1) unresolved - i.e. the previous fix was necessary but not
  sufficient. The per-object unresolved counts divide evenly (4/object, 2/object,
  1/object), which is the signature of a systematic field-shape mismatch, not
  stale/missing data. **Hypothesis (unit-tested against synthetic payloads shaped
  this way, NOT yet reconfirmed against a live EDA response body - open item below):**
  some `/api/eda/v1/` endpoints return certain FK fields (`organization`,
  `credential_type` on eda-credentials; likely `organization`/`project`/
  `decision_environment`/`user` on activations) as a fully EMBEDDED OBJECT
  (`{"id": 3, "name": "..."}`), not a bare scalar id the way Controller/Gateway do -
  EDA-server's serializers don't follow the AWX summary_fields convention uniformly.
  `clean_object()`'s scalar/list FK loops assumed `cleaned[field]` was always a bare
  id; when it's actually a dict, `str(raw_id)` never matches an id_maps key
  (`"{'id': 3, ...}"` vs `"3"`), which is a 100%-miss, matching the clean divisibility
  seen. Fixed via a new `_resolve_fk_value()` helper that both scalar and list FK
  loops now go through: it detects a dict value, tries the embedded object's own
  name first, then falls back to id_maps keyed by the embedded `id` - and if still
  unresolved, stores the embedded id (not a raw dict repr) so the output stays
  sane either way. **Open item: confirm against one real
  `/api/eda/v1/eda-credentials/` and `/api/eda/v1/activations/` response body (e.g.
  `curl` one object, or add `-vvv` and inspect the raw JSON before `clean_object`
  strips it) that this is actually the shape - the fix is defensive/backward-compatible
  either way (scalar-id shape is unit-tested as unchanged), but the root-cause
  explanation above is inference from the count pattern, not a confirmed payload.**
- **Root cause of the original gateway-users bug (fixed):** id maps for `users`/`teams`
  were built from the *Controller* API only, and a gateway-referenced user id (2)
  wasn't present in that map, since Gateway is the authoritative identity source in
  AAP 2.5+ and isn't guaranteed 1:1 with what Controller's endpoint surfaces to a given
  token. Confirmed and fixed by switching to `summary_fields`-first resolution
  (verified against the actual failing objects from a live run) rather than by trying
  to build a second, gateway-specific id map — the summary_fields fix is more general
  and covers cases a hand-maintained second map wouldn't (e.g. `role_definition`,
  `authenticator` references, which also weren't in the resolvable field list at all
  until this fix).
- **`eda_activation.project` unresolved-FK gap (fixed, 2026-07-13, static-checked
  only — not yet confirmed against a live `eda_activation` object with a real
  `project` value; see the 2026-07-13 open-item section below for why the very next
  live run didn't exercise this path).** Same root-cause class as the
  organizations/credential_types/decision_environments fix above, just one field that
  fix missed: EDA has its own project id space (`aap_discover_components` finds it as
  a separate `eda_project` at `/api/eda/v1/projects/`, distinct from Controller's
  `/api/controller/v2/projects/`), but `MAP_TYPE_CONFIG` had no `eda_projects` entry
  and `EDA_FK_MAP_OVERRIDE` had no `"projects"` key, so `activation.project` fell
  through to the Controller-scoped `projects` map and matched only by coincidence.
  Fixed by adding `eda_projects` (`/api/eda/v1/projects/`) to `MAP_TYPE_CONFIG` and
  `"projects": "eda_projects"` to `EDA_FK_MAP_OVERRIDE`, same pattern as the other
  three EDA-scoped overrides. **Open item: confirm against a live `eda_activation`
  object that actually has a non-null `project` field that it now resolves to a name
  instead of a raw id** — none of the runs so far (2026-07-11 dev run predates this
  fix; 2026-07-13 UAT run had `include_eda: false`) have exercised this specific path
  live.

## Open item found 2026-07-12, fixed same day (static-checked + unit-tested only)

- **`_id`-suffixed EDA scalar FK fields were silently invisible to `clean_object()`,
  and NOT counted toward `unresolved_fk_count`.** Found by reading the actual
  `cac_all_files_output.txt` from the 2026-07-12 live run (the one that first got a
  clean `unresolved_fk_count: 0` across the board) line-by-line rather than trusting
  the zero: `eda_activation.yml` had `decision_environment_id: 1` and
  `organization_id: 1` sitting as raw, unresolved ids with no name anywhere in the
  file, and the nested `event_streams[].eda_credential` objects in both
  `eda_activation.yml` and `eda_eventstream.yml` had the same problem for
  `credential_type_id`/`organization_id`. Root cause: `FK_SCALAR_FIELDS` is keyed by
  bare field names (`organization`, `credential_type`, `decision_environment`), and
  `clean_object()`'s loop did `if field not in cleaned: continue` - but these specific
  EDA fields arrive ONLY in `<field>_id` form (no bare-named key, no embedded dict),
  so the loop never even saw them. Worse than the earlier embedded-dict bug: that one
  at least incremented `unresolved_fk_count` so the playbook's fail-fast check could
  catch it; this one didn't touch the field at all, so it stayed silently uncounted -
  exactly the kind of gap the "never silently swallowed" rule in this file exists to
  prevent. Fixed in `clean_object()`'s scalar FK loop: it now also checks
  `<field>_id` when the bare field name isn't present, resolves it the same way, and
  writes the resolved name under the bare field name (adding a new key alongside the
  existing `<field>_id`, same coexistence pattern EDA's `project`/`project_id` already
  uses natively) - if unresolved, it now correctly increments `unresolved_fk_count`
  instead of silently doing nothing. Unit-tested against the exact shapes pulled from
  the live output (top-level activation, nested eda_credential, and a synthetic
  genuinely-unresolvable id to confirm the counter now fires) plus a regression test
  of the existing Controller bare-field/summary_fields path. **Not yet re-run against
  the live Test instance** - next live run should confirm `eda_activation.yml`
  and `eda_eventstream.yml` now show `decision_environment`/`organization`/
  `credential_type` name fields instead of only the `_id` forms, and should double
  check there wasn't a second organization in play that this was masking (only 1 org
  had any objects in this run, so a wrong-organization-id case wouldn't have been
  visible either way).

## Open item found 2026-07-13, fixed same day

- **`aap_build_id_maps` always tried to build the full default map-type set, `eda_*`
  included, even when `include_eda: false` (or against an AAP instance with no EDA
  root deployed at all) — burning 5 guaranteed-failing API calls and logging 5
  `HTTP 503` "id_maps build issues" lines on every single run against such an
  environment, for maps nothing in that run could ever need.** Found from a live UAT
  run (`export_bulk.yml -e target_env=uat -e include_eda=false`) that itself
  succeeded cleanly (`PLAY RECAP: failed=0`, no unresolved FKs — correct, since with
  EDA excluded from discovery no `eda_*` objects were ever exported to need those
  maps) but logged `eda_organizations`/`eda_credential_types`/
  `eda_decision_environments`/`eda_credentials`/`eda_projects` all failing with
  `HTTP 503 fetching https://.../api/eda/v1/.../` — non-fatal, but pure noise, and
  would repeat on every future run against any EDA-less environment. Root cause:
  `export_bulk.yml`'s `aap_map_types` var was `{{ id_map_types | default(omit) }}` —
  unless the caller explicitly passed `id_map_types`, the module always fell back to
  its own full default `map_types` list, with no connection at all to the
  `include_eda`/`include_hub` toggles that already gate *discovery*. Fixed by adding
  `aap_all_map_types` (the full default set, kept explicitly in the playbook rather
  than left implicit in the module) and `aap_map_types_auto`, which rejects
  `eda_`-prefixed entries from that list when `aap_include_eda` is false;
  `aap_map_types` now resolves to `id_map_types` if explicitly set (unchanged,
  no trimming applied — explicit always wins outright), else to
  `aap_map_types_auto`. No `hub_`-prefixed map types exist in `MAP_TYPE_CONFIG` yet
  (Hub v1 is config-only and doesn't need FK resolution the way EDA's
  `activation.project`/`decision_environment`/etc. do), so there's nothing to trim
  there today — add the equivalent `reject('match', '^hub_')` guarded by
  `aap_include_hub` if/when a Hub-scoped map type is ever added. Verified via
  `ansible-playbook --syntax-check` plus four real `ansible-playbook -e ...` renders
  of just the vars block (`include_eda=false` → 10 Controller/Gateway-only types,
  no `eda_*`; `include_eda=true` and unset/default-true → all 15; explicit
  `id_map_types` → exactly that list, trimming skipped) — **not yet re-run against
  the live Test UAT instance to confirm the `HTTP 503` lines are actually gone
  end-to-end**, only the Jinja logic itself has been exercised for real.

## Before shipping any change here

1. `python3 -m py_compile` every changed module.
2. Parse the playbook with `yaml.safe_load_all` to catch YAML errors before a live run.
3. If you don't have a live AAP instance to test against, say so explicitly rather than
   implying it's been validated end-to-end — static checks are not the same as a real
   run, and this collection has already caught real bugs (the gateway user id gap
   above) only by being run against real data.
4. Update README.md's variable table and Known Limitations section in the same change
   — don't let documentation drift from what the playbook actually accepts.

## Milestone tracking

Longer-term gaps versus `filetree_create` are tracked as milestones below, in
priority order (1 = work on next, highest → lowest). This list was checked against
`filetree_create`'s actual role variables and output tree (not just its README
prose) on 2026-07-11 to make sure nothing was missed. Update it as milestones are
completed or reprioritized — don't let it go stale.

1. ~~FK-to-name resolution~~ — done. Primary path is `summary_fields` (present on
   every AWX/AAP object, always in sync with its own api_root); `id_maps` is a
   fallback. Fixed a real bug found in testing (gateway-referenced user id missing
   from a controller-built map) by switching to this order rather than patching the
   old map-only approach.
2. ~~Retry/backoff on transient API failures~~ — done, 2026-07-11 (unit-tested against
   synthetic 503/network-error/404 responses, NOT yet exercised against a real transient
   failure on a live instance). `aap_export_bulk.py`'s single-attempt `_do_get` is now
   `_do_get_once` (unchanged behavior) wrapped by a retrying `_do_get` that every caller
   (`fetch_all_pages`, `fetch_single_object`) already goes through unmodified. Retries
   HTTP 429/500/502/503/504 and network-level failures (`URLError`/`socket.timeout` -
   DNS, connection refused, TLS handshake, read timeout - previously uncaught and would
   have propagated as an unhandled exception instead of the module's normal fail_json
   path) with exponential backoff. Never retries 401/403/404/other 4xx - those are
   deterministic given the same token/path. New params: `retry_max_attempts` (default 3
   total attempts), `retry_backoff_seconds` (default 2s base, doubles each attempt; 0
   for immediate retries). Set `retry_max_attempts: 1` to fully restore old behavior.
   This was the top-priority gap for anyone relying on this for real reimport backups -
   previously a single transient 5xx/network blip mid-run on a 47-component paginated
   export failed the whole play with no resume.
3. ~~Doc-block `EXAMPLES` gap~~ — done, 2026-07-11. `aap_discover_components.py` and
   `aap_build_id_maps.py` both now have `EXAMPLES` blocks matching `aap_export_bulk.py`'s
   style (a default-params example plus one showing the more interesting optional
   params - `include_gateway`/`include_eda`/`include_settings` toggles for discovery,
   a trimmed `map_types` list for id-map building), closing the gap against CLAUDE.md's
   own module-convention rule that anything with a non-obvious call signature needs one.
4. ~~`organization_filter` not applied to `gateway_*`/`eda_*` objects~~ — done, 2026-07-11.
   `export_bulk.yml` now passes `target_org` regardless of `item.api_root` (previously
   gated to `controller` only). This is safe because `aap_export_bulk.py`'s
   `fetch_all_pages()` already auto-detects AAP's "this model has no organization field"
   400 response (`_is_org_field_error`) and transparently retries without the filter for
   that specific component - a gateway/EDA object type with no org field degrades to
   all-orgs export exactly like Organization/User/CredentialType already do on the
   Controller side, rather than hard-failing. **Not yet confirmed:** that this doesn't
   just avoid erroring but actually returns the *correct* org-scoped subset on Gateway/EDA
   object types that DO have an organization field - Gateway/EDA's org-scoping field
   name/semantics haven't been checked against a live instance to confirm they match
   Controller's `organization__name` convention. Flag this on the first live run against
   an instance with more than one organization and gateway/EDA objects split across them.
5. ~~Secrets handling (strip vs. vault-placeholder)~~ — implemented AND confirmed live,
   2026-07-12 (Test dev run, `secrets_as_variables=true`). `aap_export_bulk.py` has
   `secrets_as_variables` (default `true`) / `secrets_as_variables_prefix` (default
   `vaulted`) params. Any field found equal to the literal string AAP substitutes for
   an encrypted value (`$encrypted$` — this is what AAP returns for EVERY encrypted
   field on EVERY GET response; it never returns real secret values over the API, full
   stop) is rewritten to `{{ <prefix>_<object_type>_<object_name>_<field_path> }}` via
   a recursive `vaultize_secrets()` helper (reaches nested fields like a credential's
   `inputs.password`, not just top-level fields). `export_bulk.yml` aggregates every
   component's created variable names into one `vaulted_vars_template.yml` stub in the
   output dir (values `"CHANGEME"`) so there's a single file to fill in and
   `ansible-vault encrypt` before reimport. Detection is by VALUE, not a hand-maintained
   field-name list, so it automatically covers credentials, users, gateway
   authenticators, notification templates, and settings alike without per-type wiring.
   The live run confirmed the `$encrypted$` detection actually fires against real AAP
   2.5+ GET responses (not just unit-test fixtures): 25 vaulted vars generated across
   `users`, `credentials`, `gateway_applications`, `gateway_users`, `eda_activation`,
   `eda_edacredential`, `eda_eventstream`, `controller_settings`, and `gateway_settings`
   — including nested paths like
   `vaulted_eda_activation_service_down_remediation_eda_credentials_0_inputs_oauth_token`,
   confirming the recursive/nested-field path works, not just top-level fields.
6. ~~Controller/Gateway settings export~~ — implemented AND confirmed live,
   2026-07-12. `aap_discover_components.py` unconditionally appends two
   live AAP instance. `aap_discover_components.py` now unconditionally appends two
   hardcoded components (same pattern as `aap_build_id_maps`' `MAP_TYPE_CONFIG`,
   not a root-index walk): `controller_settings` → `/api/controller/v2/settings/all/`,
   and (if `include_gateway`) `gateway_settings` → `/api/gateway/v1/settings/all/`,
   each tagged `kind: settings`. `aap_export_bulk.py` gained an `object_shape`
   param (`list` default, `dict` for these) with a `fetch_single_object()` path
   that does one GET instead of a pagination loop, and writes the output file as
   `{object_type: <flat dict>}` instead of `{object_type: [<list>]}`. The playbook
   passes `object_shape: dict` and skips `organization_filter` for anything tagged
   `kind: settings` (settings has no organization field). The live run confirmed BOTH
   paths exist and return real data on this AAP instance: `controller_settings.yml`
   (count 1, 1 secret vaulted - `subscriptions_password`) and `gateway_settings.yml`
   (count 1, 3 secrets vaulted - `jwt_private_key`, `redhat_password`,
   `subscriptions_password`) both exported cleanly, no 404s. The Gateway path guess
   was correct - no longer "best guess," confirmed against a real Gateway root.
7. ~~Output YAML safety for survey/extra_vars fields~~ — implemented AND confirmed
   live, 2026-07-12. Investigated as two separate failure modes, since they turned out
   to need different treatment:
   - "Numeric-looking strings get corrupted" (`123-123-123`, `0123`, `yes`, ...) —
     tested directly against this module's actual `yaml.safe_dump`/`safe_load`
     round-trip and confirmed NOT a live bug: `SafeDumper` already quotes any plain
     scalar that would otherwise resolve to a non-str implicit type, and it reads
     back identical. No code change needed for this half.
   - "Jinja-looking strings get re-interpreted as templates on reimport" — this IS a
     real, distinct risk (YAML round-trips the string fine; it's Ansible's own Jinja
     templar re-evaluating `{{ ... }}` content at reimport time that's the danger).
     Fixed via `mark_unsafe_templates()` in `aap_export_bulk.py`: any
     Jinja-looking string inside `extra_vars`, `extra_data`, `source_vars`,
     `variables`, or `survey_spec` (however nested) is wrapped and written with an
     explicit `!unsafe` YAML tag via a `_CaCDumper` (subclasses `SafeDumper`),
     the same mechanism Ansible core itself uses for `no_log`/unsafe data. Toggle:
     `yaml_mark_unsafe_templates` (default `true`). The live run gave the first real
     (non-synthetic) confirmation: `hosts.yml`'s one host has
     `variables: !unsafe '{"ansible_connection": "local", "ansible_python_interpreter":
     "''{{ ansible_playbook_python }}''"}'` — a genuine Jinja-looking value from a real
     AAP object, correctly detected and tagged (`unsafe_templates_tagged: 1` in that
     task's result), and the emitted YAML in `cac_all_files_output.txt` shows the
     `!unsafe` tag rendered correctly, not mangled. Still open: this confirms the tag
     is *emitted* correctly, not that `infra.aap_configuration`'s reimport roles
     actually *honor* `!unsafe` the way core `ansible-playbook` does when consuming
     var files - that depends on how that role loads vars, and hasn't been tested
     through an actual reimport yet. `survey_spec` itself still isn't fetched by this
     collection (see the note in `UNSAFE_TAG_FIELDS`'s docstring) — tagging is wired up
     for it in advance so nothing needs revisiting if that field is added later.
8. ~~Applications (OAuth2 apps) export~~ — turns out to already be working, just
   undocumented. `aap_discover_components.py`'s generic Gateway-root walk was never
   given an entry for `"applications"` in `GATEWAY_BLOCKLIST`, so dynamic-discovery
   mode (`explicit_path`, what `export_bulk.yml` actually uses) picks it up
   automatically as `gateway_applications` — confirmed by a live run producing
   `gateway_applications.yml` plus a correctly vaultized
   `vaulted_gateway_applications_..._client_secret` entry in
   `vaulted_vars_template.yml`. The old note in `aap_export_bulk.py`'s `API_PATHS`
   comment (now corrected) only ever applied to the legacy static `object_type=`
   table, not the dynamic-discovery path. No code change was needed here — this was
   a stale-documentation gap, not a missing feature. Still open, if it becomes
   important: nothing here validates the OAuth2 application's `redirect_uris` /
   `authorization_grant_type` fields specifically, or exercises the Gateway
   Applications auth model beyond what already worked for every other
   gateway-rooted component.
9. **EDA + Hub discovery** — EDA half implemented AND validated against a live
   instance (2026-07-11, Test dev - see the run that surfaced the two bugs below).
   Hub half still not started.
   - EDA: `aap_discover_components.py` probes `/api/eda/v1/` (AAP 2.5+, served through
     the Gateway front door) the same generic way it already probes Controller/Gateway.
     `aap_export_bulk.py` needed no *structural* changes for EDA specifically - dynamic
     discovery mode was already generic enough that EDA components flow through the
     same pipeline (secrets vaultizing, `!unsafe` tagging) Gateway components use.
     Added `decision_environment` to `FK_SCALAR_FIELDS` as a fallback resolver for
     `activation.decision_environment`. New params: `eda_host` (defaults to
     `gateway_host`), `include_eda` (default `true`).
   - **A first live run surfaced two real bugs, both fixed:**
     1. **EDA's root-index key naming is nothing like Controller/Gateway's, and my
        first-pass `EDA_BLOCKLIST` was built on a wrong guess about it (cross-checked
        against the `ansible.eda` collection's module names, not a real response -
        wrong call in hindsight for something this checkable).** Controller/Gateway
        key their index by plain resource name (`job_templates`, `applications`). EDA
        keys it by DRF's default-router convention: `<basename>-list` for every real
        paginated collection, PLUS one-off action endpoints that aren't collections at
        all (`config`, `session-login`, `session-logout`, `token-refresh`,
        `current-user`, `openapi-json/yaml/docs/redoc`). None of my guessed blocklist
        entries matched, so nothing got filtered and non-collection endpoints got
        exported as if they were paginated lists. Fixed: the walk now keeps ONLY
        `*-list` entries, strips the suffix to get the real object name (confirmed
        real names differ from what the `ansible.eda` collection's docs implied too -
        it's `activation` not `rulebook_activation`, `edacredential` not
        `credential`), and checks that basename against a corrected
        `EDA_BLOCKLIST_BASENAMES` (`activationinstance`, `auditrule`,
        `controller-token` [SECURITY - live AWX tokens], `rulebook` [derived from a
        project sync]).
     2. **A real, more serious latent bug this exposed rather than caused:**
        `aap_export_bulk.py` only ever built request URLs from `controller_host`,
        regardless of which API root a component came from, and blindly concatenated
        `base_url + path`. This "worked" for Gateway purely by accident (relative
        paths, `gateway_host` happened to equal `controller_host` in every environment
        tested so far) - `aap_export_bulk.py` didn't even HAVE a `gateway_host`
        param. It broke loudly the moment EDA returned genuinely ABSOLUTE URLs with an
        unreliable scheme/host baked in (`http://...` pointing at what's presumably
        EDA's own internal view of its hostname behind the OCP route's TLS
        termination, not the externally-reachable `https://` one) - producing a
        garbled `https://realhosthttp://realhost/...` string and a DNS failure.
        Fixed on both sides: `aap_discover_components.py` now normalizes any
        absolute-URL path to just its path component at discovery time (defensively
        applied to all three roots, not just EDA); `aap_export_bulk.py` gained
        `gateway_host`/`eda_host`/`api_root` params and now picks base_url per
        component instead of always using `controller_host`, PLUS a second,
        independent defensive normalization for any absolute path that reaches it
        directly, PLUS the same fix applied to pagination `next`-link handling (which
        had the identical "trust the API's self-reported absolute URL" flaw for page 2
        onward - untested in the live run since none of the exported EDA collections
        had enough rows to paginate, but the flaw was structurally identical and cheap
        to fix at the same time).
     Reproduced the exact garbled URL from the failure and verified the fix against it
     with a unit test before calling this done - see the EDA section of
     `aap_discover_components.py`/`aap_export_bulk.py` for the reasoning inline.
     **Not yet re-validated with a fresh live run** - the fix is unit-tested against
     the exact data the failing run produced, but hasn't been confirmed end-to-end
     against the actual instance yet.
   - ~~Org-filtering not applied to `eda_*`/`gateway_*` components~~ — fixed as milestone
     4 above, 2026-07-11. Still open from THIS bullet specifically: confirm the filter's
     actual semantics (not just its non-fatal fallback) are correct on a live EDA/Gateway
     instance with multiple organizations - see milestone 4's note.
   - **`eda_activation.project` unresolved-FK gap** — fixed 2026-07-13, see the
     Foreign-key resolution rules section above. Related, same-day fix: id-map
     building now skips `eda_*` map types entirely when `include_eda: false` instead
     of always attempting (and failing noisily against) all of them regardless of the
     toggle - see "Open item found 2026-07-13" above.
   - ~~Hub/Automation Hub/Galaxy: still not started~~ — v1 implemented, 2026-07-12,

     **not yet validated against a live instance.** Confirmed live 2026-07-12: the
     Pulp API root at `/api/galaxy/pulp/api/v3/` self-reports absolute `http://` URLs
     with the same unreliable-internal-host problem as EDA's root (see the
     `_normalize_path` fix above) - same fix reused as-is, no new normalization logic
     needed. Design decision (2026-07-12): v1 scope is deliberately narrow and
     config-only - `namespaces` (ansible + container), `remotes` (ansible collection/
     git/role, container/pull-through/container, file), and `contentguards`/
     `access_policies` - via a new `HUB_ALLOWLIST` in `aap_discover_components.py`,
     the OPPOSITE approach from the blocklists used for Controller/Gateway/EDA,
     because Hub's root index is dominated by actual content and runtime artifacts
     (collection versions, container blobs, publications, tasks, workers) rather than
     config - blocklisting down from ~50 keys would be far more fragile than naming
     the config-relevant handful directly. `include_hub` defaults `false` (unlike
     `include_gateway`/`include_eda`, which default `true`) since this is unvalidated.
     `aap_export_bulk.py` gained matching `hub_host` param and `"hub"` `api_root`
     choice, same base_url-dispatch pattern as `gateway_host`/`eda_host`.
     Deliberately deferred out of v1: `repositories`/`distributions`/`content/*`
     (repo/content *state*, not declarable config), `users`/`groups`/`roles` under
     the Hub root (ambiguous whether still separate from Gateway's RBAC in this
     topology, or fully delegated - see `HUB_ALLOWLIST` comment), `domains`/
     `signing-services`/`acs` (plausible v2 candidates, not prioritized). **Not yet
     run against a live instance** - static-checked and syntax-validated only.
10. ~~Service-level scoping (`services: [controller, gateway, eda, hub]` single var)~~ — done,
    2026-07-13, static-checked + Jinja-logic-simulated only (**not yet re-run against a live
    instance with `services` actually passed** - the 2026-07-13 sbx/UAT live runs both predate
    this change and used the old `include_gateway`/`include_eda`/`include_hub` booleans
    directly). `export_bulk.yml` gained `aap_services` (default
    `[controller, gateway, eda]`, matching the collection's original defaults so an
    invocation that never passes `services` behaves identically to before this milestone).
    `aap_include_gateway`/`aap_include_eda`/`aap_include_hub` are now
    `{{ include_gateway | default('gateway' in aap_services) }}` (etc.) - the old booleans
    still work exactly as before and take precedence when passed explicitly; only when one
    is left unset does it fall back to services-list membership. No changes needed to
    `aap_discover_components.py`, `aap_build_id_maps.py`, or `aap_export_bulk.py` - they
    still only ever see the three derived booleans, unchanged. Added a non-fatal "Warn on
    unrecognized or incomplete services list" debug task (catches typo'd service names and
    a missing `"controller"` entry - the latter is a no-op, not an error, since Controller
    can't actually be excluded). Verified via a standalone Jinja2 render of the
    `aap_services`/`aap_include_*` expressions across 5 cases (no vars passed; `services`
    subset dropping a root; explicit `include_eda=false` alone; explicit `include_eda=false`
    together with a `services` list that would otherwise include it) - all five produced the
    expected booleans, including confirming explicit `include_*` correctly overrides
    `services` when both are given. README's variable table updated in the same change.
11. **Per-object / global export override hooks** — not started, feature-parity item.
   `filetree_create` has `templates_overrides_resources` (per-named-object) and
   `templates_overrides_global` (applies-to-all-of-a-type) dicts that let a caller
   rewrite a field (e.g. force `scm_branch: dev` on one job template, `main` on the
   rest) at export time. We have no equivalent hook in `aap_export_bulk.py`.
12. ~~Static inventory content (hosts/groups) as an explicit opt-in~~ — done. `hosts`
   and `groups` moved out of `RUNTIME_OR_META_BLOCKLIST` into their own
   `INVENTORY_CONTENT_NAMES` set in `aap_discover_components.py`, merged into the
   effective blocklist only when `include_inventory_content` is `false` (the default -
   opt-in, not opt-out, matching the collection's "declare desired state, don't
   snapshot someone else's system of record" stance for dynamically-sourced
   inventories). New module param `include_inventory_content` (default `false`);
   `export_bulk.yml` exposes it as the `include_inventory_content` playbook var
   (`aap_include_inventory_content`, same default/passthrough pattern as
   `include_gateway`/`include_eda`). When excluded, the `excluded` dict now
   distinguishes "static inventory content, excluded by default" from "runtime/meta,
   not CaC-relevant" so it's clear from output alone why `hosts`/`groups` didn't show
   up, without reading the module source. Confirmed live: run against the Test dev
   instance did NOT export hosts/groups with the default `false`, consistent with the
   docs.
13. ~~Single-object filters~~ (`project_id`, `job_template_id`, `inventory_id`,
    `workflow_job_template_id`, `schedule_id`, `label_filter`) — done, 2026-07-11. No
    module changes needed - `aap_export_bulk`'s `extra_query_params` dict already
    supported ad hoc filters like `{"id": 42}`. `export_bulk.yml` now defines a
    `single_object_filter_map` dict (component name -> a `{"id": ...}`/`{"name": ...}`
    filter built only if the matching playbook var is set, `{}` otherwise) and passes
    `extra_query_params: "{{ single_object_filter_map[item.name] | default({}) }}"` in
    the export task. Each of the six named vars (`project_id`, `job_template_id`,
    `inventory_id`, `workflow_job_template_id`, `schedule_id`, `label_filter`) is a
    no-op unless explicitly set, so passing none of them behaves exactly like before -
    a full export. `label_filter` filters by `name` rather than `id` since labels are
    normally referenced by name, not id. Not combined with `organization_filter` beyond
    both landing in the same query string (AAP ANDs query params, so
    `organization_filter` + e.g. `job_template_id` together do work as "this job
    template, if it's in that org"). Only meaningful to set one id-style filter per run
    per component - setting more than one for the same component isn't validated
    against and the behavior depends on how AAP's API handles multiple simultaneous
    filters, not something this module enforces.
14. **Mapping layer for direct `filetree_read` → `dispatch` (`infra.aap_configuration`)
    reimport compatibility** — not started, scoped 2026-07-11. Today this collection's
    output is export/backup/diff-friendly, NOT directly reimportable through the
    `filetree_read` → `dispatch` pipeline, and that's by design, not an oversight - two
    separate, independent mismatches:
    - **Shape mismatch.** `filetree_read` expects `filetree_create`'s nested,
      per-object, per-organization directory tree. This collection deliberately
      flattens everything to one file per component type (`job_templates.yml`
      containing every job template as a list) - see the "one rule that must never be
      violated" section above. That flattening IS the fix for the `pids_limit`
      problem, so getting the nested tree back would mean reintroducing the
      fork-per-object cost this collection exists to avoid. Not fixable without
      contradicting the collection's core purpose - a mapping layer has to bridge this
      gap, not eliminate it.
    - **Naming mismatch.** Even setting shape aside, `dispatch`'s import roles
      (`infra.aap_configuration`) expect specific variable names -
      `aap_user_accounts`, not `users`; similarly for every other component - and this
      collection's output keys match the API endpoint name (see Naming conventions
      above), not the import role's expected var name. Already called out there and in
      README's Known Limitations, just tracked here as an actual milestone instead of
      a permanent footnote.

    **Scope of the fix, if/when this becomes a real goal (not started, no code written
    yet):** a small, separate transform playbook/role - NOT a change to
    `aap_export_bulk.py`/`aap_discover_components.py`/`aap_build_id_maps.py` themselves,
    since their flat/endpoint-named output is correct for their actual purpose
    (backup/audit/diff) and shouldn't be bent to serve reimport at the cost of that.
    The transform layer would:
    1. Read each flat `<component>.yml` this collection produces.
    2. Rename/reshape its top-level key and structure into whatever
       `infra.aap_configuration`'s corresponding import role expects (a lookup table
       from this collection's component names to the import role's variable names -
       needs to be built against the actual `infra.aap_configuration` role
       defaults/docs, not guessed).
    3. If per-org output is wanted (matching `filetree_create`'s layout), explode the
       flat list by each object's `organization` field into per-org files/vars.
    4. Leave the existing flat export untouched as the backup/audit artifact - this is
       an additive transform step that runs AFTER export, not a replacement for it.

    This is a real chunk of design + implementation work (a new mapping table between
    ~30-45 component types and their import-role variable names, at minimum), not a
    config toggle - don't let "just add a rename" scope-creep in without accounting for
    the per-org explosion and the mapping table's own maintenance burden as AAP versions
    change. Until this is built, treat this collection's output as backup/audit/diff
    material only, not a drop-in `filetree_create` replacement for reimport.

    **Status update, 2026-07-13: first cut built, using `dispatch` directly (not a
    custom apply engine).** Decision made explicitly: don't write a custom
    create/update engine for 60+ object types - `infra.aap_configuration.dispatch`
    already does idempotent create/update, correct ordering (Gateway -> Hub ->
    Controller -> EDA), and per-type error collection via
    `aap_configuration_role_errors`. Reinventing that would be a much bigger lift than
    export ever was. What was actually missing was a rename/merge bridge, which is now:
    - `vars/dispatch_component_map.yml` - one entry per exported component, verified
      2026-07-13 directly against the published `gateway_configuration_dispatcher_roles`
      / `controller_configuration_dispatcher_roles` / `eda_configuration_dispatcher_roles`
      lists (not guessed, not from training-data memory of the collection). Of the 63
      unique exported component names, 34 map cleanly to a dispatch var; 29 do not, each
      with an explicit `reason` (either a real gap - a role exists but isn't in the
      default dispatcher list, e.g. `role_definitions`/`role_user_assignments`/
      `role_team_assignments`/`eda_user`/`eda_credentialinputsource`, or no role exists
      at all yet, e.g. all Hub `access_policies`/`contentguards_*` types, most
      container-type Hub remotes - or a deliberate duplicate, e.g. `organizations`/
      `users`/`teams`/`eda_organization`/`eda_team` are the same data as their
      `gateway_*` counterparts and are skipped in favor of the Gateway source feeding
      `aap_organizations`/`aap_user_accounts`/`aap_teams` once instead of twice).
    - `playbooks/transform_to_dispatch.yml` - reads whichever `<component>.yml` files
      actually exist in `output_dir`, applies the map, writes a single
      `dispatch_vars.yml` (dispatch-var-named, ready for `include_vars` +
      `include_role: infra.aap_configuration.dispatch`) plus a `dispatch_gap_report.yml`
      listing every exported component that did NOT make it in, with why - so nothing
      silently vanishes on reimport.
    - `playbooks/apply_dispatch.yml` - thin example wiring `dispatch_vars.yml` into
      `infra.aap_configuration.dispatch` with `aap_configuration_collect_logs: true`.
    - **Found, not yet fixed, while building this:** `aap_discover_components.py` names
      both the `/api/gateway/v1/settings/` category-listing component AND the
      `/api/gateway/v1/settings/all/` dict-shape component `gateway_settings` (Controller
      avoids this by renaming its dict-shape one to `controller_settings`). Both write to
      the same `gateway_settings.yml`, so the second one silently overwrites the first.
      In the 2026-07-13 sbx run this happened to land in our favor (the dict-shape "all
      settings" export ran last, so today's `gateway_settings.yml` is actually the shape
      `dispatch` wants) - but that's task-ordering luck, not something to rely on. Real
      fix (rename one of the two, same pattern as `controller_settings`) not done yet.
    - **Not yet run against a live instance** - map coverage checked programmatically
      against the full 64-component 2026-07-13 sbx discovery output (all accounted for,
      none missing/extra), and both new playbooks are YAML-syntax-valid, but neither has
      been executed with real `ansible-playbook` yet, and `apply_dispatch.yml` has not
      been run against any target AAP instance. Hub v1's two real gaps
      (`access_policies`/`contentguards_*`) remain explicitly deferred, per direction
      2026-07-13 to look at those later rather than block this on them.

    **Re-split, same day (2026-07-13): transform moved to happen at EXPORT time, not
    dispatch time - AND reworked again to drop the merged file entirely.** Two rounds
    of direct feedback, both acted on:
    1. The rename/merge should happen once, at export time, automatically - not a
       separate manual step, and definitely not something dispatch-time code does.
    2. Why does a merged `dispatch_vars.yml` need to exist at all when
       `filetree_create` doesn't produce one? It doesn't - `filetree_create` writes
       one file per object type, already named with dispatch's own variable name, and
       `dispatch` just does `include_vars: dir: ...` over that directory. There's no
       reason our reimport path should look any different once the rename is done.

    Final shape:
    - `vars/dispatch_component_map.yml` - unchanged in spirit: one entry per exported
      component, `used_in_dispatch: true/false` + the reason either way, verified
      against the published dispatcher-role lists (see the original entry above for
      the full 34-mapped/29-excluded breakdown - now written out as static docs in
      README.md too, per direction to document exclusions there rather than only in a
      runtime-generated report).
    - `playbooks/tasks/build_dispatch_ready_dir.yml` - for every `used_in_dispatch:
      true` component, copies the already-exported file into
      `<output_dir>/dispatch_ready/<dispatch_var>.yml` with its top-level key renamed
      to match. One small file per component, same shape `filetree_create` produces -
      no merging, no combined dict, no separate `dispatch_vars.yml`/
      `dispatch_gap_report.yml` (dropped - a debug-output summary of what was
      written/left-out at the end of the task is enough at run time; the durable
      documentation of what's excluded and why now lives in README.md).
    - `export_bulk.yml` calls this automatically as its last step (unchanged from the
      previous cut), gated by `build_dispatch_vars` (default `true`).
    - `playbooks/apply_dispatch.yml` - two tasks, unchanged in spirit but now points at
      the directory instead of a single file: `include_vars: dir:
      "{{ output_dir }}/dispatch_ready"` then `include_role:
      infra.aap_configuration.dispatch`. This is now exactly the `filetree_read` +
      `dispatch` pattern, not a lookalike.
    - `playbooks/transform_to_dispatch.yml` still exists, still only for re-running
      the transform against an already-exported directory (e.g. after editing the
      map) without re-exporting from AAP - not part of the normal flow.
    - **Not yet run against a live instance** - same caveat as before, unchanged by
      this rework: map coverage was checked programmatically, YAML syntax is valid,
      nothing has executed against real `ansible-playbook` or a live target yet.

**Bug found via a live giv-aap-sbx run (2026-07-13), fixed same day, still not
yet re-confirmed against a live run:** a run against sbx wrote all 63 expected
`<component>.yml` files correctly, but `dispatch_ready/` came back completely
empty (`ls -l` showed the directory present, zero files in it). Root cause:
`Build dispatch_ready/ from this export` was the LAST task in `export_bulk.yml`,
positioned AFTER the two hard `fail:` tasks (`Fail the play if unresolved FK
references exist anywhere` / `Fail the play if any component export hit a REAL
error`). Either fail: task firing stops the play immediately - since
`fail_on_unresolved_fk` defaults to `true`, ANY unresolved FK anywhere (or any
single component's real error) meant `include_tasks:
tasks/build_dispatch_ready_dir.yml` was simply never reached, even though every
export file it would have read from was already on disk by that point. This is
a structural ordering bug, independent of whether the EDA embedded-dict FK fix
(above) is fully correct - it would silently zero out `dispatch_ready/` on any
run with ANY unresolved FK anywhere or ANY single failed component, which is a
much easier condition to hit than "the EDA fix has a remaining gap."
Fixed by moving the `Build dispatch_ready/` task to run immediately after the
vaulted-secrets summary and BEFORE both fail: tasks - dispatch_ready/ now always
gets built whenever export files exist, and the fail: tasks still run afterward
and still fail the play (so CI/automation still sees red), but a human re-running
by hand gets partial, inspectable dispatch_ready/ output instead of nothing, and
can judge whether the specific unresolved FKs / failed components actually block
their reimport instead of being blocked from seeing anything at all.
**Open item: this reorder has NOT yet been re-run against giv-aap-sbx or any
live instance** - YAML-syntax-valid (checked), and the dispatch_component_map.yml
coverage was separately cross-checked programmatically against the exact 63
component names from the 2026-07-13 sbx `ls -l` output (all present, all mapped,
no silent gaps in the map itself - the map was never the problem). Re-running
`export_bulk.yml` against sbx is the next step to confirm `dispatch_ready/`
actually populates now, and separately to get the live confirmation the EDA
embedded-dict FK fix (above) is still waiting on.

**Second bug, found immediately after re-running with the ordering fix above
(2026-07-13, same day) - `dispatch_ready/` still came back empty.** Root cause
this time: `playbooks/tasks/build_dispatch_ready_dir.yml`'s `include_vars` task
loaded `vars/dispatch_component_map.yml` WITHOUT a `name:` parameter. Without
`name:`, `include_vars` defines the file's ~63 top-level keys
(`instance_groups`, `settings`, `organizations`, ...) as 63 SEPARATE top-level
Ansible variables - it does NOT create a single variable called
`dispatch_component_map`, despite the file's own name and every downstream task
in this file assuming that variable exists (`dispatch_component_map | dict2items`
in both the `stat` loop and the `copy` loop). This was a hard Jinja "undefined
variable" error, which failed the task immediately after the (empty)
`dispatch_ready/` directory got created by the preceding task, before the
stat/copy loop populated anything.

This bug was PRE-EXISTING in the original 2026-07-13 giv-aap-sbx run too, but
was invisible there because the task-ordering bug (above) killed the play at
the `fail_on_unresolved_fk` check before this task list was ever reached at
all - one bug was masking the other. Fixing the ordering surfaced this one.

Confirmed with a real `ansible-playbook` run (not just static reasoning) against
a local fixture directory shaped like the actual sbx export: the un-fixed task
reproducibly fails with `'dispatch_component_map' is undefined` right after
creating the empty dispatch_ready/ dir; adding `name: dispatch_component_map`
to the `include_vars` task fixes it - verified output included correctly
renamed/reshaped files (e.g. `eda_activation.yml`'s `eda_activation:` key
written out as `dispatch_ready/eda_rulebook_activations.yml`'s
`eda_rulebook_activations:` key, matching `dispatch_component_map.yml`'s
mapping exactly).

**Still open:** neither fix has been re-run against the actual giv-aap-sbx
instance yet (only against a local fixture with real Ansible). Also still open:
live confirmation of the EDA embedded-dict FK resolution fix - now that
dispatch_ready/ should actually populate on the next real run, the export
summary's per-component `unresolved_fk_count` for eda_activation/
eda_edacredential/eda_eventstream will finally be visible again (it was masked
both times dispatch_ready/ came back empty, since the play failed either before
or without ever getting past the export step's own debug summary in a way that
would have been captured in what was shared back).

## Milestone: one-pass export directly to dispatch_ready/ (2026-07-13)

Removed the two-pass export-then-rename pattern. Previously: `aap_export_bulk`
wrote every discovered component to `<output_dir>/<raw_name>.yml` under the
raw endpoint-discovered name, then a second task list
(`playbooks/tasks/build_dispatch_ready_dir.yml`, invoked from
`export_bulk.yml`, or standalone via `playbooks/transform_to_dispatch.yml`)
read `vars/dispatch_component_map.yml`, `stat`-checked, `include_vars`-loaded,
and `copy`-rewrote each `used_in_dispatch: true` component a second time into
`dispatch_ready/<dispatch_var>.yml`.

This was reviewed against `infra.aap_configuration_extended.filetree_create`
as a best-practices reference: `filetree_create`'s per-component task queries
the API and renders its Jinja template straight to the final destination in
the same task - the template itself opens with the dispatch variable name
(`aap_organizations:`, not the raw endpoint name). There's no rename pass
there at all. Our two-pass version existed only because `aap_export_bulk.py`
always wrote under `object_type` (the raw name) with no way to override it.

Fixed by:
- `aap_export_bulk.py`: new optional `output_var_name` param. When passed, it
  is used as the write's top-level YAML key instead of `object_type`. Defaults
  to `object_type` when omitted, so any existing manual/non-dispatch usage of
  this module is unaffected.
- `export_bulk.yml`: loads `vars/dispatch_component_map.yml` right after
  discovery (before any export API call), and splits `discovery.components`
  into three lists before the export loop runs:
  - `dispatch_components` (`used_in_dispatch: true`) - looped and exported
    straight to `dispatch_ready/<var>.yml` with `output_var_name: <var>`.
  - `excluded_components` (`used_in_dispatch: false`) - **not looped by
    default, so no API call is made for these at all.** Only included in the
    loop when `export_excluded_components: true` is passed, in which case
    they're written to `excluded_or_informational/<name>.yml` - never mixed
    into `dispatch_ready/`.
  - `unmapped_components` (discovered but absent from the map entirely - a
    new/renamed AAP endpoint the map hasn't caught up to) - always exported,
    to `unmapped/<name>.yml`, and flagged in the "component split" debug
    output, so auto-discovery never silently drops data just because the map
    is stale.
- Removed `playbooks/tasks/build_dispatch_ready_dir.yml` and
  `playbooks/transform_to_dispatch.yml` entirely - nothing calls them, and
  there is no longer a scenario ("re-run the transform against an existing
  export without re-hitting AAP") that makes sense, since there's no
  intermediate raw-named export for dispatch-mapped components to re-transform
  in the first place.
- This also fixes the exact class of bug the old two-pass approach kept
  hitting (see the "Second bug" entry above this one, and its sibling further
  up the file): a missing `name:` on `include_vars`, or a hard `fail:` task
  running before the copy step, could each independently leave
  `dispatch_ready/` empty while the raw per-component files were already on
  disk. With one write per component, either the write happened (file exists,
  correctly named and keyed) or the task itself failed loudly for that
  component in the normal per-component error/summary path - there's no
  separate step left to silently not run.

**Not done in this pass (next):** the full enterprise collection restructure
(`galaxy.yml`, `roles/cac_export/` with `defaults`/`vars`/`tasks`, moving the
three modules into `plugins/modules/`, `meta/runtime.yml`, `changelogs/`) to
match `infra.aap_configuration_extended`'s layout. This milestone was scoped
to the export-destination logic only, per an explicit decision to sequence
the functional fix before the directory/layout restructure.

## Milestone: enterprise collection restructure (2026-07-14)

Repackaged as a real Ansible collection to match `infra.aap_configuration`'s
layout, per the "not done in this pass" note at the end of the previous
milestone. Namespace/name: `kushalsankhe.cac_export`.

Changes:
- Added `galaxy.yml` and `meta/runtime.yml` (`requires_ansible: ">=2.15.0"`).
- Moved the three modules from `library/` to `plugins/modules/` - unchanged
  otherwise, now referenced by FQCN (`kushalsankhe.cac_export.aap_export_bulk`,
  etc.) or transitively via the roles below.
- The old `export_bulk.yml` playbook body became `roles/cac_export/` (tasks +
  defaults + its own copy of `vars/dispatch_component_map.yml`). This is now
  the reusable unit - anyone can `include_role:
  name: kushalsankhe.cac_export.cac_export` from their own playbook instead
  of running this collection's playbook at all.
- The old `apply_dispatch.yml` playbook body became `roles/apply_dispatch/`
  the same way.
- `playbooks/export_bulk.yml` and `playbooks/apply_dispatch.yml` still exist,
  now as thin wrappers (`roles: [kushalsankhe.cac_export.cac_export]` /
  `[kushalsankhe.cac_export.apply_dispatch]`) - kept per standard collection
  practice: the role is the composable interface, the playbook is a ready-
  to-run convenience entry point, and existing invocations of either playbook
  keep working unchanged (same variable names).
- Role `defaults/main.yml` uses `null` for "no value set" instead of the
  previous playbook-vars pattern of `| default(omit)`. This incidentally
  fixes a real footgun: `organization_filter` used to default to Ansible's
  `omit` placeholder, which is itself a non-empty string, so
  `organization_filter is defined` read as truthy even on runs where no
  filter was ever passed - the export summary's "no organization field"
  caveat could theoretically misfire on that basis. `is not none` against a
  real `null` default doesn't have this problem. All other previously-omit
  vars (tokens, hosts, per-component id filters) got the same treatment.
- Added `changelogs/config.yaml` (antsibull-changelog) and a first fragment
  documenting this restructure, including the still-open `gateway_settings`
  discovery collision as a `known_issue` so it isn't lost.
- Did NOT add `tests/` (sanity/unit) in this pass - scoped out, same
  sequencing decision as before (functional correctness and layout first).
- Bonus finding from actually validating this with real tooling (not just
  static reasoning): all three modules had `DOCUMENTATION` options with no
  `description` field (`controller_oauthtoken`/`controller_username`/
  `controller_password`/`validate_certs` in all three; `page_size` in
  `aap_build_id_maps`/`aap_export_bulk`; `controller_host` in
  `aap_build_id_maps`; `extra_query_params` in `aap_export_bulk`) - this is a
  hard `ansible-doc` error (a real `ansible-galaxy collection build`/import
  would fail sanity checks over it), invisible while the modules lived loose
  under `library/` and were never run through `ansible-doc`. Fixed by adding
  a one-line description to each. Verified: `ansible-doc
  kushalsankhe.cac_export.<module>` now succeeds for all three (previously
  errored with "All (sub-)options and return values must have a
  'description' field" on all three).
- Verified with real `ansible-core` 2.21.1 (not just YAML syntax-checking):
  `ansible-playbook --syntax-check` passes for both wrapper playbooks against
  a fake `ANSIBLE_COLLECTIONS_PATH` pointed at this directory as
  `ansible_collections/kushalsankhe/cac_export`, and all three modules
  resolve and document correctly by FQCN.

**Not yet done / open:**
- `gateway_settings` double-discovery collision (see
  `roles/cac_export/vars/dispatch_component_map.yml`) - deferred, low risk,
  isolated one-line fix whenever it's picked up.
- This restructure has NOT been re-run against giv-aap-sbx or any live
  instance yet - directory layout and Jinja are consistent by inspection
  (var names lining up between `defaults/main.yml` and `tasks/main.yml`,
  `role_path` used correctly instead of `playbook_dir`), but a real
  `ansible-galaxy collection install` + `ansible-playbook
  kushalsankhe.cac_export.export_bulk` run against sbx is the next step
  before trusting this over the pre-restructure playbook-only version.
