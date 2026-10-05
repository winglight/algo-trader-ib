-- Schema initialization for local algo-trader runtime services.
--
-- This script provisions the MariaDB tables required by the local main
-- application and runtime services.

CREATE TABLE IF NOT EXISTS config (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    config_key VARCHAR(191) NOT NULL,
    config_value LONGTEXT NULL,
    description TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_config_config_key UNIQUE KEY (config_key)
);

-- Database-backed Broker Runner adapter catalog and account profiles.  Adapter
-- software installation is still owned by the reviewed installer/update flow;
-- these tables never carry package names, entrypoints, images, commands, or
-- host paths supplied by the web API.
CREATE TABLE IF NOT EXISTS broker_adapter_types (
    adapter_type VARCHAR(64) NOT NULL PRIMARY KEY,
    display_name VARCHAR(191) NOT NULL,
    installed_version VARCHAR(64) NOT NULL,
    protocol_version VARCHAR(64) NOT NULL,
    management_mode VARCHAR(32) NOT NULL,
    release_tier VARCHAR(32) NOT NULL,
    config_schema_version VARCHAR(64) NOT NULL,
    config_schema_json JSON NOT NULL,
    ui_schema_json JSON NOT NULL,
    capabilities_json JSON NOT NULL,
    available TINYINT(1) NOT NULL DEFAULT 0,
    installed_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    KEY idx_broker_adapter_types_available (available, release_tier)
);

CREATE TABLE IF NOT EXISTS broker_adapter_profiles (
    id CHAR(36) NOT NULL PRIMARY KEY,
    profile_id VARCHAR(64) NOT NULL,
    adapter_type VARCHAR(64) NOT NULL,
    display_name VARCHAR(191) NOT NULL,
    environment VARCHAR(32) NOT NULL,
    management_mode VARCHAR(32) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    lifecycle_status VARCHAR(32) NOT NULL,
    configuration_status VARCHAR(32) NOT NULL,
    diagnostic_codes_json JSON NOT NULL,
    config_schema_version VARCHAR(64) NOT NULL,
    config_json JSON NOT NULL,
    config_fingerprint CHAR(64) NOT NULL,
    current_broker_account_id CHAR(36) NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    created_by VARCHAR(191) NOT NULL,
    updated_by VARCHAR(191) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    deleted_at DATETIME(6) NULL,
    -- Written by the service on create/delete. MariaDB 11.4 rejects the old
    -- generated expression because it references adapter_type in this table.
    singleton_adapter_type VARCHAR(64) NULL,
    CONSTRAINT uq_broker_adapter_profiles_profile_id UNIQUE KEY (profile_id),
    CONSTRAINT uq_broker_adapter_profiles_singleton UNIQUE KEY (singleton_adapter_type),
    KEY idx_broker_adapter_profiles_type_status (adapter_type, lifecycle_status, enabled),
    CONSTRAINT fk_broker_adapter_profiles_type FOREIGN KEY (adapter_type)
        REFERENCES broker_adapter_types(adapter_type) ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS broker_adapter_profile_secrets (
    profile_id CHAR(36) NOT NULL PRIMARY KEY,
    ciphertext LONGBLOB NOT NULL,
    nonce VARBINARY(32) NOT NULL,
    wrapped_data_key BLOB NOT NULL,
    kek_version VARCHAR(64) NOT NULL,
    secret_fields_json JSON NOT NULL,
    updated_by VARCHAR(191) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_broker_adapter_profile_secrets_profile FOREIGN KEY (profile_id)
        REFERENCES broker_adapter_profiles(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    id CHAR(36) NOT NULL PRIMARY KEY,
    profile_id CHAR(36) NOT NULL,
    broker_account_ref_enc BLOB NULL,
    broker_account_ref_hash CHAR(64) NULL,
    broker_account_masked VARCHAR(64) NULL,
    display_name VARCHAR(191) NULL,
    base_currency VARCHAR(16) NULL,
    status VARCHAR(32) NOT NULL,
    first_verified_at DATETIME(6) NULL,
    last_verified_at DATETIME(6) NULL,
    last_snapshot_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    deleted_at DATETIME(6) NULL,
    CONSTRAINT uq_broker_accounts_profile_ref UNIQUE KEY (profile_id, broker_account_ref_hash),
    KEY idx_broker_accounts_profile_status (profile_id, status),
    CONSTRAINT fk_broker_accounts_profile FOREIGN KEY (profile_id)
        REFERENCES broker_adapter_profiles(id) ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS broker_account_fund_state (
    broker_account_id CHAR(36) NOT NULL PRIMARY KEY,
    equity DECIMAL(28,8) NOT NULL,
    cash_balance DECIMAL(28,8) NULL,
    buying_power DECIMAL(28,8) NULL,
    available_funds DECIMAL(28,8) NULL,
    maintenance_margin DECIMAL(28,8) NULL,
    initial_margin DECIMAL(28,8) NULL,
    unrealized_pnl DECIMAL(28,8) NULL,
    broker_daily_pnl DECIMAL(28,8) NULL,
    currency VARCHAR(16) NOT NULL,
    equity_source VARCHAR(32) NOT NULL,
    broker_observed_at DATETIME(6) NOT NULL,
    received_at DATETIME(6) NOT NULL,
    source_revision BIGINT NOT NULL,
    quality VARCHAR(32) NOT NULL,
    raw_schema_version VARCHAR(64) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_broker_account_fund_state_account FOREIGN KEY (broker_account_id)
        REFERENCES broker_accounts(id) ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS broker_account_equity_snapshots (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    broker_account_id CHAR(36) NOT NULL,
    equity DECIMAL(28,8) NOT NULL,
    cash_balance DECIMAL(28,8) NULL,
    buying_power DECIMAL(28,8) NULL,
    available_funds DECIMAL(28,8) NULL,
    maintenance_margin DECIMAL(28,8) NULL,
    initial_margin DECIMAL(28,8) NULL,
    unrealized_pnl DECIMAL(28,8) NULL,
    broker_daily_pnl DECIMAL(28,8) NULL,
    net_cash_flow DECIMAL(28,8) NULL,
    currency VARCHAR(16) NOT NULL,
    equity_source VARCHAR(32) NOT NULL,
    broker_observed_at DATETIME(6) NOT NULL,
    received_at DATETIME(6) NOT NULL,
    source_revision BIGINT NOT NULL,
    quality VARCHAR(32) NOT NULL,
    raw_schema_version VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT uq_broker_account_equity_revision UNIQUE KEY (broker_account_id, source_revision),
    KEY idx_broker_account_equity_observed (broker_account_id, broker_observed_at),
    CONSTRAINT fk_broker_account_equity_account FOREIGN KEY (broker_account_id)
        REFERENCES broker_accounts(id) ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS broker_account_equity_daily (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    broker_account_id CHAR(36) NOT NULL,
    trading_date_ny DATE NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    opening_equity DECIMAL(28,8) NOT NULL,
    peak_equity DECIMAL(28,8) NOT NULL,
    low_equity DECIMAL(28,8) NOT NULL,
    latest_equity DECIMAL(28,8) NOT NULL,
    closing_equity DECIMAL(28,8) NULL,
    net_cash_flow DECIMAL(28,8) NULL,
    adjusted_equity_change DECIMAL(28,8) NULL,
    actual_daily_loss DECIMAL(28,8) NULL,
    broker_daily_pnl DECIMAL(28,8) NULL,
    currency VARCHAR(16) NOT NULL,
    equity_source VARCHAR(32) NOT NULL,
    metric_quality VARCHAR(32) NOT NULL,
    first_observed_at DATETIME(6) NOT NULL,
    last_observed_at DATETIME(6) NOT NULL,
    sample_count INT NOT NULL,
    source_revision BIGINT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT uq_broker_account_equity_daily UNIQUE KEY (broker_account_id, trading_date_ny),
    CONSTRAINT fk_broker_account_equity_daily_account FOREIGN KEY (broker_account_id)
        REFERENCES broker_accounts(id) ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS broker_profile_operations (
    operation_id CHAR(36) NOT NULL PRIMARY KEY,
    operation_type VARCHAR(32) NOT NULL,
    profile_id CHAR(36) NULL,
    status VARCHAR(32) NOT NULL,
    step VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    actor VARCHAR(191) NOT NULL,
    correlation_id VARCHAR(191) NULL,
    previous_revision BIGINT NULL,
    target_revision BIGINT NULL,
    error_code VARCHAR(128) NULL,
    secret_free_error_message TEXT NULL,
    recovery_json JSON NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6) NULL,
    CONSTRAINT uq_broker_profile_operations_idempotency UNIQUE KEY (idempotency_key),
    KEY idx_broker_profile_operations_profile (profile_id, created_at),
    CONSTRAINT fk_broker_profile_operations_profile FOREIGN KEY (profile_id)
        REFERENCES broker_adapter_profiles(id) ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS strategy_packages (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    package_key VARCHAR(191) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT NULL,
    schema_version VARCHAR(32) NOT NULL DEFAULT 'spec.v2',
    package_type VARCHAR(64) NOT NULL DEFAULT 'STRATEGY_DEFINITION_PACKAGE',
    lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'published',
    source_type VARCHAR(32) NOT NULL DEFAULT 'import',
    current_draft_version_id BIGINT UNSIGNED NULL,
    latest_published_version_id BIGINT UNSIGNED NULL,
    metadata_json LONGTEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_strategy_packages_package_key UNIQUE KEY (package_key)
);

CREATE TABLE IF NOT EXISTS strategy_package_versions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    package_id BIGINT UNSIGNED NOT NULL,
    version_no INT NOT NULL,
    version_label VARCHAR(64) NOT NULL,
    state VARCHAR(32) NOT NULL DEFAULT 'published',
    schema_version VARCHAR(32) NOT NULL DEFAULT 'spec.v2',
    package_manifest_json LONGTEXT NOT NULL,
    normalized_spec_json LONGTEXT NOT NULL,
    compiled_spec_json LONGTEXT NULL,
    compile_diagnostics_json LONGTEXT NULL,
    simulation_summary_json LONGTEXT NULL,
    compatibility_reports_json LONGTEXT NULL,
    release_metadata_json LONGTEXT NOT NULL,
    ui_layout_json LONGTEXT NULL,
    bundle_storage_mode VARCHAR(32) NOT NULL DEFAULT 'plaintext',
    bundle_encryption_policy_id VARCHAR(191) NULL,
    encrypted_bundle_json LONGTEXT NULL,
    encrypted_bundle_ref TEXT NULL,
    bundle_ciphertext_hash VARCHAR(128) NULL,
    bundle_plaintext_hash VARCHAR(128) NULL,
    spec_hash CHAR(64) NOT NULL,
    compiler_version VARCHAR(64) NULL,
    import_source VARCHAR(32) NOT NULL DEFAULT 'manual_upload',
    import_bundle_hash CHAR(64) NOT NULL,
    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    imported_by VARCHAR(191) NULL,
    remote_package_key VARCHAR(191) NULL,
    remote_version_label VARCHAR(64) NULL,
    remote_published_at DATETIME NULL,
    update_channel VARCHAR(64) NULL,
    installed_from_release_uri TEXT NULL,
    import_diagnostics_json LONGTEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_strategy_package_versions_package_version UNIQUE KEY (package_id, version_no),
    CONSTRAINT uq_strategy_package_versions_package_label UNIQUE KEY (package_id, version_label)
);


CREATE TABLE IF NOT EXISTS strategy_deployments (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    package_id BIGINT UNSIGNED NOT NULL,
    version_id BIGINT UNSIGNED NOT NULL,
    runtime_profile_key VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    symbol_override VARCHAR(64) NULL,
    runtime_override_json LONGTEXT NULL,
    compatibility_snapshot_json LONGTEXT NULL,
    deployment_validation_json LONGTEXT NULL,
    started_at DATETIME NULL,
    stopped_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    run_id VARCHAR(64) NULL,
    runtime_state_json LONGTEXT NULL,
    subscription_bindings_json LONGTEXT NULL,
    last_heartbeat_at DATETIME NULL,
    deployment_version INT NOT NULL DEFAULT 1,
    KEY idx_strategy_deployments_status_updated (status, updated_at, id)
);

CREATE TABLE IF NOT EXISTS position_groups (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    group_key VARCHAR(64) NOT NULL,
    package_id BIGINT UNSIGNED NOT NULL,
    version_id BIGINT UNSIGNED NOT NULL,
    deployment_id BIGINT UNSIGNED NOT NULL,
    asset_family VARCHAR(32) NOT NULL DEFAULT 'NON_OPTION',
    underlying_symbol VARCHAR(64) NOT NULL,
    instrument_type VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN',
    grouping_mode VARCHAR(32) NOT NULL DEFAULT 'STRATEGY_SYMBOL',
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    entry_order_id BIGINT UNSIGNED NULL,
    exit_order_id BIGINT UNSIGNED NULL,
    net_quantity DECIMAL(24,8) NULL,
    avg_entry_price DECIMAL(24,8) NULL,
    avg_exit_price DECIMAL(24,8) NULL,
    realized_pnl DECIMAL(24,8) NULL,
    unrealized_pnl DECIMAL(24,8) NULL,
    metadata_json LONGTEXT NULL,
    opened_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_position_groups_group_key UNIQUE KEY (group_key),
    KEY idx_position_groups_deployment_updated (deployment_id, updated_at, id)
);

CREATE TABLE IF NOT EXISTS spec_runtime_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    package_id BIGINT UNSIGNED NULL,
    version_id BIGINT UNSIGNED NULL,
    deployment_id BIGINT UNSIGNED NULL,
    event_scope VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    event_name VARCHAR(128) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'info',
    payload_json LONGTEXT NULL,
    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    simulation_run_id BIGINT UNSIGNED NULL,
    run_id VARCHAR(64) NULL,
    trace_type VARCHAR(64) NULL,
    trace_id VARCHAR(191) NULL,
    node_id VARCHAR(128) NULL,
    order_id BIGINT UNSIGNED NULL,
    position_group_id BIGINT UNSIGNED NULL,
    correlation_id VARCHAR(191) NULL,
    INDEX idx_spec_runtime_events_occurred_id (occurred_at, id),
    KEY idx_spec_runtime_events_trace_event (trace_type, trace_id, event_name),
    KEY idx_spec_runtime_events_deployment_type_occurred (deployment_id, event_type, occurred_at, id)
);


CREATE TABLE IF NOT EXISTS orders (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    command_id VARCHAR(191) NULL,
    client_order_id VARCHAR(191) NULL,
    adapter_id VARCHAR(64) NOT NULL,
    adapter_order_id VARCHAR(191) NULL,
    adapter_order_ref VARCHAR(191) NULL,
    adapter_metadata JSON NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    action VARCHAR(64) NULL,
    side VARCHAR(16) NULL,
    quantity DOUBLE NULL,
    order_type VARCHAR(32) NULL,
    price DOUBLE NULL,
    limit_price DOUBLE NULL,
    stop_price DOUBLE NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'Submitted',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    executed_at DATETIME NULL,
    order_timestamp DATETIME NULL,
    fill_price DOUBLE NULL,
    filled_quantity DOUBLE NULL,
    remaining_quantity DOUBLE NULL,
    avg_fill_price DOUBLE NULL,
    order_source VARCHAR(32) NULL,
    strategy VARCHAR(191) NULL,
    strategy_name VARCHAR(191) NULL,
    metrics_owner_id VARCHAR(191) NULL,
    rule_id VARCHAR(191) NULL,
    position_effect VARCHAR(16) NULL,
    parent_order_id VARCHAR(191) NULL,
    exchange VARCHAR(64) NULL,
    sec_type VARCHAR(64) NULL,
    last_trade_date VARCHAR(32) NULL,
    strike DOUBLE NULL,
    option_right VARCHAR(8) NULL,
    underlying_symbol VARCHAR(64) NULL,
    primary_exchange VARCHAR(64) NULL,
    local_symbol VARCHAR(191) NULL,
    trading_class VARCHAR(64) NULL,
    contract_fingerprint VARCHAR(191) NULL,
    position_side VARCHAR(16) NULL,
    position_opened_at DATETIME NULL,
    entry_order_links LONGTEXT NULL,
    entry_strategy_ids LONGTEXT NULL,
    entry_strategy_names LONGTEXT NULL,
    notes TEXT NULL,
    commission DOUBLE NULL,
    pnl DOUBLE NULL,
    realized_pnl DOUBLE NULL,
    unrealized_pnl DOUBLE NULL,
    rejection_reason TEXT NULL,
    invoker VARCHAR(191) NULL,
    invoker_type VARCHAR(191) NULL,
    invoker_status VARCHAR(191) NULL,
    invoker_price DOUBLE NULL,
    account VARCHAR(191) NULL,
    source VARCHAR(191) NULL,
    raw_payload JSON NULL,
    pricing_trace_json LONGTEXT NULL,
    workflow_id VARCHAR(191) NULL,
    spec_hash VARCHAR(191) NULL,
    decision_trace_id VARCHAR(191) NULL,
    sim_run_id VARCHAR(191) NULL,
    exec_plan_id VARCHAR(191) NULL,
    package_id BIGINT UNSIGNED NULL,
    package_version_id BIGINT UNSIGNED NULL,
    deployment_id BIGINT UNSIGNED NULL,
    position_group_id BIGINT UNSIGNED NULL,
    order_spec_id VARCHAR(128) NULL,
    signal_node_id VARCHAR(128) NULL,
    ee_rule_id VARCHAR(128) NULL,
    intent_type VARCHAR(64) NULL,
    intent_id VARCHAR(128) NULL,
    correlation_id VARCHAR(191) NULL,
    pricing_policy VARCHAR(64) NULL,
    is_deleted TINYINT(1) NOT NULL DEFAULT 0,
    CONSTRAINT uq_orders_adapter_ref UNIQUE KEY (adapter_id, adapter_order_ref),
    CONSTRAINT uq_orders_adapter_order UNIQUE KEY (adapter_id, adapter_order_id),
    KEY idx_orders_adapter_created (adapter_id, created_at, id)
);


SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_symbol_status'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_symbol_status ON orders (symbol, status)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_strategy'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_strategy ON orders (strategy)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_metrics_owner'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_metrics_owner ON orders (metrics_owner_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_created_at'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_created_at ON orders (created_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_pnl_calendar_window'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_pnl_calendar_window ON orders (is_deleted, created_at, parent_order_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_pnl_calendar_executed_window'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_pnl_calendar_executed_window ON orders (is_deleted, executed_at, parent_order_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_pnl_calendar_updated_window'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_pnl_calendar_updated_window ON orders (is_deleted, updated_at, parent_order_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_decision_trace_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_decision_trace_id ON orders (decision_trace_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_sim_run_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_sim_run_id ON orders (sim_run_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_exec_plan_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_orders_exec_plan_id ON orders (exec_plan_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

DELIMITER //
CREATE PROCEDURE backfill_orders_metrics_owner()
BEGIN
    DECLARE raw_payload_exists INT DEFAULT 0;
    DECLARE metrics_owner_exists INT DEFAULT 0;
    DECLARE decision_trace_exists INT DEFAULT 0;
    DECLARE sim_run_exists INT DEFAULT 0;
    DECLARE exec_plan_exists INT DEFAULT 0;

    SELECT COUNT(*) INTO raw_payload_exists
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND COLUMN_NAME = 'raw_payload';

    SELECT COUNT(*) INTO metrics_owner_exists
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND COLUMN_NAME = 'metrics_owner_id';

    SELECT COUNT(*) INTO decision_trace_exists
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND COLUMN_NAME = 'decision_trace_id';

    SELECT COUNT(*) INTO sim_run_exists
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND COLUMN_NAME = 'sim_run_id';

    SELECT COUNT(*) INTO exec_plan_exists
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND COLUMN_NAME = 'exec_plan_id';

    IF raw_payload_exists > 0 AND metrics_owner_exists > 0 THEN
        UPDATE orders
        SET metrics_owner_id = NULLIF(
                RTRIM(LTRIM(REPLACE(JSON_EXTRACT(raw_payload, '$.metrics_owner_id'), '"', ''))),
                ''
            )
        WHERE metrics_owner_id IS NULL
          AND raw_payload IS NOT NULL
          AND JSON_EXTRACT(raw_payload, '$.metrics_owner_id') IS NOT NULL;

        UPDATE orders
        SET metrics_owner_id = NULLIF(
                RTRIM(LTRIM(REPLACE(JSON_EXTRACT(raw_payload, '$.metadata.metrics_owner_id'), '"', ''))),
                ''
            )
        WHERE metrics_owner_id IS NULL
          AND raw_payload IS NOT NULL
          AND JSON_EXTRACT(raw_payload, '$.metadata.metrics_owner_id') IS NOT NULL;
    END IF;

    IF raw_payload_exists > 0 AND decision_trace_exists > 0 THEN
        UPDATE orders
        SET decision_trace_id = NULLIF(
                RTRIM(LTRIM(REPLACE(JSON_EXTRACT(raw_payload, '$.decision_trace_id'), '"', ''))),
                ''
            )
        WHERE decision_trace_id IS NULL
          AND raw_payload IS NOT NULL
          AND JSON_EXTRACT(raw_payload, '$.decision_trace_id') IS NOT NULL;
    END IF;

    IF raw_payload_exists > 0 AND sim_run_exists > 0 THEN
        UPDATE orders
        SET sim_run_id = NULLIF(
                RTRIM(LTRIM(REPLACE(JSON_EXTRACT(raw_payload, '$.sim_run_id'), '"', ''))),
                ''
            )
        WHERE sim_run_id IS NULL
          AND raw_payload IS NOT NULL
          AND JSON_EXTRACT(raw_payload, '$.sim_run_id') IS NOT NULL;
    END IF;

    IF raw_payload_exists > 0 AND exec_plan_exists > 0 THEN
        UPDATE orders
        SET exec_plan_id = NULLIF(
                RTRIM(LTRIM(REPLACE(JSON_EXTRACT(raw_payload, '$.exec_plan_id'), '"', ''))),
                ''
            )
        WHERE exec_plan_id IS NULL
          AND raw_payload IS NOT NULL
          AND JSON_EXTRACT(raw_payload, '$.exec_plan_id') IS NOT NULL;
    END IF;
END//
DELIMITER ;
CALL backfill_orders_metrics_owner();
DROP PROCEDURE backfill_orders_metrics_owner;

CREATE TABLE IF NOT EXISTS order_fills (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id BIGINT UNSIGNED NOT NULL,
    adapter_id VARCHAR(64) NOT NULL,
    adapter_execution_id VARCHAR(191) NOT NULL,
    adapter_metadata JSON NOT NULL,
    fill_time DATETIME NOT NULL,
    quantity DOUBLE NULL,
    price DOUBLE NULL,
    commission DOUBLE NULL,
    realized_pnl DOUBLE NULL,
    currency VARCHAR(16) NULL,
    commission_currency VARCHAR(16) NULL,
    pnl_currency VARCHAR(16) NULL,
    price_multiplier DOUBLE NULL,
    source_hash VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_order_fills_adapter_exec (adapter_id, adapter_execution_id),
    KEY idx_order_fills_order_id (order_id),
    CONSTRAINT fk_order_fills_orders FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS trade_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT UNSIGNED NULL,
    account_id BIGINT UNSIGNED NULL,
    `date` DATE NOT NULL,
    `type` VARCHAR(16) NOT NULL,
    trades_count INT NULL,
    overall_feeling TEXT NULL,
    fact_record TEXT NULL,
    learning_points TEXT NULL,
    improvement_direction TEXT NULL,
    self_affirmation TEXT NULL,
    associated_trades JSON NULL,
    weekly_total_trades INT NULL,
    weekly_pnl_result DOUBLE NULL,
    weekly_max_win DOUBLE NULL,
    weekly_max_loss DOUBLE NULL,
    weekly_win_rate DOUBLE NULL,
    follows_daily_limit TINYINT(1) NULL,
    success_planned_trades JSON NULL,
    mistake_violated_plans JSON NULL,
    mistake_emotional_factors JSON NULL,
    next_good_habit TEXT NULL,
    next_mistake_to_avoid TEXT NULL,
    next_specific_actions TEXT NULL,
    weekly_affirmation TEXT NULL,
    system_strategy_improvement TEXT NULL,
    llm_log_id VARCHAR(64) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'trade_logs'
      AND INDEX_NAME = 'idx_trade_logs_date_type'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_trade_logs_date_type ON trade_logs (`date`, `type`)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'trade_logs'
      AND INDEX_NAME = 'idx_trade_logs_user_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_trade_logs_user_id ON trade_logs (user_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'trade_logs'
      AND INDEX_NAME = 'idx_trade_logs_account_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_trade_logs_account_id ON trade_logs (account_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


CREATE TABLE IF NOT EXISTS risk_rules (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(191) NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    position_limit JSON NULL,
    loss_limit JSON NULL,
    stop_loss_offset DOUBLE NULL DEFAULT NULL,
    take_profit_offset DOUBLE NULL DEFAULT NULL,
    stop_loss_price DOUBLE NULL DEFAULT NULL,
    take_profit_price DOUBLE NULL DEFAULT NULL,
    max_loss_percent DOUBLE NULL DEFAULT NULL,
    max_time_span INT NULL DEFAULT NULL,
    trailing_stop JSON NULL DEFAULT NULL,
    auto_trailing TINYINT(1) NOT NULL DEFAULT 0,
    atr_params JSON NULL DEFAULT NULL,
    atr_day_percentage DOUBLE NULL DEFAULT NULL,
    session_decay DOUBLE NULL DEFAULT NULL,
    rule_type VARCHAR(64) NOT NULL DEFAULT 'fixed',
    min_adx DOUBLE NULL DEFAULT NULL,
    max_adx DOUBLE NULL DEFAULT NULL,
    min_atr_ratio DOUBLE NULL DEFAULT NULL,
    max_atr_ratio DOUBLE NULL DEFAULT NULL,
    max_bullish_fractals DOUBLE NULL DEFAULT NULL,
    max_bearish_fractals DOUBLE NULL DEFAULT NULL,
    notes TEXT NULL DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'risk_rules'
      AND INDEX_NAME = 'idx_risk_rules_symbol_rule_type'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_risk_rules_symbol_rule_type ON risk_rules (symbol, rule_type)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'risk_rules'
      AND INDEX_NAME = 'idx_risk_rules_enabled'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_risk_rules_enabled ON risk_rules (enabled)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS notifications (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    message TEXT NOT NULL,
    level VARCHAR(32) NOT NULL DEFAULT 'info',
    category VARCHAR(64) NOT NULL DEFAULT 'general',
    title VARCHAR(191) NULL,
    metadata JSON NULL,
    is_read TINYINT(1) NOT NULL DEFAULT 0,
    read_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'notifications'
      AND INDEX_NAME = 'idx_notifications_created_at'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_notifications_created_at ON notifications (created_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'notifications'
      AND INDEX_NAME = 'idx_notifications_is_read'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_notifications_is_read ON notifications (is_read)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS watchlist_groups (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(191) NOT NULL,
    group_type VARCHAR(32) NOT NULL DEFAULT 'manual',
    strategy_ref_id BIGINT UNSIGNED NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'watchlist_groups'
      AND INDEX_NAME = 'idx_watchlist_groups_sort'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_watchlist_groups_sort ON watchlist_groups (sort_order, id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'watchlist_groups'
      AND INDEX_NAME = 'idx_watchlist_groups_type'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_watchlist_groups_type ON watchlist_groups (group_type)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS watchlist_items (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    group_ref_id BIGINT UNSIGNED NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_watchlist_items_group_symbol UNIQUE KEY (group_ref_id, symbol),
    CONSTRAINT fk_watchlist_items_group FOREIGN KEY (group_ref_id)
        REFERENCES watchlist_groups (id)
        ON DELETE CASCADE
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'watchlist_items'
      AND INDEX_NAME = 'idx_watchlist_items_group_sort'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_watchlist_items_group_sort ON watchlist_items (group_ref_id, sort_order, id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL,
    event_time DATETIME NOT NULL,
    event_name VARCHAR(191) NOT NULL,
    service VARCHAR(64) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    level VARCHAR(16) NOT NULL DEFAULT 'info',
    correlation_id VARCHAR(128) NULL,
    strategy_id VARCHAR(128) NULL,
    symbols_json JSON NULL,
    client_id VARCHAR(128) NULL,
    session_id VARCHAR(128) NULL,
    params_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_audit_events_event_id UNIQUE KEY (event_id)
);

CREATE TABLE IF NOT EXISTS audit_rollups_daily (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    rollup_date DATE NOT NULL,
    service VARCHAR(64) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    strategy_id VARCHAR(128) NULL,
    total_events INT NOT NULL DEFAULT 0,
    error_events INT NOT NULL DEFAULT 0,
    warn_events INT NOT NULL DEFAULT 0,
    conversion_rate DOUBLE NULL,
    metrics_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_issues (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    issue_key VARCHAR(191) NOT NULL,
    issue_type VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL DEFAULT 'warn',
    stage VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    title VARCHAR(255) NOT NULL,
    description TEXT NULL,
    strategy_id VARCHAR(128) NULL,
    correlation_id VARCHAR(128) NULL,
    payload_json JSON NULL,
    detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_audit_issues_issue_key UNIQUE KEY (issue_key)
);

CREATE TABLE IF NOT EXISTS audit_config_changes (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    service VARCHAR(64) NOT NULL,
    config_key VARCHAR(191) NOT NULL,
    old_value_json JSON NULL,
    new_value_json JSON NULL,
    changed_by VARCHAR(128) NULL,
    changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    correlation_id VARCHAR(128) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS spec_runtime_backtest_baselines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    baseline_id VARCHAR(191) NOT NULL,
    deployment_id BIGINT UNSIGNED NULL,
    package_id VARCHAR(191) NULL,
    version_id VARCHAR(191) NULL,
    workflow_id VARCHAR(191) NULL,
    backtest_run_id VARCHAR(191) NULL,
    source_id VARCHAR(191) NULL,
    baseline_json LONGTEXT NOT NULL,
    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_backtest_baselines_baseline_id UNIQUE KEY (baseline_id),
    KEY idx_spec_runtime_backtest_baselines_deployment_id (deployment_id),
    KEY idx_spec_runtime_backtest_baselines_package_id (package_id)
);

CREATE TABLE IF NOT EXISTS spec_runtime_daily_performance (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    performance_id VARCHAR(191) NOT NULL,
    deployment_id BIGINT UNSIGNED NOT NULL,
    package_id VARCHAR(191) NULL,
    version_id VARCHAR(191) NULL,
    trade_date DATE NOT NULL,
    realized_pnl DOUBLE NOT NULL DEFAULT 0,
    unrealized_pnl DOUBLE NOT NULL DEFAULT 0,
    total_pnl DOUBLE NOT NULL DEFAULT 0,
    trade_count INT NOT NULL DEFAULT 0,
    win_rate DOUBLE NULL,
    average_win DOUBLE NULL,
    average_loss DOUBLE NULL,
    expectancy DOUBLE NULL,
    profit_factor DOUBLE NULL,
    max_drawdown DOUBLE NULL,
    consecutive_losses INT NULL,
    average_slippage DOUBLE NULL,
    order_rejections INT NOT NULL DEFAULT 0,
    kill_switch_events INT NOT NULL DEFAULT 0,
    metrics_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_daily_performance_id UNIQUE KEY (performance_id),
    CONSTRAINT uq_spec_runtime_daily_performance_deployment_date UNIQUE KEY (deployment_id, trade_date),
    KEY idx_spec_runtime_daily_performance_date (trade_date)
);

CREATE TABLE IF NOT EXISTS spec_runtime_performance_reports (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    report_id VARCHAR(191) NOT NULL,
    deployment_id BIGINT UNSIGNED NULL,
    package_id VARCHAR(191) NULL,
    version_id VARCHAR(191) NULL,
    period_type VARCHAR(32) NOT NULL,
    period_start DATETIME NOT NULL,
    period_end DATETIME NOT NULL,
    recommendation VARCHAR(64) NOT NULL,
    drift_type VARCHAR(64) NULL,
    llm_log_id VARCHAR(191) NULL,
    report_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_performance_reports_report_id UNIQUE KEY (report_id),
    KEY idx_spec_runtime_performance_reports_deployment_period (deployment_id, period_type, period_start),
    KEY idx_spec_runtime_performance_reports_package_period (package_id, period_type, period_start),
    KEY idx_spec_runtime_performance_reports_created_at (created_at)
);

CREATE TABLE IF NOT EXISTS spec_runtime_period_reports (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    report_id VARCHAR(191) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    period_type VARCHAR(32) NOT NULL,
    period_start DATETIME NOT NULL,
    period_end DATETIME NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    data_status VARCHAR(32) NOT NULL,
    trade_count INT NOT NULL DEFAULT 0,
    strategy_count INT NOT NULL DEFAULT 0,
    net_pnl DOUBLE NOT NULL DEFAULT 0,
    final_recommendation VARCHAR(64) NULL,
    llm_status VARCHAR(64) NULL,
    llm_log_id VARCHAR(191) NULL,
    report_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_period_reports_report_id UNIQUE KEY (report_id),
    KEY idx_spec_runtime_period_reports_period (period_type, period_start, period_end),
    KEY idx_spec_runtime_period_reports_created_at (created_at),
    KEY idx_spec_runtime_period_reports_status (status, data_status)
);

CREATE TABLE IF NOT EXISTS spec_runtime_report_task_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id VARCHAR(191) NOT NULL,
    trigger_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at DATETIME NOT NULL,
    completed_at DATETIME NULL,
    generated_count INT NOT NULL DEFAULT 0,
    skipped_count INT NOT NULL DEFAULT 0,
    error_count INT NOT NULL DEFAULT 0,
    llm_log_ids_json LONGTEXT NULL,
    details_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_report_task_runs_run_id UNIQUE KEY (run_id),
    KEY idx_spec_runtime_report_task_runs_started_at (started_at),
    KEY idx_spec_runtime_report_task_runs_status (status)
);

CREATE TABLE IF NOT EXISTS spec_runtime_report_schedule_executions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    execution_key VARCHAR(191) NOT NULL,
    trigger_type VARCHAR(32) NOT NULL,
    period_type VARCHAR(32) NOT NULL,
    period_start DATETIME NOT NULL,
    period_end DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_report_schedule_executions_key UNIQUE KEY (execution_key),
    KEY idx_spec_runtime_report_schedule_executions_period (trigger_type, period_type, period_start, period_end)
);

CREATE TABLE IF NOT EXISTS spec_runtime_evaluation_settings (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    settings_key VARCHAR(191) NOT NULL DEFAULT 'default',
    settings_json LONGTEXT NOT NULL,
    updated_by VARCHAR(191) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_spec_runtime_evaluation_settings_key UNIQUE KEY (settings_key)
);

CREATE TABLE IF NOT EXISTS screeners_definitions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    screener_id VARCHAR(191) NOT NULL,
    name VARCHAR(255) NOT NULL,
    adapter_binding_json JSON NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    active_revision_id BIGINT UNSIGNED NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'DRAFT',
    created_by VARCHAR(191) NOT NULL,
    deleted_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_definitions_screener_id UNIQUE KEY (screener_id),
    KEY idx_screeners_definitions_status_updated (status, updated_at)
);

CREATE TABLE IF NOT EXISTS screeners_definition_revisions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    definition_id BIGINT UNSIGNED NOT NULL,
    revision INT UNSIGNED NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    definition_json JSON NOT NULL,
    compiled_plan_json JSON NULL,
    definition_hash VARCHAR(128) NOT NULL,
    compiled_plan_hash VARCHAR(128) NULL,
    created_by VARCHAR(191) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_definition_revision UNIQUE KEY (definition_id, revision),
    KEY idx_screeners_definition_revisions_hash (definition_hash),
    CONSTRAINT fk_screeners_definition_revisions_definition FOREIGN KEY (definition_id)
        REFERENCES screeners_definitions(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_adapter_capability_snapshots (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    capability_snapshot_id VARCHAR(191) NOT NULL,
    adapter_id VARCHAR(191) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    profile_id VARCHAR(191) NULL,
    profile_json JSON NOT NULL,
    status VARCHAR(64) NOT NULL,
    probed_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_capability_snapshot_id UNIQUE KEY (capability_snapshot_id),
    KEY idx_screeners_capability_adapter_probed (adapter_id, probed_at)
);

CREATE TABLE IF NOT EXISTS screeners_catalog_versions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    provider VARCHAR(64) NOT NULL,
    version VARCHAR(191) NOT NULL,
    status VARCHAR(64) NOT NULL,
    source_hash VARCHAR(128) NOT NULL,
    fetched_at DATETIME NOT NULL,
    last_checked_at DATETIME NOT NULL,
    activated_at DATETIME NULL,
    error_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_catalog_provider_version UNIQUE KEY (provider, version),
    KEY idx_screeners_catalog_provider_status (provider, status, activated_at)
);

CREATE TABLE IF NOT EXISTS screeners_catalog_items (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    version_id BIGINT UNSIGNED NOT NULL,
    kind VARCHAR(64) NOT NULL,
    item_key VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT NULL,
    value_type VARCHAR(64) NULL,
    operators_json JSON NULL,
    enum_values_json JSON NULL,
    compatibility_json JSON NULL,
    search_text TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_catalog_item UNIQUE KEY (version_id, kind, item_key),
    KEY idx_screeners_catalog_items_lookup (version_id, kind, item_key),
    KEY idx_screeners_catalog_items_kind (version_id, kind),
    FULLTEXT KEY idx_screeners_catalog_items_search (search_text),
    CONSTRAINT fk_screeners_catalog_items_version FOREIGN KEY (version_id)
        REFERENCES screeners_catalog_versions(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_catalog_sync_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    sync_run_id VARCHAR(191) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    trigger_type VARCHAR(64) NOT NULL,
    status VARCHAR(64) NOT NULL,
    adapter_id VARCHAR(191) NULL,
    catalog_version VARCHAR(191) NULL,
    source_hash VARCHAR(128) NULL,
    counts_json JSON NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    error_code VARCHAR(128) NULL,
    error_detail_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_catalog_sync_run_id UNIQUE KEY (sync_run_id),
    KEY idx_screeners_catalog_sync_provider_started (provider, started_at),
    KEY idx_screeners_catalog_sync_status (status, started_at)
);

CREATE TABLE IF NOT EXISTS screeners_catalog_overrides (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    provider VARCHAR(64) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    item_key VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT NULL,
    compatibility_json JSON NULL,
    reason TEXT NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    unverified TINYINT(1) NOT NULL DEFAULT 1,
    created_by VARCHAR(191) NOT NULL,
    updated_by VARCHAR(191) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_catalog_override UNIQUE KEY (provider, kind, item_key),
    KEY idx_screeners_catalog_overrides_enabled (provider, kind, enabled)
);

CREATE TABLE IF NOT EXISTS screeners_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id VARCHAR(191) NOT NULL,
    definition_id BIGINT UNSIGNED NOT NULL,
    revision_id BIGINT UNSIGNED NOT NULL,
    mode VARCHAR(64) NOT NULL,
    session_date DATE NULL,
    status VARCHAR(64) NOT NULL,
    counts_json JSON NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_runs_run_id UNIQUE KEY (run_id),
    KEY idx_screeners_runs_definition_started (definition_id, started_at),
    KEY idx_screeners_runs_status (status, started_at),
    CONSTRAINT fk_screeners_runs_definition FOREIGN KEY (definition_id)
        REFERENCES screeners_definitions(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_screeners_runs_revision FOREIGN KEY (revision_id)
        REFERENCES screeners_definition_revisions(id) ON DELETE RESTRICT ON UPDATE CASCADE
);

-- ATI_OPTIONS_V9_ACCOUNT_BEGIN
-- A01: Account owns broker evidence; Orders remains the owner of financial lots.
CREATE TABLE IF NOT EXISTS option_account_cursor (
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    broker VARCHAR(16) NOT NULL,
    environment VARCHAR(8) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    profile_id VARCHAR(128) NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    account_sequence BIGINT NOT NULL DEFAULT 0,
    as_of DATETIME(6) NULL,
    checkpoint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    UNIQUE KEY uq_option_account_identity (broker,environment,account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS option_account_snapshot (
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    snapshot_id VARCHAR(128) NOT NULL,
    account_sequence BIGINT NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    profile_id VARCHAR(128) NOT NULL,
    generation BIGINT NOT NULL,
    as_of DATETIME(6) NOT NULL,
    checkpoint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    equity_cash DECIMAL(65,12) NOT NULL,
    cash_available DECIMAL(65,12) NOT NULL,
    option_buying_power DECIMAL(65,12) NULL,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (account_key,snapshot_id),
    UNIQUE KEY uq_option_account_sequence (account_key,account_sequence),
    UNIQUE KEY uq_option_account_checkpoint (account_key,checkpoint),
    CONSTRAINT fk_option_account_snapshot_cursor FOREIGN KEY (account_key) REFERENCES option_account_cursor(account_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS option_account_lifecycle (
    sequence BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    activity_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    revision BIGINT NOT NULL,
    payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload JSON NOT NULL,
    effective_at DATETIME(6) NOT NULL,
    received_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_option_lifecycle_revision (account_key,activity_key,revision),
    KEY idx_option_lifecycle_account_sequence (account_key,sequence),
    CONSTRAINT fk_option_account_lifecycle_cursor FOREIGN KEY (account_key) REFERENCES option_account_cursor(account_key),
    CONSTRAINT ck_option_lifecycle_revision CHECK (revision > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS option_account_outbox (
    sequence BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    checkpoint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    delivered_at DATETIME(6) NULL,
    UNIQUE KEY uq_option_account_event (event_id),
    KEY idx_option_account_outbox_pending (delivered_at,sequence),
    KEY idx_option_account_outbox_account (account_key,sequence),
    CONSTRAINT fk_option_account_outbox_cursor FOREIGN KEY (account_key) REFERENCES option_account_cursor(account_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
-- ATI_OPTIONS_V9_ACCOUNT_END


-- ATI_OPTIONS_V9_EXPOSURE_BEGIN
-- R01: all coordinated account order writers share these Risk-owned tables.
CREATE TABLE IF NOT EXISTS account_exposure_budget (
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    broker VARCHAR(64) NOT NULL,
    environment VARCHAR(8) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    profile_id VARCHAR(128) NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    snapshot_id VARCHAR(128) NULL,
    snapshot_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    account_checkpoint VARCHAR(128) NULL,
    as_of DATETIME(6) NULL,
    free_bp_cash DECIMAL(65,12) NOT NULL DEFAULT 0,
    cash_available DECIMAL(65,12) NOT NULL DEFAULT 0,
    open_risk_cash DECIMAL(65,12) NOT NULL DEFAULT 0,
    max_open_risk_cash DECIMAL(65,12) NOT NULL DEFAULT 0,
    bp_limit_cash DECIMAL(65,12) NOT NULL DEFAULT 0,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    unresolved_orders INT NOT NULL DEFAULT 0,
    unallocated_positions INT NOT NULL DEFAULT 0,
    all_order_writers_admission_version INT NOT NULL DEFAULT 0,
    options_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    global_halt BOOLEAN NOT NULL DEFAULT FALSE,
    revision BIGINT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_exposure_account_identity (broker,environment,account_id),
    CONSTRAINT ck_exposure_budget_nonnegative CHECK (
        free_bp_cash >= 0 AND cash_available >= 0 AND open_risk_cash >= 0
        AND max_open_risk_cash >= 0 AND bp_limit_cash >= 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS account_exposure_claims (
    reservation_id VARCHAR(128) NOT NULL PRIMARY KEY,
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    command_id VARCHAR(128) NOT NULL,
    request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    order_payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    owner_domain VARCHAR(16) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    profile_id VARCHAR(128) NOT NULL,
    generation BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    held_bp_cash DECIMAL(65,12) NOT NULL,
    held_cash DECIMAL(65,12) NOT NULL,
    held_risk_cash DECIMAL(65,12) NOT NULL,
    remaining_bp_cash DECIMAL(65,12) NOT NULL,
    remaining_cash DECIMAL(65,12) NOT NULL,
    bound_order_group_id VARCHAR(128) NULL,
    bound_legacy_order_id VARCHAR(128) NULL,
    account_checkpoint VARCHAR(128) NOT NULL,
    release_evidence_ref VARCHAR(128) NULL,
    reflection_evidence_ref VARCHAR(128) NULL,
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NULL,
    resource_version BIGINT NOT NULL DEFAULT 1,
    UNIQUE KEY uq_exposure_command (account_key,command_id),
    UNIQUE KEY uq_exposure_group (account_key,bound_order_group_id),
    UNIQUE KEY uq_exposure_legacy_order (account_key,bound_legacy_order_id),
    KEY idx_exposure_active (account_key,status,expires_at),
    CONSTRAINT fk_exposure_budget FOREIGN KEY (account_key) REFERENCES account_exposure_budget(account_key),
    CONSTRAINT ck_exposure_claim_status CHECK (status IN ('HELD','COMMITTED','REFLECTED','RELEASED','EXPIRED')),
    CONSTRAINT ck_exposure_claim_domain CHECK (owner_domain IN ('LEGACY','OPTIONS','MANUAL')),
    CONSTRAINT ck_exposure_claim_nonnegative CHECK (
        held_bp_cash >= 0 AND held_cash >= 0 AND held_risk_cash >= 0
        AND remaining_bp_cash >= 0 AND remaining_bp_cash <= held_bp_cash
        AND remaining_cash >= 0 AND remaining_cash <= held_cash
    ),
    CONSTRAINT ck_exposure_single_order CHECK (bound_order_group_id IS NULL OR bound_legacy_order_id IS NULL),
    CONSTRAINT ck_exposure_held_unbound CHECK (
        status != 'HELD' OR (bound_order_group_id IS NULL AND bound_legacy_order_id IS NULL)
    ),
    CONSTRAINT ck_exposure_claimed_bound CHECK (
        status NOT IN ('COMMITTED','REFLECTED') OR
        (owner_domain='OPTIONS' AND bound_order_group_id IS NOT NULL AND bound_legacy_order_id IS NULL) OR
        (owner_domain IN ('LEGACY','MANUAL') AND bound_legacy_order_id IS NOT NULL AND bound_order_group_id IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

ALTER TABLE account_exposure_claims ADD COLUMN IF NOT EXISTS reflection_evidence_ref VARCHAR(128) NULL;

CREATE TABLE IF NOT EXISTS risk_exposure_outbox (
    sequence_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_id VARCHAR(128) NOT NULL,
    account_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    aggregate_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    delivered_at DATETIME(6) NULL,
    attempts INT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_exposure_outbox_event (event_id),
    KEY idx_exposure_outbox_pending (delivered_at,sequence_id),
    KEY idx_exposure_outbox_account (account_key,sequence_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
-- ATI_OPTIONS_V9_EXPOSURE_END

CREATE TABLE IF NOT EXISTS screeners_run_stages (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id VARCHAR(191) NOT NULL,
    stage_key VARCHAR(191) NOT NULL,
    logical_order INT UNSIGNED NOT NULL,
    status VARCHAR(64) NOT NULL,
    input_count INT UNSIGNED NOT NULL DEFAULT 0,
    output_count INT UNSIGNED NOT NULL DEFAULT 0,
    input_json JSON NULL,
    output_json JSON NULL,
    error_json JSON NULL,
    duration_ms BIGINT UNSIGNED NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_run_stage UNIQUE KEY (run_id, stage_key),
    KEY idx_screeners_run_stages_order (run_id, logical_order),
    CONSTRAINT fk_screeners_run_stages_run FOREIGN KEY (run_id)
        REFERENCES screeners_runs(run_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_stage_symbol_results (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    stage_id BIGINT UNSIGNED NOT NULL,
    symbol_key VARCHAR(255) NOT NULL,
    status VARCHAR(64) NOT NULL,
    input_json JSON NULL,
    output_json JSON NULL,
    reason_code VARCHAR(128) NULL,
    error_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_screeners_stage_symbols_status (stage_id, status, symbol_key),
    CONSTRAINT fk_screeners_stage_symbols_stage FOREIGN KEY (stage_id)
        REFERENCES screeners_run_stages(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_candidates (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    definition_id BIGINT UNSIGNED NOT NULL,
    run_id VARCHAR(191) NOT NULL,
    symbol_key VARCHAR(255) NOT NULL,
    symbol_label VARCHAR(64) NOT NULL,
    lifecycle VARCHAR(64) NOT NULL,
    fields_json JSON NOT NULL,
    discovery_json JSON NOT NULL,
    last_seen_at DATETIME NOT NULL,
    expires_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_candidate UNIQUE KEY (definition_id, symbol_key),
    KEY idx_screeners_candidates_lifecycle (definition_id, lifecycle, last_seen_at),
    CONSTRAINT fk_screeners_candidates_definition FOREIGN KEY (definition_id)
        REFERENCES screeners_definitions(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_screeners_candidates_run FOREIGN KEY (run_id)
        REFERENCES screeners_runs(run_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_symbol_field_cache (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    adapter_id VARCHAR(191) NOT NULL,
    symbol_key VARCHAR(255) NOT NULL,
    symbol_label VARCHAR(64) NOT NULL,
    contract_json JSON NOT NULL,
    fields_json JSON NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'READY',
    error_json JSON NULL,
    fetched_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_screeners_symbol_field_cache (adapter_id, symbol_key),
    KEY idx_screeners_symbol_field_cache_expiry (expires_at)
);

-- UI-only candidate metadata. This table is deliberately separate from both
-- Preview values and fields used for live Screener qualification.
CREATE TABLE IF NOT EXISTS screeners_symbol_display_cache (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    adapter_id VARCHAR(191) NOT NULL,
    symbol_key VARCHAR(255) NOT NULL,
    symbol_label VARCHAR(64) NOT NULL,
    fields_json JSON NOT NULL,
    fetched_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_screeners_symbol_display_cache (adapter_id, symbol_key),
    KEY idx_screeners_symbol_display_cache_expiry (expires_at)
);

CREATE TABLE IF NOT EXISTS screeners_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_id VARCHAR(191) NOT NULL,
    definition_id BIGINT UNSIGNED NOT NULL,
    run_id VARCHAR(191) NOT NULL,
    symbol_key VARCHAR(255) NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    payload_json JSON NOT NULL,
    occurred_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_events_event_id UNIQUE KEY (event_id),
    KEY idx_screeners_events_definition_occurred (definition_id, occurred_at),
    CONSTRAINT fk_screeners_events_definition FOREIGN KEY (definition_id)
        REFERENCES screeners_definitions(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_screeners_events_run FOREIGN KEY (run_id)
        REFERENCES screeners_runs(run_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_actions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    action_id VARCHAR(191) NOT NULL,
    event_id VARCHAR(191) NOT NULL,
    action_type VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    target_json JSON NOT NULL,
    result_json JSON NULL,
    error_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_actions_action_id UNIQUE KEY (action_id),
    CONSTRAINT uq_screeners_actions_idempotency UNIQUE KEY (idempotency_key),
    KEY idx_screeners_actions_event (event_id, status),
    CONSTRAINT fk_screeners_actions_event FOREIGN KEY (event_id)
        REFERENCES screeners_events(event_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS screeners_runtime_state (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    definition_id BIGINT UNSIGNED NOT NULL,
    revision_id BIGINT UNSIGNED NOT NULL,
    session_date DATE NULL,
    state VARCHAR(64) NOT NULL,
    stream_handles_json JSON NOT NULL,
    summary_json JSON NOT NULL,
    heartbeat_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_screeners_runtime_definition UNIQUE KEY (definition_id),
    KEY idx_screeners_runtime_state_heartbeat (state, heartbeat_at),
    CONSTRAINT fk_screeners_runtime_definition FOREIGN KEY (definition_id)
        REFERENCES screeners_definitions(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_screeners_runtime_revision FOREIGN KEY (revision_id)
        REFERENCES screeners_definition_revisions(id) ON DELETE RESTRICT ON UPDATE CASCADE
);
