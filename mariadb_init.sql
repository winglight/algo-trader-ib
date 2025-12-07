-- Schema initialization for the algo-trader services.
--
-- This script provisions the MariaDB tables required by the
-- order management, configuration, and optimizer components.

CREATE TABLE IF NOT EXISTS config (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    config_key VARCHAR(191) NOT NULL,
    config_value TEXT NULL,
    description TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_config_config_key UNIQUE KEY (config_key)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    version VARCHAR(255) NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_schema_migrations_version UNIQUE KEY (version)
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
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
    fill_price DOUBLE NULL,
    filled_quantity DOUBLE NULL,
    remaining_quantity DOUBLE NULL,
    order_source VARCHAR(32) NULL,
    strategy VARCHAR(191) NULL,
    strategy_name VARCHAR(191) NULL,
    metrics_owner_id VARCHAR(191) NULL,
    parent_order_id VARCHAR(191) NULL,
    exchange VARCHAR(64) NULL,
    sec_type VARCHAR(64) NULL,
    notes TEXT NULL,
    commission DOUBLE NULL,
    pnl DOUBLE NULL,
    is_deleted TINYINT(1) NOT NULL DEFAULT 0,
    CONSTRAINT uq_orders_ib_perm_id UNIQUE KEY (ib_perm_id),
    CONSTRAINT uq_orders_ib_order_id UNIQUE KEY (ib_order_id)
);

ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_orders_symbol_status (symbol, status);
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_orders_strategy (strategy);
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_orders_metrics_owner (metrics_owner_id);
ALTER TABLE orders ADD INDEX IF NOT EXISTS idx_orders_created_at (created_at);

DELIMITER //
CREATE PROCEDURE backfill_orders_metrics_owner()
BEGIN
    DECLARE raw_payload_exists INT DEFAULT 0;
    DECLARE metrics_owner_exists INT DEFAULT 0;

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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_order_fills_exec (exec_id),
    KEY idx_order_fills_order_id (order_id),
    CONSTRAINT fk_order_fills_orders FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
);

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
    rule_type VARCHAR(64) NOT NULL DEFAULT 'fixed',
    notes TEXT NULL DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

ALTER TABLE risk_rules ADD UNIQUE INDEX IF NOT EXISTS ux_risk_rules_symbol (symbol);
ALTER TABLE risk_rules ADD INDEX IF NOT EXISTS idx_risk_rules_enabled (enabled);

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

ALTER TABLE optimizer_jobs ADD INDEX IF NOT EXISTS idx_optimizer_jobs_status (status);
ALTER TABLE optimizer_jobs ADD INDEX IF NOT EXISTS idx_optimizer_jobs_optimizer_plan_id (optimizer_plan_id);
ALTER TABLE optimizer_jobs ADD INDEX IF NOT EXISTS idx_optimizer_jobs_created_at (created_at);

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

ALTER TABLE notifications ADD INDEX IF NOT EXISTS idx_notifications_created_at (created_at);
ALTER TABLE notifications ADD INDEX IF NOT EXISTS idx_notifications_is_read (is_read);

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
    scanner_profile JSON NULL,
    scanner_schedule JSON NULL,
    scanner_filter_definitions TEXT NULL,
    child_strategy_type VARCHAR(191) NULL,
    child_parameters JSON NULL,
    max_children INT NULL,
    selection_limit INT NULL,
    primary_symbol VARCHAR(191) NULL,
    data_source VARCHAR(191) NULL,
    trigger_count INT NOT NULL DEFAULT 0,
    last_triggered_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

ALTER TABLE strategies ADD INDEX IF NOT EXISTS idx_strategies_enabled (enabled);
ALTER TABLE strategies ADD INDEX IF NOT EXISTS idx_strategies_updated_at (updated_at);

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
