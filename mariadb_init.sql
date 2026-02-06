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
    parent_order_id VARCHAR(191) NULL,
    exchange VARCHAR(64) NULL,
    sec_type VARCHAR(64) NULL,
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
    is_deleted TINYINT(1) NOT NULL DEFAULT 0,
    CONSTRAINT uq_orders_ib_perm_id UNIQUE KEY (ib_perm_id),
    CONSTRAINT uq_orders_ib_order_id UNIQUE KEY (ib_order_id)
);

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_symbol_status'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_orders_symbol_status ON orders (symbol, status)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_strategy'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_orders_strategy ON orders (strategy)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_metrics_owner'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_orders_metrics_owner ON orders (metrics_owner_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'orders'
      AND INDEX_NAME = 'idx_orders_created_at'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_orders_created_at ON orders (created_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'trade_logs'
      AND INDEX_NAME = 'idx_trade_logs_date_type'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_trade_logs_date_type ON trade_logs (`date`, `type`)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'trade_logs'
      AND INDEX_NAME = 'idx_trade_logs_user_id'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_trade_logs_user_id ON trade_logs (user_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'trade_logs'
      AND INDEX_NAME = 'idx_trade_logs_account_id'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_trade_logs_account_id ON trade_logs (account_id)', 'DO 0');
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

CREATE TABLE IF NOT EXISTS news_trade_signal (
    signal_id VARCHAR(191) NOT NULL PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    quantity DOUBLE NULL,
    stop_loss DOUBLE NULL,
    take_profit DOUBLE NULL,
    confidence DOUBLE NULL,
    prompt_template_id VARCHAR(191) NULL,
    news_ref VARCHAR(191) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'news_trade_signal'
      AND INDEX_NAME = 'idx_news_trade_signal_symbol_created_at'
);
SET @sql := IF(
    @idx_exists = 0,
    'CREATE INDEX idx_news_trade_signal_symbol_created_at ON news_trade_signal (symbol, created_at)',
    'DO 0'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS news_trade_execution (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    signal_id VARCHAR(191) NOT NULL,
    status VARCHAR(32) NOT NULL,
    order_id BIGINT UNSIGNED NULL,
    reason TEXT NULL,
    filled_qty DOUBLE NULL,
    filled_price DOUBLE NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_news_trade_execution_signal_id (signal_id),
    KEY idx_news_trade_execution_order_id (order_id),
    CONSTRAINT fk_news_trade_execution_order_id FOREIGN KEY (order_id)
        REFERENCES orders(id)
        ON DELETE SET NULL
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

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'risk_rules'
      AND INDEX_NAME = 'ux_risk_rules_symbol'
);
SET @sql := IF(@idx_exists = 0, 'CREATE UNIQUE INDEX ux_risk_rules_symbol ON risk_rules (symbol)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'risk_rules'
      AND INDEX_NAME = 'idx_risk_rules_enabled'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_risk_rules_enabled ON risk_rules (enabled)', 'DO 0');
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

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'optimizer_jobs'
      AND INDEX_NAME = 'idx_optimizer_jobs_status'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_optimizer_jobs_status ON optimizer_jobs (status)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'optimizer_jobs'
      AND INDEX_NAME = 'idx_optimizer_jobs_optimizer_plan_id'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_optimizer_jobs_optimizer_plan_id ON optimizer_jobs (optimizer_plan_id)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'optimizer_jobs'
      AND INDEX_NAME = 'idx_optimizer_jobs_created_at'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_optimizer_jobs_created_at ON optimizer_jobs (created_at)', 'DO 0');
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

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'notifications'
      AND INDEX_NAME = 'idx_notifications_created_at'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_notifications_created_at ON notifications (created_at)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'notifications'
      AND INDEX_NAME = 'idx_notifications_is_read'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_notifications_is_read ON notifications (is_read)', 'DO 0');
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
    primary_symbol VARCHAR(191) NULL,
    data_source VARCHAR(191) NULL,
    trigger_count INT NOT NULL DEFAULT 0,
    last_triggered_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND INDEX_NAME = 'idx_strategies_enabled'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_strategies_enabled ON strategies (enabled)', 'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND INDEX_NAME = 'idx_strategies_updated_at'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_strategies_updated_at ON strategies (updated_at)', 'DO 0');
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

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'screener_results'
      AND INDEX_NAME = 'idx_screener_results_strategy'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_screener_results_strategy ON screener_results (strategy_ref_id, run_at)', 'DO 0');
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

SET @idx_exists := (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'screener_result_symbols'
      AND INDEX_NAME = 'idx_screener_result_symbols_result'
);
SET @sql := IF(@idx_exists = 0, 'CREATE INDEX idx_screener_result_symbols_result ON screener_result_symbols (result_ref_id, rank)', 'DO 0');
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
