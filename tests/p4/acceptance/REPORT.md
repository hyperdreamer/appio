# Files-tab real-store acceptance — package and result

## Why this package exists

The Files tab is only correct if it serves a real interval from a store that predates the code under
test. A synthetic fixture can be shaped to fit the implementation, so the acceptance runs against a
frozen copy of this host's live daemon store. Everything needed to reproduce and re-check that run is
committed here:

| File | Role |
| --- | --- |
| `capture_store.py` | reproduces the capture (`sqlite3 .backup`) and re-verifies its digest |
| `capture.json` | provenance: source, method, digest, boot, watermark, row counts |
| `oracle.py` | independent expectations, stdlib only, no `appio` import |
| `oracle-out.json` | the oracle's committed output for this capture |
| `REPORT.md` | this record |
| `../test_files_tab_real_store.py` | the acceptance test |

The capture itself is **not** committed: it is 1.4 GB. It is reproducible byte-for-byte only from the
same store, which is why its digest is pinned in both `capture.json` and `oracle-out.json` and checked
before and after every run.

## The capture

```
snapshot  /tmp/appio-files-tab-capture/iod.sqlite   1,497,067,520 bytes
sha256    5eea10690422e3eba12e3376044c953dfcfe1ae3657bf13e34cb42d5bfe891e6
boot      c753ab5f-87a1-4285-9c4e-d749ce9ac758
watermark file_finalized_through = 216,000,000,000,000 ns (hour 60)
rows      file_sample 6163 · file_coverage 1112 · app_boot_file_total 2942 · gap 1009 · sample 1,332,567
```

Independently derived expectations (`oracle-out.json`):

```
file-bearing hours                 46, 48, 49, 50, 51, 57, 58, 59
expected served interval           [48h, 52h)  = 172,800,000,000,000 .. 187,200,000,000,000 ns
gaps inside that interval          12
apps with rows in that interval    32
rows in that interval              3169
```

## Result

```
view state        incomplete
rows rendered     156 (for the selected application)
apps in selector  32
served interval   [2d 0h, 2d 4h) after boot
requested window  since_boot through 2d 12h
coverage          complete=False, both gap figures reported, served count not None
disclosure        "rows retained; served interval only: T0=2d 0h through T1=2d 4h after boot;
                   requested window: since_boot through T1=2d 12h after boot"
```

## Falsification

Restoring the pre-pivot rule (unproved segments must be hour-aligned) makes this test fail with
`File ownership cannot be represented by one store` / `assert 'unavailable' == 'incomplete'`. Restored,
it passes. The pin therefore bites on the mechanism it claims to protect, and on this store the
pre-pivot rule made the tab unservable — 12 gaps inside `[48h,52h)` and 41 inside `[57h,60h)`, none
with hour-aligned endpoints.

## Modes

- default suite: absent capture → **skip**, labelled, and visibly not a pass;
- acceptance run: `APPIO_ACCEPTANCE_REQUIRED=1` → absent or drifted capture is a **failure**.

```
python tests/p4/acceptance/capture_store.py verify /tmp/appio-files-tab-capture
APPIO_ACCEPTANCE_REQUIRED=1 python -m pytest -q tests/p4/test_files_tab_real_store.py
```
