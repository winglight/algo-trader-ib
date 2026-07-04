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
    ib_order_id VARCHAR(191) NULL,
    ib_perm_id VARCHAR(191) NULL,
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
    ib_open_close VARCHAR(16) NULL,
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
    CONSTRAINT uq_orders_ib_perm_id UNIQUE KEY (ib_perm_id),
    CONSTRAINT uq_orders_ib_order_id UNIQUE KEY (ib_order_id)
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
    exec_id VARCHAR(191) NOT NULL,
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
    UNIQUE KEY uq_order_fills_exec (exec_id),
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

DELIMITER //
CREATE PROCEDURE add_trade_logs_foreign_keys()
BEGIN
    IF EXISTS (
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
    ) THEN
        IF NOT EXISTS (
            SELECT 1
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'trade_logs'
              AND CONSTRAINT_NAME = 'fk_trade_logs_user'
        ) THEN
            ALTER TABLE trade_logs
                ADD CONSTRAINT fk_trade_logs_user FOREIGN KEY (user_id)
                    REFERENCES users(id)
                    ON DELETE SET NULL
                    ON UPDATE CASCADE;
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'accounts'
    ) THEN
        IF NOT EXISTS (
            SELECT 1
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'trade_logs'
              AND CONSTRAINT_NAME = 'fk_trade_logs_account'
        ) THEN
            ALTER TABLE trade_logs
                ADD CONSTRAINT fk_trade_logs_account FOREIGN KEY (account_id)
                    REFERENCES accounts(id)
                    ON DELETE SET NULL
                    ON UPDATE CASCADE;
        END IF;
    END IF;
END//
DELIMITER ;

CALL add_trade_logs_foreign_keys();
DROP PROCEDURE add_trade_logs_foreign_keys;

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

CREATE TABLE IF NOT EXISTS optimizer_plans (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    algorithm VARCHAR(64) NOT NULL,
    base_metrics JSON NULL,
    feature_metrics JSON NULL,
    parameters JSON NULL,
    start_date VARCHAR(32) NULL,
    end_date VARCHAR(32) NULL,
    frequency_minutes INT NULL,
    iterations INT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 0,
    last_run_at VARCHAR(64) NULL
);

CREATE TABLE IF NOT EXISTS optimizer_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    optimizer_plan_id BIGINT UNSIGNED NOT NULL,
    objective VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    progress DOUBLE NOT NULL DEFAULT 0,
    metadata JSON NULL,
    parameter_space JSON NULL,
    result_payload JSON NULL,
    error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_optimizer_jobs_plan FOREIGN KEY (optimizer_plan_id) REFERENCES optimizer_plans (id)
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'optimizer_jobs'
      AND INDEX_NAME = 'idx_optimizer_jobs_status'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_optimizer_jobs_status ON optimizer_jobs (status)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'optimizer_jobs'
      AND INDEX_NAME = 'idx_optimizer_jobs_optimizer_plan_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_optimizer_jobs_optimizer_plan_id ON optimizer_jobs (optimizer_plan_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'optimizer_jobs'
      AND INDEX_NAME = 'idx_optimizer_jobs_created_at'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_optimizer_jobs_created_at ON optimizer_jobs (created_at)', 'DO 0');
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

CREATE TABLE IF NOT EXISTS strategies (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    strategy_type VARCHAR(191) NOT NULL,
    strategy_origin VARCHAR(32) NOT NULL DEFAULT 'internal',
    title VARCHAR(191) NOT NULL,
    description TEXT NULL,
    file_path TEXT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    parameters JSON NULL,
    schedule JSON NULL,
    screener_profile JSON NULL,
    screener_schedule JSON NULL,
    scanner_filter_definitions TEXT NULL,
    child_strategy_type VARCHAR(191) NULL,
    child_parameters JSON NULL,
    max_children INT NULL,
    selection_limit INT NULL,
    scanner_tag_filters TEXT NULL,
    primary_symbol VARCHAR(191) NULL,
    data_source VARCHAR(191) NULL,
    trigger_count INT NOT NULL DEFAULT 0,
    last_triggered_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);


SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND INDEX_NAME = 'idx_strategies_enabled'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_strategies_enabled ON strategies (enabled)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND INDEX_NAME = 'idx_strategies_updated_at'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_strategies_updated_at ON strategies (updated_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS screener_results (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    strategy_ref_id BIGINT UNSIGNED NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    run_at DATETIME NOT NULL,
    trading_date DATE NOT NULL,
    screener_profile JSON NULL,
    screener_schedule JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_screener_results_strategy FOREIGN KEY (strategy_ref_id) REFERENCES strategies (id)
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'screener_results'
      AND INDEX_NAME = 'idx_screener_results_strategy'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_screener_results_strategy ON screener_results (strategy_ref_id, run_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS screener_result_symbols (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    result_ref_id BIGINT UNSIGNED NOT NULL,
    symbol VARCHAR(191) NOT NULL,
    rank INT NULL,
    metadata JSON NULL,
    open_price DOUBLE NULL,
    close_price DOUBLE NULL,
    return_rate DOUBLE NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_screener_symbols_result FOREIGN KEY (result_ref_id) REFERENCES screener_results (id) ON DELETE CASCADE
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'screener_result_symbols'
      AND INDEX_NAME = 'idx_screener_result_symbols_result'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_screener_result_symbols_result ON screener_result_symbols (result_ref_id, rank)', 'DO 0');
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
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_watchlist_groups_strategy UNIQUE KEY (strategy_ref_id),
    CONSTRAINT fk_watchlist_groups_strategy FOREIGN KEY (strategy_ref_id)
        REFERENCES strategies (id)
        ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS strategy_risk_settings (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    strategy_ref_id BIGINT UNSIGNED NOT NULL,
    max_position INT NULL,
    forbid_pyramiding TINYINT(1) NOT NULL DEFAULT 0,
    loss_threshold DOUBLE NULL,
    loss_duration_minutes INT NULL,
    notify_on_breach TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_strategy_risk_settings_strategy_ref_id UNIQUE KEY (strategy_ref_id),
    CONSTRAINT fk_strategy_risk_settings_strategy FOREIGN KEY (strategy_ref_id) REFERENCES strategies (id)
);

-- =========================================================
-- Audit service tables (main/live schema)
-- =========================================================

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

CREATE TABLE IF NOT EXISTS audit_counterfactual_jobs (
    job_id VARCHAR(191) NOT NULL PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    scenario VARCHAR(64) NOT NULL,
    request_json TEXT NULL,
    result_json TEXT NULL,
    rows_count INT NOT NULL DEFAULT 0,
    requested_by VARCHAR(191) NULL,
    error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    updated_at DATETIME NOT NULL
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'audit_counterfactual_jobs'
      AND index_name = 'idx_audit_counterfactual_jobs_status'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_audit_counterfactual_jobs_status ON audit_counterfactual_jobs (status)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'audit_counterfactual_jobs'
      AND index_name = 'idx_audit_counterfactual_jobs_created_at'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_audit_counterfactual_jobs_created_at ON audit_counterfactual_jobs (created_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS sim_runs (
    sim_run_id VARCHAR(191) NOT NULL PRIMARY KEY,
    correlation_id VARCHAR(191) NOT NULL,
    decision_trace_id VARCHAR(191) NULL,
    run_mode VARCHAR(32) NOT NULL DEFAULT 'sync',
    status VARCHAR(32) NOT NULL DEFAULT 'completed',
    priority INT NOT NULL DEFAULT 0,
    timeout_ms INT NULL,
    budget_json JSON NULL,
    model_version_map_json JSON NULL,
    trade_candidate_json JSON NULL,
    payoff_spec_json JSON NULL,
    exec_plan_id VARCHAR(191) NULL,
    error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'sim_runs'
      AND INDEX_NAME = 'idx_sim_runs_correlation_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_sim_runs_correlation_id ON sim_runs (correlation_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'sim_runs'
      AND INDEX_NAME = 'idx_sim_runs_decision_trace_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_sim_runs_decision_trace_id ON sim_runs (decision_trace_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'sim_runs'
      AND INDEX_NAME = 'idx_sim_runs_exec_plan_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_sim_runs_exec_plan_id ON sim_runs (exec_plan_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'sim_runs'
      AND INDEX_NAME = 'idx_sim_runs_created_run_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_sim_runs_created_run_id ON sim_runs (created_at, sim_run_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS sim_results (
    sim_run_id VARCHAR(191) NOT NULL PRIMARY KEY,
    status VARCHAR(32) NOT NULL DEFAULT 'completed',
    summary_json JSON NULL,
    recommendations_json JSON NULL,
    diagnostics_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_sim_results_runs FOREIGN KEY (sim_run_id)
        REFERENCES sim_runs(sim_run_id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS sim_templates (
    template_id VARCHAR(191) NOT NULL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    config_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exec_plans (
    exec_plan_id VARCHAR(191) NOT NULL PRIMARY KEY,
    sim_run_id VARCHAR(191) NULL,
    decision_trace_id VARCHAR(191) NULL,
    symbol VARCHAR(64) NULL,
    side VARCHAR(16) NULL,
    quantity DOUBLE NULL,
    plan_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_exec_plans_runs FOREIGN KEY (sim_run_id)
        REFERENCES sim_runs(sim_run_id)
        ON DELETE SET NULL
        ON UPDATE CASCADE
);

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'exec_plans'
      AND INDEX_NAME = 'idx_exec_plans_sim_run_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_exec_plans_sim_run_id ON exec_plans (sim_run_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'exec_plans'
      AND INDEX_NAME = 'idx_exec_plans_decision_trace_id'
);
SET @sql = IF(@idx_exists = 0, 'CREATE INDEX idx_exec_plans_decision_trace_id ON exec_plans (decision_trace_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- Strategy Spec Runtime evaluation tables.
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

CREATE TABLE IF NOT EXISTS strategy_runtime_simulation_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    trace_id VARCHAR(191) NOT NULL,
    sim_run_id VARCHAR(191) NULL,
    exec_plan_id VARCHAR(191) NULL,
    decision_trace_id VARCHAR(191) NULL,
    workflow_id VARCHAR(191) NULL,
    spec_hash VARCHAR(191) NULL,
    strategy_id VARCHAR(191) NULL,
    strategy_name VARCHAR(255) NULL,
    symbol VARCHAR(64) NULL,
    side VARCHAR(32) NULL,
    quantity DOUBLE NULL,
    order_type VARCHAR(64) NULL,
    order_status VARCHAR(64) NULL,
    status VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    reason VARCHAR(255) NULL,
    enforcement_mode VARCHAR(64) NULL,
    summary_json LONGTEXT NOT NULL,
    recommendations_json LONGTEXT NOT NULL,
    breaches_json LONGTEXT NOT NULL,
    logs_json LONGTEXT NOT NULL,
    trace_json LONGTEXT NOT NULL,
    evaluated_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_strategy_runtime_sim_trace UNIQUE KEY (trace_id),
    KEY idx_strategy_runtime_sim_created (created_at),
    KEY idx_strategy_runtime_sim_symbol (symbol),
    KEY idx_strategy_runtime_sim_strategy (strategy_id),
    KEY idx_strategy_runtime_sim_status (status)
);
