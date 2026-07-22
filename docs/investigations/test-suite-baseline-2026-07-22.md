# Test suite inventory

This generated baseline classifies collected pytest cases by resource and isolation
contract. Counts are cases after parametrization, not test functions.

Source digest: `0088add37c5ee8acffcb628638387b412d0963f22a50593f9e45084889f3fa47` (326 inputs).

| Collected | Files | Parameterized cases | Families | Estimated blocking CI selected case slots |
|---:|---:|---:|---:|---:|
| 2943 | 86 | 1290 | 252 | 14754 |

## Contracts and lanes

| Kind | ID | Cases | Files | Variants | Skip policy | Django settings | Owner | Exact selection |
|---|---|---:|---:|---:|---|---|---|---|
| execution_contract | `hermetic` | 2133 | 59 | 1 | `allow` | `unset` | Pure-Python and mocked-boundary tests | `(path:tests) AND NOT marker:django_db AND NOT marker:live_cluster AND NOT marker:postgresql AND NOT marker:real_ray AND NOT fixture:admin_client AND NOT fixture:admin_user AND NOT fixture:db AND NOT fixture:django_db_reset_sequences AND NOT fixture:django_db_serialized_rollback AND NOT fixture:live_server AND NOT fixture:transactional_db` |
| execution_contract | `sqlite-django` | 742 | 41 | 1 | `allow` | `unset` | Django ORM and application integration tests | `(path:tests) AND (marker:django_db OR fixture:admin_client OR fixture:admin_user OR fixture:db OR fixture:django_db_reset_sequences OR fixture:django_db_serialized_rollback OR fixture:live_server OR fixture:transactional_db) AND NOT marker:live_cluster AND NOT marker:postgresql AND NOT marker:real_ray` |
| execution_contract | `local-ray` | 33 | 6 | 1 | `forbid` | `unset` | Ray Core and worker integration tests | `(path:tests) AND marker:real_ray AND NOT marker:live_cluster AND NOT marker:postgresql` |
| execution_contract | `postgresql` | 32 | 5 | 1 | `forbid` | `tests.postgres_settings` | Database coordination and PostgreSQL persistence tests | `(path:tests) AND marker:postgresql AND NOT marker:live_cluster AND NOT marker:real_ray` |
| execution_contract | `live-cluster` | 3 | 1 | 1 | `forbid` | `unset` | Ray cluster fault and transport integration tests | `(path:tests) AND marker:live_cluster` |
| domain | `workflow-progress` | 889 | 21 | 1 | `allow` | `unset` | Workflow progress preparation, persistence, reads, summaries, cleanup, and API | `(path:tests/integration/test_postgresql_workflow_progress_reads.py OR path:tests/integration/test_postgresql_workflow_progress_storage.py OR path:tests/integration/test_workflow_progress_api.py OR path:tests/integration/test_workflow_progress_detail_storage_migration.py OR path:tests/integration/test_workflow_progress_migration.py OR path:tests/integration/test_workflow_progress_summary_migration.py OR path:tests/unit/test_workflow_progress.py OR path:tests/unit/test_workflow_progress_benchmark_command.py OR path:tests/unit/test_workflow_progress_cleanup.py OR path:tests/unit/test_workflow_progress_cleanup_command.py OR path:tests/unit/test_workflow_progress_preparation.py OR path:tests/unit/test_workflow_progress_preparation_prototype.py OR path:tests/unit/test_workflow_progress_reads.py OR path:tests/unit/test_workflow_progress_storage_audit.py OR path:tests/unit/test_workflow_progress_storage_boundaries.py OR path:tests/unit/test_workflow_progress_storage_models.py OR path:tests/unit/test_workflow_progress_storage_persistence.py OR path:tests/unit/test_workflow_progress_storage_records.py OR path:tests/unit/test_workflow_progress_storage_scaling.py OR path:tests/unit/test_workflow_progress_summary.py OR path:tests/unit/test_workflow_progress_terminal_expiry.py)` |
| domain | `compiled-graph-and-kuberay` | 677 | 6 | 1 | `allow` | `unset` | Compiled Graph capability and guarded KubeRay evidence | `(path:tests/unit/test_compiled_graph.py OR path:tests/unit/test_compiled_graph_lifecycle.py OR path:tests/unit/test_compiled_graph_probe.py OR path:tests/unit/test_kuberay_compiled_graph_pilot.py OR path:tests/unit/test_kubernetes_web_probes.py OR path:tests/unit/test_local_kuberay_gate.py)` |
| domain | `repository-policy-and-release` | 224 | 6 | 1 | `allow` | `unset` | Repository policy, coverage review, packaging, and release | `(path:tests/unit/test_check_conventional_commits.py OR path:tests/unit/test_ci_workflow_policy.py OR path:tests/unit/test_coverage_debt.py OR path:tests/unit/test_package_metadata.py OR path:tests/unit/test_release.py OR path:tests/unit/test_test_suite_inventory.py)` |
| domain | `worker-and-ray-runtime` | 249 | 11 | 1 | `allow` | `unset` | Worker command, runner, distributed helper, and remote execution | `(path:tests/integration/test_worker.py OR path:tests/unit/test_distributed.py OR path:tests/unit/test_distributed_mocked.py OR path:tests/unit/test_ray_core.py OR path:tests/unit/test_ray_core_runner.py OR path:tests/unit/test_ray_job.py OR path:tests/unit/test_remote.py OR path:tests/unit/test_worker_command_coverage.py OR path:tests/unit/test_worker_command_runtime.py OR path:tests/unit/test_worker_mode_selection.py OR path:tests/unit/test_worker_reconnect_poll_reconcile.py)` |
| domain | `coordination-polling-and-recovery` | 152 | 9 | 1 | `allow` | `unset` | Coordination leases, polling, recovery, priority, and fault handling | `(path:tests/integration/test_failure_injection.py OR path:tests/integration/test_live_failure_injection.py OR path:tests/integration/test_postgresql_coordination.py OR path:tests/integration/test_postgresql_polling.py OR path:tests/unit/test_leasing.py OR path:tests/unit/test_polling.py OR path:tests/unit/test_polling_benchmark_command.py OR path:tests/unit/test_reconciliation.py OR path:tests/unit/test_worker_reconnect_poll_reconcile.py)` |
| domain | `schema-migrations-and-bootstrap` | 36 | 7 | 1 | `allow` | `unset` | Persisted-schema compatibility and application bootstrap | `(path:tests/integration/test_input_payload_migration.py OR path:tests/integration/test_priority_migration.py OR path:tests/integration/test_workflow_plan_migration.py OR path:tests/integration/test_workflow_progress_detail_storage_migration.py OR path:tests/integration/test_workflow_progress_migration.py OR path:tests/integration/test_workflow_progress_summary_migration.py OR path:tests/unit/test_entrypoint.py)` |
| domain | `workflow-plan-and-task-runtime` | 235 | 7 | 1 | `allow` | `unset` | Workflow plans, workflow execution, and Django task backend | `(path:tests/integration/test_task_execution.py OR path:tests/unit/test_backends.py OR path:tests/unit/test_cancellation.py OR path:tests/unit/test_lifecycle.py OR path:tests/unit/test_retry.py OR path:tests/unit/test_workflow_plans.py OR path:tests/unit/test_workflows.py)` |
| domain | `configuration-storage-and-observability` | 388 | 11 | 1 | `allow` | `unset` | Configuration, durable payload/result storage, and observability | `(path:tests/unit/test_input_storage.py OR path:tests/unit/test_logging.py OR path:tests/unit/test_metrics.py OR path:tests/unit/test_observability.py OR path:tests/unit/test_redaction.py OR path:tests/unit/test_result_buffer.py OR path:tests/unit/test_result_fold.py OR path:tests/unit/test_result_storage.py OR path:tests/unit/test_runtime_env.py OR path:tests/unit/test_serialization.py OR path:tests/unit/test_settings.py)` |
| domain | `django-admin-and-api` | 121 | 4 | 1 | `allow` | `unset` | Django admin and bundled HTTP API | `(path:tests/integration/test_admin.py OR path:tests/integration/test_admin_coverage.py OR path:tests/integration/test_api.py OR path:tests/integration/test_workflow_progress_api.py)` |
| boundary | `bundled-testproject` | 85 | 4 | 1 | `allow` | `unset` | Bundled Django sample application | `(path:tests/integration/test_api.py OR path:tests/integration/test_workflow_progress_api.py OR path:tests/unit/test_sample_security.py OR path:tests/unit/test_testproject_workflows.py)` |
| profile | `portable-local` | 2907 | 84 | 1 | `allow` | `unset` | Cross-platform local suite profiling | `(path:tests) AND NOT marker:live_cluster AND NOT marker:real_ray` |
| ci_lane | `supported-python` | 2940 | 85 | 3 | `allow` | `unset` | Required Python compatibility and source coverage gate | `(path:tests) AND NOT marker:live_cluster` |
| ci_lane | `postgresql-evidence` | 32 | 5 | 1 | `forbid` | `tests.postgres_settings` | Required PostgreSQL coordination gate | `(path:tests/integration/test_postgresql_coordination.py OR path:tests/integration/test_postgresql_metrics.py OR path:tests/integration/test_postgresql_polling.py OR path:tests/integration/test_postgresql_workflow_progress_reads.py OR path:tests/integration/test_postgresql_workflow_progress_storage.py) AND marker:postgresql` |
| ci_lane | `live-cluster-evidence` | 3 | 1 | 1 | `forbid` | `unset` | Required live-cluster fault gate | `(path:tests/integration/test_live_failure_injection.py) AND marker:live_cluster` |
| ci_lane | `testproject-contract` | 85 | 4 | 1 | `forbid` | `unset` | Required bundled-application gate | `(path:tests/integration/test_api.py OR path:tests/integration/test_workflow_progress_api.py OR path:tests/unit/test_sample_security.py OR path:tests/unit/test_testproject_workflows.py)` |
| ci_lane | `dependency-compatibility` | 2907 | 84 | 2 | `allow` | `unset` | Required minimum and latest dependency gate | `(path:tests) AND NOT marker:live_cluster AND NOT marker:real_ray` |

Execution contracts partition the collection. Boundaries and CI lanes overlap those
contracts intentionally. The estimate multiplies selected cases by current CI variants;
it measures selected pytest case slots before runtime skips, not completed execution
or wall-clock time. Nested JavaScript subtests are outside this estimate.

## Largest files

| File | Cases | Parameterized | Logical domains | Execution contracts |
|---|---:|---:|---|---|
| `tests/unit/test_kuberay_compiled_graph_pilot.py` | 307 | 246 | compiled-graph-and-kuberay | hermetic: 307 |
| `tests/unit/test_workflow_progress_preparation.py` | 195 | 125 | workflow-progress | hermetic: 195 |
| `tests/unit/test_check_conventional_commits.py` | 173 | 109 | repository-policy-and-release | hermetic: 173 |
| `tests/unit/test_local_kuberay_gate.py` | 156 | 69 | compiled-graph-and-kuberay | hermetic: 156 |
| `tests/unit/test_workflow_progress_summary.py` | 151 | 102 | workflow-progress | hermetic: 21, sqlite-django: 130 |
| `tests/unit/test_workflow_plans.py` | 103 | 34 | workflow-plan-and-task-runtime | hermetic: 89, sqlite-django: 14 |
| `tests/unit/test_compiled_graph_lifecycle.py` | 87 | 40 | compiled-graph-and-kuberay | hermetic: 87 |
| `tests/unit/test_workflow_progress_reads.py` | 86 | 25 | workflow-progress | sqlite-django: 86 |
| `tests/unit/test_settings.py` | 79 | 54 | configuration-storage-and-observability | hermetic: 79 |
| `tests/unit/test_workflow_progress_preparation_prototype.py` | 77 | 40 | workflow-progress | hermetic: 77 |
| `tests/unit/test_compiled_graph.py` | 75 | 53 | compiled-graph-and-kuberay | hermetic: 75 |
| `tests/unit/test_workflow_progress_storage_records.py` | 74 | 49 | workflow-progress | hermetic: 74 |
| `tests/unit/test_workflows.py` | 68 | 13 | workflow-plan-and-task-runtime | hermetic: 60, sqlite-django: 2, local-ray: 6 |
| `tests/unit/test_input_storage.py` | 65 | 36 | configuration-storage-and-observability | hermetic: 59, sqlite-django: 6 |
| `tests/unit/test_workflow_progress_storage_boundaries.py` | 63 | 22 | workflow-progress | hermetic: 30, sqlite-django: 33 |
| `tests/unit/test_workflow_progress_storage_persistence.py` | 55 | 13 | workflow-progress | hermetic: 2, sqlite-django: 53 |
| `tests/unit/test_workflow_progress_benchmark_command.py` | 52 | 37 | workflow-progress | hermetic: 52 |
| `tests/unit/test_result_storage.py` | 51 | 13 | configuration-storage-and-observability | hermetic: 51 |
| `tests/unit/test_worker_reconnect_poll_reconcile.py` | 51 | 0 | worker-and-ray-runtime, coordination-polling-and-recovery | hermetic: 8, sqlite-django: 43 |
| `tests/unit/test_observability.py` | 46 | 17 | configuration-storage-and-observability | hermetic: 26, sqlite-django: 20 |

## Largest parameterized families

| Family | Cases | Parameters |
|---|---:|---|
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_blocked_evidence_rejects_mutated_nested_context` | 38 | `mutation` |
| `tests/unit/test_check_conventional_commits.py::test_validate_rejects_missing_or_generic_validation_evidence` | 35 | `validation` |
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_blocked_evidence_requires_every_root_field` | 31 | `field` |
| `tests/unit/test_workflow_progress_summary.py::test_summary_rejects_invalid_scalar_boundaries` | 24 | `message, path, value` |
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_blocked_evidence_writer_independently_revalidates_every_invariant` | 23 | `error_match, mutation` |
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_pod_requires_each_exact_identity_environment_entry_once` | 21 | `mutation, variable_name` |
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_blocked_evidence_rejects_bool_and_float_integer_coercion` | 19 | `invalid_value, path` |
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_kuberay_operator_rejects_decoys_inventory_and_ownership_drift` | 18 | `mutation` |
| `tests/unit/test_check_conventional_commits.py::test_validate_rejects_modal_or_explanatory_result_prose` | 15 | `validation` |
| `tests/unit/test_workflow_progress_preparation_prototype.py::test_workspace_configuration_rejects_values_outside_preparation_v1` | 14 | `config` |
| `tests/unit/test_settings.py::TestValidateSettings::test_validate_numeric_settings_reject_booleans` | 13 | `setting` |
| `tests/unit/test_kuberay_compiled_graph_pilot.py::test_head_pod_requires_every_profile_ray_start_parameter_once` | 12 | `mutation, option` |
| `tests/unit/test_local_kuberay_gate.py::test_secret_token_rejects_unbounded_or_non_header_safe_values` | 12 | `token` |
| `tests/unit/test_result_buffer.py::test_actor_options_reject_unbounded_or_unsupported_values` | 12 | `actor_options, message` |
| `tests/unit/test_workflow_progress_preparation.py::test_candidate_signature_rejects_tampering_before_capability_issue` | 12 | `mutation, tamper_phase` |
| `tests/unit/test_workflow_progress_preparation.py::test_public_spill_preparer_has_byte_for_byte_materialized_parity` | 12 | `ordering, scenario` |
| `tests/unit/test_check_conventional_commits.py::test_validate_accepts_specific_validation_results` | 10 | `validation` |
| `tests/unit/test_result_storage.py::TestResultStorageFactory::test_result_reference_validation` | 10 | `expected, reference` |
| `tests/unit/test_workflow_plans.py::test_explicit_ray_defaults_match_omitted_options` | 10 | `ray_options` |
| `tests/unit/test_workflow_progress_preparation.py::test_capability_references_defeat_evidence_replacement_and_id_reuse` | 10 | `field` |

## Most-used fixtures

Fixture counts describe setup paths selected by collected cases; they are not timing
attribution. Inherited and autouse fixtures therefore appear on many or all cases.

| Fixture | Selected cases |
|---|---:|
| `_clear_django_ray_remote_caches` | 2943 |
| `_dj_autoclear_mailbox` | 2943 |
| `_django_clear_site_cache` | 2943 |
| `_django_db_marker` | 2943 |
| `_django_isolate_apps` | 2943 |
| `_django_set_urlconf` | 2943 |
| `_django_setup_unittest` | 2943 |
| `_fail_for_invalid_template_variable` | 2943 |
| `_live_server_helper` | 2943 |
| `_template_string_if_invalid_marker` | 2943 |
| `django_db_blocker` | 2943 |
| `django_test_environment` | 2943 |
| `monkeypatch` | 2943 |
| `request` | 2943 |
| `tmp_path` | 313 |
| `tmp_path_factory` | 313 |
| `_django_db_helper` | 171 |
| `db` | 171 |
| `django_db_createdb` | 171 |
| `django_db_keepdb` | 171 |

## Overlap review inventory

These are review candidates, not pre-approved deletions.

| Domain | Cases | Parameterized | Owner | Why inspect | Review boundary |
|---|---:|---:|---|---|---|
| `workflow-progress-protocol` | 237 | 127 | Workflow progress summary protocol and read fixtures | Pure summary codec cases pay for a database identity, while several read-order cases construct larger workflows than their cursor boundary requires. | Split DB-free protocol cases from ORM/lifecycle cases and right-size read cardinalities without merging HTTP adapter coverage. |
| `workflow-progress-preparation` | 272 | 165 | Workflow progress preparation and prototype | Production and prototype suites have similar ordering and reporting shapes but retain different end-to-end topology and detachment contracts. | Do not consolidate until a scheduled benchmark or replacement harness proves both production and prototype invariants. |
| `bundled-browser-contract` | 44 | 2 | Bundled dashboard authentication | Authentication setup overlaps endpoint cases, and one collected pytest case launches a nested Node suite whose subtests are repeated by every CI variant. | Share the configured token fixture, remove only proven endpoint overlap, and keep browser behavior as a distinct contract. |
| `kuberay-overlay-render` | 156 | 69 | Guarded local KubeRay overlay policy | Two overlay-policy tests copy and render the same immutable Kustomize overlay independently. | Share one module-scoped raw render and deep-copy parsed data while retaining separate source-binding and topology assertions. |

## Runtime measurements

| Lane | Observation | Variant | Measured UTC | Python | Django / Ray / pytest | Outcomes | Platform | Queue | Environment | Collection | Test execution wall | Setup sum | Call sum | Teardown sum | Post-test/coverage | Terminal rendering |
|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `portable-local` | `windows-local-py312` | `locked-dependencies` | `2026-07-23T00:54:19.939104+00:00` | `3.12.12` | Django 6.0.7; Ray 2.56.0; pytest 9.1.1; xdist not-installed | passed=2869, skipped=38 | Windows-11-10.0.26200-SP0 | not measured | not measured | 6.201s | 114.210s | 10.694s | 97.494s | 3.147s | 2.748s | 0.000s |
| `supported-python` | `github-actions-ubuntu-py312` | `locked-dependencies` | `2026-07-23T01:08:36.369291+00:00` | `3.12.13` | Django 6.0.7; Ray 2.56.0; pytest 9.1.1; xdist not-installed | passed=2907, skipped=33 | Linux-6.17.0-1020-azure-x86_64-with-glibc2.39 | not measured | not measured | 10.016s | 230.827s | 27.195s | 194.231s | 7.372s | 4.116s | 0.000s |

### `portable-local` / `windows-local-py312` / `locked-dependencies` slow paths at `2026-07-23T00:54:19.939104+00:00`

External timing note: Windows local developer path; uv environment was already synchronized; runner queue and dependency setup excluded..

| Slow file | Domain | Phase total |
|---|---|---:|
| `tests/unit/test_workflow_progress_reads.py` | workflow-progress | 17.549s |
| `tests/unit/test_workflow_progress_preparation.py` | workflow-progress | 9.748s |
| `tests/unit/test_workflow_progress_storage_scaling.py` | workflow-progress | 7.737s |
| `tests/unit/test_result_fold.py` | configuration-storage-and-observability | 6.994s |
| `tests/unit/test_workflow_progress_storage_persistence.py` | workflow-progress | 6.418s |
| `tests/unit/test_test_suite_inventory.py` | repository-policy-and-release | 6.245s |
| `tests/unit/test_kuberay_compiled_graph_pilot.py` | compiled-graph-and-kuberay | 5.915s |
| `tests/unit/test_workflow_progress_preparation_prototype.py` | workflow-progress | 5.245s |
| `tests/unit/test_local_kuberay_gate.py` | compiled-graph-and-kuberay | 4.089s |
| `tests/unit/test_workflows.py` | workflow-plan-and-task-runtime | 3.522s |
| `tests/unit/test_workflow_progress_benchmark_command.py` | workflow-progress | 3.342s |
| `tests/unit/test_workflow_progress_storage_boundaries.py` | workflow-progress | 2.822s |
| `tests/unit/test_workflow_progress_storage_audit.py` | workflow-progress | 2.683s |
| `tests/unit/test_kubernetes_web_probes.py` | compiled-graph-and-kuberay | 2.639s |
| `tests/unit/test_workflow_plans.py` | workflow-plan-and-task-runtime | 2.506s |

| Slow test | Domain | Phase total |
|---|---|---:|
| `tests/unit/test_workflow_progress_storage_scaling.py::test_sparse_publication_round_trips_are_independent_of_retained_workflow_size` | workflow-progress | 7.737s |
| `tests/unit/test_result_fold.py::test_large_folds_keep_admission_references_bytes_and_plan_metadata_bounded[50000]` | configuration-storage-and-observability | 3.744s |
| `tests/unit/test_workflow_progress_preparation.py::test_production_topology_subprocess_benchmark_reports_resource_boundary` | workflow-progress | 2.685s |
| `tests/unit/test_workflow_progress_preparation_prototype.py::test_subprocess_benchmark_reports_resources_cardinality_spill_and_cleanup` | workflow-progress | 2.559s |
| `tests/unit/test_workflow_progress_reads.py::test_topology_pages_keep_stable_node_and_edge_order_across_cursors` | workflow-progress | 2.282s |
| `tests/unit/test_test_suite_inventory.py::test_cli_run_records_completed_outcomes_and_merges_valid_timing` | repository-policy-and-release | 2.078s |
| `tests/unit/test_workflow_progress_storage_persistence.py::test_idempotent_publication_batches_more_than_five_hundred_touched_nodes` | workflow-progress | 1.982s |
| `tests/unit/test_workflow_progress_benchmark_command.py::test_command_emits_versioned_safe_deterministic_evidence` | workflow-progress | 1.893s |
| `tests/unit/test_workflows.py::test_bounded_map_keeps_peak_pending_refs_within_window[50000]` | workflow-plan-and-task-runtime | 1.710s |
| `tests/unit/test_result_fold.py::test_retry_rejects_fold_resource_drift_before_leaf_effects` | configuration-storage-and-observability | 1.553s |
| `tests/unit/test_test_suite_inventory.py::test_cli_run_enforces_group_skip_policy` | repository-policy-and-release | 1.460s |
| `tests/unit/test_test_suite_inventory.py::test_cli_select_and_collect_use_fixture_aware_taxonomy_and_atomic_outputs` | repository-policy-and-release | 1.356s |
| `tests/unit/test_workflow_progress_reads.py::test_topology_cursor_positions_are_checked_against_authenticated_pages` | workflow-progress | 1.321s |
| `tests/unit/test_workflow_progress_reads.py::test_summary_pages_and_indexed_node_use_public_bounded_contract` | workflow-progress | 1.210s |
| `tests/unit/test_compiled_graph_probe.py::test_abrupt_root_exit_cleans_real_grandchild` | compiled-graph-and-kuberay | 1.108s |

### `supported-python` / `github-actions-ubuntu-py312` / `locked-dependencies` slow paths at `2026-07-23T01:08:36.369291+00:00`

External timing note: GitHub Actions queue and environment setup are recorded by workflow metadata; this record starts at pytest instrumentation.

| Slow file | Domain | Phase total |
|---|---|---:|
| `tests/unit/test_workflows.py` | workflow-plan-and-task-runtime | 59.770s |
| `tests/unit/test_result_fold.py` | configuration-storage-and-observability | 42.261s |
| `tests/unit/test_result_buffer.py` | configuration-storage-and-observability | 37.609s |
| `tests/unit/test_distributed.py` | worker-and-ray-runtime | 24.546s |
| `tests/integration/test_task_execution.py` | workflow-plan-and-task-runtime | 11.288s |
| `tests/unit/test_workflow_progress_reads.py` | workflow-progress | 10.019s |
| `tests/unit/test_workflow_progress_storage_scaling.py` | workflow-progress | 4.520s |
| `tests/unit/test_workflow_progress_preparation.py` | workflow-progress | 3.952s |
| `tests/unit/test_workflow_progress_storage_persistence.py` | workflow-progress | 3.204s |
| `tests/unit/test_kuberay_compiled_graph_pilot.py` | compiled-graph-and-kuberay | 3.034s |
| `tests/unit/test_kubernetes_web_probes.py` | compiled-graph-and-kuberay | 3.032s |
| `tests/unit/test_workflow_progress_preparation_prototype.py` | workflow-progress | 2.361s |
| `tests/unit/test_test_suite_inventory.py` | repository-policy-and-release | 1.809s |
| `tests/unit/test_compiled_graph_probe.py` | compiled-graph-and-kuberay | 1.674s |
| `tests/unit/test_local_kuberay_gate.py` | compiled-graph-and-kuberay | 1.632s |

| Slow test | Domain | Phase total |
|---|---|---:|
| `tests/unit/test_workflows.py::test_real_ray_mapped_group_cleans_sibling_ref_before_failure_returns` | workflow-plan-and-task-runtime | 10.527s |
| `tests/unit/test_workflows.py::test_real_ray_workflow_persists_graph_and_execution_metadata` | workflow-plan-and-task-runtime | 9.963s |
| `tests/unit/test_result_fold.py::test_real_ray_non_detached_fold_dies_with_owner` | configuration-storage-and-observability | 9.902s |
| `tests/unit/test_result_buffer.py::test_real_ray_non_detached_buffer_dies_with_owner` | configuration-storage-and-observability | 9.844s |
| `tests/unit/test_result_fold.py::test_real_ray_production_summary_stays_out_of_coordinator_until_final_result` | configuration-storage-and-observability | 9.759s |
| `tests/unit/test_workflows.py::test_real_ray_bounded_map_preserves_order_and_limits_concurrency` | workflow-plan-and-task-runtime | 9.725s |
| `tests/unit/test_workflows.py::test_workflow_executes_on_real_ray` | workflow-plan-and-task-runtime | 9.657s |
| `tests/unit/test_result_fold.py::test_real_ray_item_overflow_stops_admission_and_preserves_actor_error` | configuration-storage-and-observability | 9.657s |
| `tests/unit/test_result_buffer.py::test_real_ray_overflow_stops_admission_and_preserves_actor_error` | configuration-storage-and-observability | 9.599s |
| `tests/unit/test_result_buffer.py::test_real_ray_production_payload_stays_out_of_coordinator_until_reducer` | configuration-storage-and-observability | 9.559s |
| `tests/unit/test_distributed.py::TestDistributedWithRay::test_parallel_map_repeated_calls_reuse_cached_remote` | worker-and-ray-runtime | 9.242s |
| `tests/unit/test_workflows.py::test_real_ray_bounded_map_persists_one_aggregate_node` | workflow-plan-and-task-runtime | 9.193s |
| `tests/unit/test_distributed.py::TestDistributedWithRay::test_parallel_map_with_ray` | worker-and-ray-runtime | 9.161s |
| `tests/unit/test_result_fold.py::test_real_ray_exact_resources_runtime_env_direct_return_and_cleanup` | configuration-storage-and-observability | 8.669s |
| `tests/unit/test_result_buffer.py::test_real_ray_actor_resources_direct_returns_and_success_cleanup` | configuration-storage-and-observability | 8.476s |
