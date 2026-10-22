# Known defects and deferred findings

No open items. Everything carried over from the reviews that ran while this
code lived in `xraydc` (as `src/xraydc/io/`), plus what the extraction review
added, has since been fixed and is now covered by a test — see
`test_discover_scans_raises_rather_than_swallows_a_dropped_share`,
`test_discover_scans_dedupes_the_same_day_under_two_year_folders`,
`test_manifest_skip_also_checks_root_not_just_source`,
`test_base_route_without_from_is_refused`,
`test_format_mapping_table_line_shows_root_when_tracked`,
`test_no_temperature_file_is_silent` (asserts `caplog.records == []`, not a
keyword), and the `tools.thin_corpus` validation/`--clean` tests in
`tools_test.py`, among others.

One item was a documented limitation rather than a bug and stays that way:
a proposal source path containing a literal `=` breaks nx5d's own argument
parser (it splits every argv element on `=` with no `maxsplit` to tell a
positional from a `key=value` kwarg) before `pxs_import` ever sees it. Not
fixable from this side of the string-resolved contract; noted in
`scans_from_pxs`'s `source` docstring ([reader.py](src/pxs_import/reader.py)).

When a new defect or deferred finding turns up, record it here the same way:
one bullet, the mechanism, and why it matters — not just a symptom.
