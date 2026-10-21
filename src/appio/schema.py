"""The schema-v1 DDL from the signed specification, section 6.2.

Keep the transaction delimiters here: migration executes these statements one at
a time and checks the empty-database preconditions after BEGIN IMMEDIATE.
"""

SCHEMA_VERSION = 1

SCHEMA_SQL = """\
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE boot (
    boot_id TEXT PRIMARY KEY NOT NULL CHECK (length(boot_id) > 0),
    boot_order INTEGER NOT NULL UNIQUE CHECK (boot_order > 0),
    file_finalized_through_mono_ns INTEGER NOT NULL DEFAULT 0
        CHECK (file_finalized_through_mono_ns >= 0 AND file_finalized_through_mono_ns % 3600000000000 = 0),
    started_mono_ns INTEGER NOT NULL CHECK (started_mono_ns >= 0),
    started_wall REAL NOT NULL CHECK (started_wall BETWEEN 0.0 AND 1.7976931348623157e308),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod'))
) STRICT;

CREATE TABLE app (
    app_id INTEGER PRIMARY KEY NOT NULL CHECK (app_id > 0),
    key TEXT NOT NULL UNIQUE CHECK (length(key) > 0),
    name TEXT NOT NULL CHECK (length(name) > 0),
    icon_path TEXT,
    first_seen REAL NOT NULL CHECK (first_seen BETWEEN 0.0 AND 1.7976931348623157e308),
    last_seen REAL NOT NULL CHECK (last_seen BETWEEN first_seen AND 1.7976931348623157e308),
    scope TEXT NOT NULL CHECK (scope IN ('user', 'system'))
) STRICT;

CREATE TABLE launch (
    launch_id INTEGER PRIMARY KEY NOT NULL CHECK (launch_id > 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    root_pid INTEGER NOT NULL CHECK (root_pid > 0),
    root_start_ticks INTEGER NOT NULL CHECK (root_start_ticks >= 0),
    started_mono_ns INTEGER NOT NULL CHECK (started_mono_ns >= 0),
    ended_mono_ns INTEGER CHECK (ended_mono_ns IS NULL OR ended_mono_ns >= started_mono_ns),
    proc_count INTEGER NOT NULL CHECK (proc_count >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    UNIQUE (launch_id, app_id, boot_id),
    UNIQUE (boot_id, app_id, root_pid, root_start_ticks)
) STRICT;

CREATE TABLE sample (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    launch_id INTEGER NOT NULL CHECK (launch_id >= 0),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    observed_mono_ns INTEGER NOT NULL CHECK (observed_mono_ns >= ts_mono_ns),
    interval_start_mono_ns INTEGER,
    observed_ns INTEGER,
    ts_wall REAL NOT NULL CHECK (ts_wall BETWEEN 0.0 AND 1.7976931348623157e308),
    rchar INTEGER NOT NULL CHECK (rchar BETWEEN 0 AND 9223372036854775807),
    wchar INTEGER NOT NULL CHECK (wchar BETWEEN 0 AND 9223372036854775807),
    read_bytes INTEGER NOT NULL CHECK (read_bytes BETWEEN 0 AND 9223372036854775807),
    write_bytes INTEGER NOT NULL CHECK (write_bytes BETWEEN 0 AND 9223372036854775807),
    delta_kind TEXT NOT NULL CHECK (delta_kind IN ('normal', 'baseline', 'reset')),
    first_seen_partial INTEGER NOT NULL CHECK (first_seen_partial IN (0, 1)),
    spans_suspend INTEGER NOT NULL CHECK (spans_suspend IN (0, 1)),
    CHECK ((interval_start_mono_ns IS NULL AND observed_ns IS NULL) OR
           (interval_start_mono_ns IS NOT NULL AND observed_ns IS NOT NULL AND
            interval_start_mono_ns BETWEEN 0 AND observed_mono_ns AND
            observed_ns BETWEEN 0 AND observed_mono_ns - interval_start_mono_ns)),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id, launch_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE sample_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE sample_dev (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    rbytes INTEGER NOT NULL CHECK (rbytes BETWEEN 0 AND 9223372036854775807),
    wbytes INTEGER NOT NULL CHECK (wbytes BETWEEN 0 AND 9223372036854775807),
    PRIMARY KEY (boot_id, source, epoch, seq, app_id, dev)
) WITHOUT ROWID, STRICT;

CREATE TABLE path (
    path_id INTEGER PRIMARY KEY NOT NULL CHECK (path_id > 0),
    fs_uuid TEXT NOT NULL,
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    inode INTEGER NOT NULL CHECK (inode BETWEEN 0 AND 9223372036854775807),
    generation INTEGER NOT NULL CHECK (generation BETWEEN 0 AND 4294967295),
    basename TEXT NOT NULL CHECK (length(basename) > 0),
    state TEXT NOT NULL CHECK (state IN ('resolved', 'truncated', 'unresolved')),
    identity_weak INTEGER NOT NULL CHECK (identity_weak IN (0, 1)),
    first_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    first_mono_ns INTEGER NOT NULL CHECK (first_mono_ns >= 0),
    last_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    last_mono_ns INTEGER NOT NULL CHECK (last_mono_ns >= 0),
    CHECK (first_boot_id <> last_boot_id OR last_mono_ns >= first_mono_ns),
    UNIQUE (fs_uuid, dev, inode, generation, basename, identity_weak)
) STRICT;

CREATE TABLE path_alias (
    path_id INTEGER NOT NULL REFERENCES path(path_id) ON DELETE CASCADE,
    basename TEXT NOT NULL CHECK (length(basename) > 0),
    first_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    first_mono_ns INTEGER NOT NULL CHECK (first_mono_ns >= 0),
    last_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    last_mono_ns INTEGER NOT NULL CHECK (last_mono_ns >= 0),
    CHECK (first_boot_id <> last_boot_id OR last_mono_ns >= first_mono_ns),
    PRIMARY KEY (path_id, basename)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_sample (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    path_id INTEGER NOT NULL REFERENCES path(path_id),
    reads INTEGER NOT NULL CHECK (reads BETWEEN 0 AND 9223372036854775807),
    writes INTEGER NOT NULL CHECK (writes BETWEEN 0 AND 9223372036854775807),
    bytes_r INTEGER NOT NULL CHECK (bytes_r BETWEEN 0 AND 9223372036854775807),
    bytes_w INTEGER NOT NULL CHECK (bytes_w BETWEEN 0 AND 9223372036854775807),
    last_seen_mono_ns INTEGER NOT NULL CHECK (last_seen_mono_ns >= 0),
    CHECK (last_seen_mono_ns >= ts_mono_ns AND last_seen_mono_ns - ts_mono_ns < 3600000000000),
    PRIMARY KEY (boot_id, app_id, ts_mono_ns, path_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    counted_omitted_files INTEGER NOT NULL CHECK (counted_omitted_files >= 0),
    counted_omitted_bytes_r INTEGER NOT NULL CHECK (counted_omitted_bytes_r >= 0),
    counted_omitted_bytes_w INTEGER NOT NULL CHECK (counted_omitted_bytes_w >= 0),
    uncounted_overflow_files INTEGER NOT NULL CHECK (uncounted_overflow_files >= 0),
    uncounted_overflow_bytes_r INTEGER NOT NULL CHECK (uncounted_overflow_bytes_r >= 0),
    uncounted_overflow_bytes_w INTEGER NOT NULL CHECK (uncounted_overflow_bytes_w >= 0),
    skipped_nonfile INTEGER NOT NULL CHECK (skipped_nonfile >= 0),
    skipped_unsupported_fs INTEGER NOT NULL CHECK (skipped_unsupported_fs >= 0),
    weak_identity_rows INTEGER NOT NULL CHECK (weak_identity_rows >= 0),
    hook_vfs_read INTEGER NOT NULL CHECK (hook_vfs_read >= 0),
    hook_vfs_write INTEGER NOT NULL CHECK (hook_vfs_write >= 0),
    hook_vfs_readv INTEGER NOT NULL CHECK (hook_vfs_readv >= 0),
    hook_vfs_writev INTEGER NOT NULL CHECK (hook_vfs_writev >= 0),
    hook_vfs_iter_read INTEGER NOT NULL CHECK (hook_vfs_iter_read >= 0),
    hook_vfs_iter_write INTEGER NOT NULL CHECK (hook_vfs_iter_write >= 0),
    PRIMARY KEY (boot_id, app_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE file_hour_state (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    PRIMARY KEY (boot_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE sampler_checkpoint (
    source TEXT PRIMARY KEY NOT NULL CHECK (source IN ('gui', 'iod')),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    checkpoint_mono_ns INTEGER NOT NULL CHECK (checkpoint_mono_ns >= 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json))
) STRICT;

CREATE TABLE file_overflow_unknown (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0 AND ts_mono_ns % 3600000000000 = 0),
    owner_key TEXT NOT NULL CHECK (owner_key = 'overflow_owner_unknown'),
    overflow_files INTEGER NOT NULL CHECK (overflow_files >= 0),
    overflow_bytes_r INTEGER NOT NULL CHECK (overflow_bytes_r >= 0),
    overflow_bytes_w INTEGER NOT NULL CHECK (overflow_bytes_w >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, ts_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE boot_file_overflow_unknown (
    boot_id TEXT PRIMARY KEY NOT NULL REFERENCES boot(boot_id),
    owner_key TEXT NOT NULL CHECK (owner_key = 'overflow_owner_unknown'),
    overflow_files INTEGER NOT NULL CHECK (overflow_files >= 0),
    overflow_bytes_r INTEGER NOT NULL CHECK (overflow_bytes_r >= 0),
    overflow_bytes_w INTEGER NOT NULL CHECK (overflow_bytes_w >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json))
) STRICT;

CREATE TABLE collector_epoch (
    source TEXT PRIMARY KEY NOT NULL CHECK (source IN ('gui', 'iod')),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    last_seq INTEGER CHECK (last_seq IS NULL OR last_seq >= 0),
    allocated_mono_ns INTEGER NOT NULL CHECK (allocated_mono_ns >= 0)
) STRICT;

CREATE TABLE collector_interval (
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    from_mono_ns INTEGER NOT NULL CHECK (from_mono_ns >= 0),
    to_mono_ns INTEGER NOT NULL CHECK (to_mono_ns > from_mono_ns),
    PRIMARY KEY (source, boot_id, epoch)
) WITHOUT ROWID, STRICT;

CREATE TABLE handover (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    from_source TEXT NOT NULL CHECK (from_source IN ('gui', 'iod')),
    to_source TEXT NOT NULL CHECK (to_source IN ('gui', 'iod') AND to_source <> from_source),
    cutoff_mono_ns INTEGER NOT NULL CHECK (cutoff_mono_ns >= 0),
    baselines_json TEXT NOT NULL CHECK (json_valid(baselines_json)),
    PRIMARY KEY (boot_id, cutoff_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE clock_map (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    segment_start_mono_ns INTEGER NOT NULL CHECK (segment_start_mono_ns >= 0),
    wall_offset_ns INTEGER NOT NULL,
    PRIMARY KEY (boot_id, segment_start_mono_ns)
) WITHOUT ROWID, STRICT;

CREATE TABLE rollup_app (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    bucket_start_mono_ns INTEGER NOT NULL CHECK (bucket_start_mono_ns >= 0),
    bucket_secs INTEGER NOT NULL CHECK (bucket_secs IN (60, 3600)),
    read_bytes INTEGER NOT NULL CHECK (read_bytes >= 0),
    write_bytes INTEGER NOT NULL CHECK (write_bytes >= 0),
    rchar INTEGER NOT NULL CHECK (rchar >= 0),
    wchar INTEGER NOT NULL CHECK (wchar >= 0),
    peak_read_bps INTEGER CHECK (peak_read_bps IS NULL OR peak_read_bps >= 0),
    peak_write_bps INTEGER CHECK (peak_write_bps IS NULL OR peak_write_bps >= 0),
    peak_total_bps INTEGER CHECK (peak_total_bps IS NULL OR peak_total_bps >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs)
) WITHOUT ROWID, STRICT;

CREATE TABLE rollup_dev (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    bucket_start_mono_ns INTEGER NOT NULL CHECK (bucket_start_mono_ns >= 0),
    bucket_secs INTEGER NOT NULL CHECK (bucket_secs IN (60, 3600)),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    rbytes INTEGER NOT NULL CHECK (rbytes >= 0),
    wbytes INTEGER NOT NULL CHECK (wbytes >= 0),
    PRIMARY KEY (boot_id, app_id, bucket_start_mono_ns, bucket_secs, dev)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_total (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    read_bytes INTEGER NOT NULL CHECK (read_bytes >= 0),
    write_bytes INTEGER NOT NULL CHECK (write_bytes >= 0),
    rchar INTEGER NOT NULL CHECK (rchar >= 0),
    wchar INTEGER NOT NULL CHECK (wchar >= 0),
    peak_read_bps INTEGER CHECK (peak_read_bps IS NULL OR peak_read_bps >= 0),
    peak_write_bps INTEGER CHECK (peak_write_bps IS NULL OR peak_write_bps >= 0),
    peak_total_bps INTEGER CHECK (peak_total_bps IS NULL OR peak_total_bps >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, app_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_dev_total (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    dev TEXT NOT NULL CHECK (length(dev) > 0),
    rbytes INTEGER NOT NULL CHECK (rbytes >= 0),
    wbytes INTEGER NOT NULL CHECK (wbytes >= 0),
    PRIMARY KEY (boot_id, app_id, dev)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_file_total (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    path_id INTEGER NOT NULL REFERENCES path(path_id),
    bytes_r INTEGER NOT NULL CHECK (bytes_r >= 0),
    bytes_w INTEGER NOT NULL CHECK (bytes_w >= 0),
    reads INTEGER NOT NULL CHECK (reads >= 0),
    writes INTEGER NOT NULL CHECK (writes >= 0),
    last_seen_mono_ns INTEGER NOT NULL CHECK (last_seen_mono_ns >= 0),
    PRIMARY KEY (boot_id, app_id, path_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_boot_file_coverage (
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    app_id INTEGER NOT NULL REFERENCES app(app_id),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    PRIMARY KEY (boot_id, app_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE app_lifetime_total (
    app_id INTEGER PRIMARY KEY NOT NULL REFERENCES app(app_id) CHECK (app_id > 0),
    read_bytes INTEGER NOT NULL CHECK (read_bytes >= 0),
    write_bytes INTEGER NOT NULL CHECK (write_bytes >= 0),
    rchar INTEGER NOT NULL CHECK (rchar >= 0),
    wchar INTEGER NOT NULL CHECK (wchar >= 0),
    coverage_json TEXT NOT NULL CHECK (json_valid(coverage_json)),
    updated_boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    updated_mono_ns INTEGER NOT NULL CHECK (updated_mono_ns >= 0)
) STRICT;

CREATE TABLE gap (
    gap_id INTEGER PRIMARY KEY NOT NULL CHECK (gap_id > 0),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    from_mono_ns INTEGER NOT NULL CHECK (from_mono_ns >= 0),
    to_mono_ns INTEGER NOT NULL CHECK (to_mono_ns > from_mono_ns),
    reason TEXT NOT NULL CHECK (reason IN ('collector_gap', 'overlap_uncertain', 'gui_absent', 'quota_exhausted'))
) STRICT;

CREATE TABLE store_diagnostic (
    diagnostic_id INTEGER PRIMARY KEY NOT NULL CHECK (diagnostic_id > 0),
    boot_id TEXT NOT NULL REFERENCES boot(boot_id),
    source TEXT NOT NULL CHECK (source IN ('gui', 'iod')),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    app_id INTEGER REFERENCES app(app_id),
    ts_mono_ns INTEGER NOT NULL CHECK (ts_mono_ns >= 0),
    code TEXT NOT NULL CHECK (code = 'counter_overflow'),
    field TEXT NOT NULL CHECK (length(field) > 0),
    UNIQUE (boot_id, source, epoch, seq, field)
) STRICT;

CREATE INDEX app_name_idx ON app(name COLLATE NOCASE, key);
CREATE INDEX launch_app_time_idx ON launch(app_id, boot_id, started_mono_ns);
CREATE INDEX sample_app_time_idx ON sample(app_id, boot_id, ts_mono_ns, source, epoch, seq);
CREATE INDEX sample_coverage_app_time_idx ON sample_coverage(app_id, boot_id, ts_mono_ns, source, epoch, seq);
CREATE INDEX sample_dev_app_time_idx ON sample_dev(app_id, boot_id, ts_mono_ns, dev);
CREATE INDEX path_basename_idx ON path(basename COLLATE NOCASE, path_id);
CREATE INDEX path_alias_basename_idx ON path_alias(basename COLLATE NOCASE, path_id);
CREATE INDEX file_sample_app_time_idx ON file_sample(app_id, boot_id, ts_mono_ns, path_id);
CREATE INDEX file_sample_last_seen_idx ON file_sample(app_id, boot_id, last_seen_mono_ns DESC, path_id);
CREATE INDEX file_coverage_app_time_idx ON file_coverage(app_id, boot_id, ts_mono_ns, source, epoch, seq);
CREATE INDEX store_diagnostic_time_idx ON store_diagnostic(boot_id, ts_mono_ns, app_id);
CREATE INDEX collector_interval_time_idx ON collector_interval(boot_id, from_mono_ns, to_mono_ns);
CREATE INDEX rollup_app_time_idx ON rollup_app(app_id, boot_id, bucket_secs, bucket_start_mono_ns);
CREATE INDEX rollup_dev_time_idx ON rollup_dev(app_id, boot_id, bucket_secs, bucket_start_mono_ns, dev);
CREATE INDEX gap_time_idx ON gap(boot_id, from_mono_ns, to_mono_ns, source, epoch);

PRAGMA user_version = 1;
COMMIT;
"""
