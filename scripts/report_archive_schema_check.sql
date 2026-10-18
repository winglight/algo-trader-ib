-- Read-only installer pre/postflight. Missing contracts are identifiers, never row data.
SELECT 'screeners_report_scan_days:engine' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND engine='InnoDB')
UNION ALL
SELECT 'screeners_report_scan_days:day_key' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='day_key' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_days:screener_id' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='screener_id' AND data_type='varchar' AND is_nullable='NO' AND character_maximum_length=191)
UNION ALL
SELECT 'screeners_report_scan_days:definition_hash' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='definition_hash' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_days:session_date' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='session_date' AND data_type='date' AND is_nullable='NO')
UNION ALL
SELECT 'screeners_report_scan_days:plan_hash' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='plan_hash' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_days:plan_json' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='plan_json' AND data_type='longtext' AND is_nullable='NO')
UNION ALL
SELECT 'screeners_report_scan_days:archived_at' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND column_name='archived_at' AND data_type='datetime' AND is_nullable='NO' AND datetime_precision=6)
UNION ALL
SELECT 'screeners_report_scan_revisions:engine' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND engine='InnoDB')
UNION ALL
SELECT 'screeners_report_scan_revisions:day_key' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='day_key' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_revisions:logical_scan_id' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='logical_scan_id' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_revisions:revision' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='revision' AND data_type='int' AND is_nullable='NO' AND column_type LIKE '%unsigned%')
UNION ALL
SELECT 'screeners_report_scan_revisions:scheduled_at' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='scheduled_at' AND data_type='datetime' AND is_nullable='NO' AND datetime_precision=6)
UNION ALL
SELECT 'screeners_report_scan_revisions:available_at' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='available_at' AND data_type='datetime' AND is_nullable='NO' AND datetime_precision=6)
UNION ALL
SELECT 'screeners_report_scan_revisions:status' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='status' AND data_type='enum' AND is_nullable='NO' AND column_type='enum(''COMPLETE'',''COMPLETE_EMPTY'',''PARTIAL'',''FAILED'')')
UNION ALL
SELECT 'screeners_report_scan_revisions:content_hash' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='content_hash' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_revisions:event_hash' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='event_hash' AND data_type='char' AND is_nullable='NO' AND character_maximum_length=64 AND collation_name='ascii_bin')
UNION ALL
SELECT 'screeners_report_scan_revisions:scan_json' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='scan_json' AND data_type='longtext' AND is_nullable='NO')
UNION ALL
SELECT 'screeners_report_scan_revisions:archived_at' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND column_name='archived_at' AND data_type='datetime' AND is_nullable='NO' AND datetime_precision=6)
UNION ALL
SELECT 'screeners_report_scan_days:unique:day_key' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND non_unique=0 GROUP BY index_name HAVING GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')='day_key' AND SUM(sub_part IS NOT NULL)=0)
UNION ALL
SELECT 'screeners_report_scan_days:unique:screener_id,definition_hash,session_date' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_days' AND non_unique=0 GROUP BY index_name HAVING GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')='screener_id,definition_hash,session_date' AND SUM(sub_part IS NOT NULL)=0)
UNION ALL
SELECT 'screeners_report_scan_revisions:unique:day_key,logical_scan_id,revision' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND non_unique=0 GROUP BY index_name HAVING GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')='day_key,logical_scan_id,revision' AND SUM(sub_part IS NOT NULL)=0)
UNION ALL
SELECT 'screeners_report_scan_revisions:unique:day_key,scheduled_at,revision' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND non_unique=0 GROUP BY index_name HAVING GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')='day_key,scheduled_at,revision' AND SUM(sub_part IS NOT NULL)=0)
UNION ALL
SELECT 'screeners_report_scan_revisions:unique:day_key,logical_scan_id,event_hash' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND non_unique=0 GROUP BY index_name HAVING GROUP_CONCAT(column_name ORDER BY seq_in_index SEPARATOR ',')='day_key,logical_scan_id,event_hash' AND SUM(sub_part IS NOT NULL)=0)
UNION ALL
SELECT 'screeners_report_scan_revisions:foreign_key' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.referential_constraints r JOIN information_schema.key_column_usage k ON k.constraint_schema=r.constraint_schema AND k.table_name=r.table_name AND k.constraint_name=r.constraint_name WHERE r.constraint_schema=DATABASE() AND r.table_name='screeners_report_scan_revisions' AND r.referenced_table_name='screeners_report_scan_days' AND r.delete_rule='RESTRICT' AND r.update_rule='RESTRICT' AND k.column_name='day_key' AND k.referenced_column_name='day_key')
UNION ALL
SELECT 'screeners_report_scan_days:check:plan_json' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.check_constraints WHERE constraint_schema=DATABASE() AND table_name='screeners_report_scan_days' AND LOWER(REPLACE(REPLACE(check_clause,'`',''),' ',''))='json_valid(plan_json)')
UNION ALL
SELECT 'screeners_report_scan_revisions:check:scan_json' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.check_constraints WHERE constraint_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND LOWER(REPLACE(REPLACE(check_clause,'`',''),' ',''))='json_valid(scan_json)')
UNION ALL
SELECT 'screeners_report_scan_revisions:check:revision' AS missing_contract WHERE NOT EXISTS (SELECT 1 FROM information_schema.check_constraints WHERE constraint_schema=DATABASE() AND table_name='screeners_report_scan_revisions' AND LOWER(REPLACE(REPLACE(check_clause,'`',''),' ',''))='revision>0');
