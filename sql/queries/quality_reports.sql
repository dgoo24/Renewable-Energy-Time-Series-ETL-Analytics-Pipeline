-- Data-quality reports for imported files.
-- Issues is stored as a JSON array of {code, severity, count, description}.
SELECT
    q.source_file,
    q.total_records,
    q.valid_measurements,
    q.invalid_measurements,
    q.suspicious_measurements,
    q.missing_measurements,
    q.duplicate_records,
    q.irregular_intervals,
    q.rejected_records,
    q.created_at,
    r.file_sha256
FROM quality_reports AS q
JOIN ingestion_runs AS r ON r.id = q.ingestion_run_id
ORDER BY q.created_at DESC;
