"""Wave-3 lane 2: restored SessionDB duplicates stay consolidated.

The September 2026 restore resurrected sibling mixin methods that SessionDB's
class body shadows. Keep the live class-body methods and exactly one owner for
each cross-file helper. These checks use imported runtime objects, not source
text, so they pin behavior rather than formatting.
"""

import importlib
from types import FunctionType

import hermes_state


CONSOLIDATED_MIXINS = {'hermes_state_compression.SessionCompressionMixin': ('_is_compression_child_row',
                                                      '_session_turn_lease_key',
                                                      '_session_turn_lease_key_on_conn',
                                                      'acquire_session_turn_lease',
                                                      'clear_compression_failure_cooldown',
                                                      'finalize_orphaned_compression_sessions',
                                                      'find_live_compression_child',
                                                      'get_compression_chain',
                                                      'get_compression_failure_cooldown',
                                                      'get_compression_failure_cooldown_row',
                                                      'get_compression_fallback_streak',
                                                      'get_compression_ineffective_count',
                                                      'get_compression_lineage',
                                                      'get_compression_lock_holder',
                                                      'get_compression_recovery_deadline',
                                                      'get_compression_tip',
                                                      'publish_compression_child',
                                                      'record_compression_failure_cooldown',
                                                      'refresh_compression_lock',
                                                      'refresh_session_turn_lease',
                                                      'release_compression_lock',
                                                      'release_session_turn_lease',
                                                      'reopen_orphaned_compression_session',
                                                      'restore_compression_failure_cooldown_row',
                                                      'set_compression_fallback_streak',
                                                      'set_compression_ineffective_count',
                                                      'set_compression_recovery_deadline',
                                                      'try_acquire_compression_lock',
                                                      'try_acquire_session_turn_lease'),
 'hermes_state_fts.SessionFtsSetupMixin': ('_ensure_fts_cjk_schema',
                                           '_ensure_fts_schema',
                                           '_enter_fts_fail_open',
                                           '_has_fts_trash',
                                           '_warn_fts5_unavailable',
                                           '_warn_trigram_unavailable'),
 'hermes_state_gateway.SessionGatewayMixin': ('_delete_routing_entries_for_sessions',
                                              '_reap_inactive_orphan_desktop_holders',
                                              'adopt_orphaned_gateway_session',
                                              'claim_handoff',
                                              'clear_backend_heartbeat',
                                              'complete_handoff',
                                              'fail_handoff',
                                              'find_latest_gateway_session_for_peer',
                                              'find_orphaned_gateway_sessions',
                                              'get_handoff_state',
                                              'increment_hygiene_failure_streak',
                                              'list_backend_heartbeats',
                                              'list_gateway_sessions',
                                              'list_never_active_keyed_sessions',
                                              'list_pending_handoffs',
                                              'load_gateway_routing_entries',
                                              'prune_never_active_keyed_sessions',
                                              'prune_stale_heartbeats',
                                              'reclaim_stale_running_handoffs',
                                              'record_gateway_session_peer',
                                              'register_backend_heartbeat',
                                              'replace_gateway_routing_entries',
                                              'request_handoff',
                                              'reset_hygiene_failure_streak',
                                              'save_gateway_routing_entry'),
 'hermes_state_maintenance.SessionMaintenanceMixin': ('_freelist_ratio',
                                                      'archive_stale_sessions',
                                                      'count_open_prune_matches',
                                                      'count_prune_matches',
                                                      'list_prune_candidates',
                                                      'logical_size_bytes',
                                                      'maybe_auto_prune_and_vacuum',
                                                      'prune_empty_ghost_sessions',
                                                      'prune_sessions',
                                                      'sweep_orphaned_sessions',
                                                      'vacuum'),
 'hermes_state_messages.SessionMessagesMixin': ('_bump_conversation_generation',
                                                '_check_transcript_write_guards',
                                                '_dedupe_display_generations',
                                                '_insert_message_rows',
                                                '_is_explicit_fork_child_row',
                                                '_message_column_names',
                                                '_resume_lineage_ids',
                                                '_rows_to_conversation',
                                                'append_message',
                                                'append_messages_batch',
                                                'archive_and_compact',
                                                'assert_resume_safe',
                                                'clear_messages',
                                                'find_pr_url_messages',
                                                'get_active_message_ids',
                                                'get_active_message_watermark',
                                                'get_ancestor_display_prefix',
                                                'get_conversation_root',
                                                'get_message_reactions',
                                                'get_message_role',
                                                'get_messages',
                                                'get_messages_around',
                                                'get_messages_as_conversation',
                                                'get_resume_conversations',
                                                'get_resume_message_count',
                                                'has_archived_messages',
                                                'has_platform_message_id',
                                                'is_explicit_fork_child',
                                                'latest_conversation_boundary',
                                                'latest_message_row_id',
                                                'message_count',
                                                'purge_stale_tool_call_markers',
                                                'replace_messages',
                                                'resolve_resume_session_id',
                                                'rewind_to_message',
                                                'set_latest_matching_message_display_kind',
                                                'set_latest_user_api_content',
                                                'set_message_reaction',
                                                'take_unseen_reactions'),
 'hermes_state_sessions.SessionSessionsMixin': ('_insert_session_row',
                                                '_is_explicit_branch_session',
                                                '_merge_model_config_json',
                                                '_own_profile_name',
                                                '_session_lineage_root_to_tip',
                                                'archive_sessions',
                                                'assert_export_safe',
                                                'backfill_null_session_profiles',
                                                'backfill_repo_roots',
                                                'clear_session_activity_labels',
                                                'count_empty_sessions',
                                                'create_session',
                                                'declared_scope_identity',
                                                'delete_empty_sessions',
                                                'delete_session',
                                                'delete_session_if_empty',
                                                'delete_sessions',
                                                'end_session',
                                                'ensure_session',
                                                'find_session_by_origin',
                                                'get_session',
                                                'get_session_delete_targets',
                                                'get_session_model_config_value',
                                                'list_recent_sessions_bounded',
                                                'list_sessions_rich',
                                                'maybe_auto_archive',
                                                'patch_session_model_config',
                                                'promote_to_session_reset',
                                                'publish_session_git_metadata',
                                                'reopen_session',
                                                'resolve_session_id',
                                                'search_sessions',
                                                'session_count',
                                                'session_count_by_source',
                                                'session_count_ge',
                                                'session_lifecycle_statuses',
                                                'set_expiry_finalized',
                                                'set_session_archived',
                                                'set_session_hidden',
                                                'set_session_pinned',
                                                'set_session_read',
                                                'set_session_yolo',
                                                'touch_session_activity',
                                                'unarchive_recoverable_session',
                                                'update_session_cwd',
                                                'update_session_meta',
                                                'update_session_model',
                                                'update_session_runtime_lock',
                                                'update_session_tool_names',
                                                'update_system_prompt'),
 'hermes_state_telegram.SessionTelegramTopicsMixin': ('apply_telegram_topic_migration',
                                                      'bind_telegram_topic',
                                                      'delete_telegram_topic_binding',
                                                      'disable_telegram_topic_mode',
                                                      'enable_telegram_topic_mode',
                                                      'get_telegram_topic_binding',
                                                      'get_telegram_topic_binding_by_session',
                                                      'is_telegram_session_linked_to_topic',
                                                      'is_telegram_topic_mode_enabled',
                                                      'list_telegram_topic_bindings_for_chat',
                                                      'list_unlinked_telegram_sessions_for_user'),
 'hermes_state_titles.SessionTitlesMixin': ('_is_compression_ancestor',
                                            '_set_session_title',
                                            'get_next_title_in_lineage',
                                            'get_session_by_title',
                                            'get_session_title',
                                            'get_session_title_source',
                                            'resolve_session_by_title',
                                            'set_auto_title',
                                            'set_session_title',
                                            'set_session_title_source'),
 'hermes_state_usage.SessionUsageMixin': ('_apply_token_batch',
                                          '_coalesce_token_deltas',
                                          '_drain_token_queue_at_exit',
                                          '_record_model_usage',
                                          '_stop_token_writer',
                                          '_token_writer_loop',
                                          'flush_token_counts',
                                          'queue_token_counts',
                                          'record_auxiliary_usage',
                                          'update_session_billing_route',
                                          'update_token_counts',
                                          'usage_totals')}

CROSS_FILE_HELPERS = {'_apply_wal_size_limit': ('hermes_state_wal.py', ('hermes_state.py', 'hermes_state_wal.py')),
 '_bump_schema_cookie': ('hermes_state_repair.py', ('hermes_state.py', 'hermes_state_repair.py')),
 '_connect_tracked_db': ('hermes_state.py', ('hermes_state.py', 'hermes_state_dbfile.py')),
 '_cwd_prefix_clause': ('hermes_state_sessions.py', ('hermes_state.py', 'hermes_state_sessions.py')),
 '_existing_malformed_backups': ('hermes_state_repair.py', ('hermes_state.py', 'hermes_state_repair.py')),
 '_is_ephemeral_port_zero_backend': ('hermes_state_dbfile.py',
                                     ('hermes_cli/dashboard_procs.py', 'hermes_state_dbfile.py')),
 '_mask_volatile_header': ('hermes_state_repair.py', ('hermes_state.py', 'hermes_state_repair.py')),
 '_normalize_telegram_topic_profile_name': ('hermes_state_telegram.py',
                                            ('hermes_state.py', 'hermes_state_telegram.py')),
 '_read_budget_for': ('hermes_state.py', ('hermes_state.py', 'hermes_state_readpool.py')),
 '_read_budget_key': ('hermes_state.py', ('hermes_state.py', 'hermes_state_readpool.py')),
 '_running_under_pytest': ('hermes_state_guard.py', ('hermes_state.py', 'hermes_state_guard.py')),
 '_set_last_init_error': ('hermes_state.py', ('hermes_state.py', 'hermes_state_guard.py')),
 '_workspace_key_clause': ('hermes_state_sessions.py', ('hermes_state.py', 'hermes_state_sessions.py'))}


def _module(path):
    return importlib.import_module(path[:-3].replace("/", "."))


def test_shadowed_mixin_methods_stay_deleted():
    for class_path, names in CONSOLIDATED_MIXINS.items():
        module_path, class_name = class_path.rsplit(".", 1)
        mixin = getattr(importlib.import_module(module_path), class_name)
        resurrected = sorted(name for name in names if name in vars(mixin))
        assert not resurrected, f"{class_path} re-defines shadowed methods: {resurrected}"


def test_sessiondb_still_binds_every_consolidated_method():
    for class_path, names in CONSOLIDATED_MIXINS.items():
        for name in names:
            assert name in vars(hermes_state.SessionDB), f"{class_path}:{name} left SessionDB"
            assert callable(getattr(hermes_state.SessionDB, name)), f"{class_path}:{name} is not callable"


def test_cross_file_helpers_have_one_defining_module():
    for name, (owner_path, site_paths) in CROSS_FILE_HELPERS.items():
        owner = _module(owner_path)
        owner_fn = vars(owner).get(name)
        assert isinstance(owner_fn, FunctionType), f"{owner_path}:{name} is not a module function"
        assert owner_fn.__module__ == owner.__name__
        for site_path in site_paths:
            site = _module(site_path)
            value = vars(site).get(name)
            assert value is None or value is owner_fn, (
                f"{site_path}:{name} diverged from its single owner {owner_path}"
            )


def test_cross_file_compatibility_imports_resolve_to_owner():
    # dbfile.collect_state_db_stats late-imports _connect_tracked_db from the
    # facade to avoid a module-import cycle. The guard has no _set_last_init_error
    # callers; leaving that name unbound keeps it independent of the facade,
    # as required by the facade/sibling one-way import rule.
    unbound_sites = {
        ("_connect_tracked_db", "hermes_state_dbfile.py"),
        ("_set_last_init_error", "hermes_state_guard.py"),
    }
    for name, (owner_path, site_paths) in CROSS_FILE_HELPERS.items():
        owner_fn = vars(_module(owner_path))[name]
        for site_path in site_paths:
            if (name, site_path) in unbound_sites:
                continue
            assert vars(_module(site_path)).get(name) is owner_fn, f"{site_path}:{name} is not re-exported"
