# OpenDota team experience

A small command-line script that ranks Dota 2 teams by the combined history of
their pro players, using OpenDota's `/proPlayers` and `/teams/{id}` endpoints.

It needs Python 3.9 or newer and nothing else: no packages, no API key. I tested
it on CPython 3.9 through 3.15, including the 3.9.6 that ships with macOS.

## Usage

```sh
python3 template.py teams.yaml -n 5 -l INFO   # write a file; logs go to stderr
python3 template.py -n 3                      # print the YAML to stdout
python3 -m unittest -v                        # offline tests; uses loopback HTTP
```

I kept the template's three arguments: an optional output path (`-` or nothing
means stdout), `-n/--num-teams` (default 5) and `-l/--loglevel` (default
WARNING). Small changes: `-n` must be positive, the log level isn't
case-sensitive, and arguments are checked before any request. Exit codes are 0
on success (even with fewer teams than you asked for), 1 if the report couldn't
be produced or written, and 2 for bad arguments.

The output file is opened only after fetching and rendering succeed, so errors
before writing starts leave an existing report untouched. The template's
`FileType("w")` truncates the file during argument parsing; shell redirection
(`> teams.yaml`) opens it before the script runs. Pass the path as an argument
to avoid that. Output is always UTF-8.

If you see `CERTIFICATE_VERIFY_FAILED`, Python couldn't verify the server
certificate. On macOS, a missing CA bundle is a common cause. For python.org
builds, run the supplied `Install Certificates.command`; if that doesn't help,
check the reported certificate error. TLS verification stays on.

## Output

`example.yaml` shows the format with made-up data. Every field the task asks for
is always present, plus `as_of` (the UTC time experience is measured against)
and `experience_unit`. Teams come best first. Anything unknown is `null` rather
than guessed, and a real `0` stays `0`.

Experience is a whole number of microseconds, the finest unit Python's
`datetime` has, so nothing is rounded and each team total is exactly the sum of
its players.

## How I read the task

- Players are grouped by `team_id`, and teams are sorted by team experience,
  highest first. Ties go to the lower `team_id`.
- A player's experience is `as_of - full_history_time`; a team's is the sum over
  its players. I don't round per player, since that can reorder teams that are
  within a second per player of each other.
- `/teams/{id}` is only fetched for the teams that make the cut: at most `1 + n`
  requests when nothing needs a retry.
- Since team experience is a sum, bigger rosters tend to score higher. That's
  the task's definition, so I left it alone.
- Some choices are mine, not the task's: microsecond precision, always writing
  every field, treating an empty ranking as an error, and ignoring timestamps
  before 2011, the year of Dota 2's first public matches.

## Missing and inconsistent data

| Case | What happens | Test |
| --- | --- | --- |
| `team_id` is 0 or null | No current team: not ranked, counted at INFO | `test_teamless_and_malformed_rows_are_not_ranked` |
| Any other invalid `team_id` (text, float, bool, negative), or a row that isn't an object | Not ranked. One WARNING with the count, since it may mean the API changed | `test_malformed_team_ids_warn_but_teamless_and_identical_rows_only_inform` |
| History missing, without a timezone, date-only, unparseable, before 2011 or in the future | Player left out; teammates still count | `test_unusable_history_excludes_player_but_keeps_teammates` |
| Timestamp variants (`Z`, `+00:00`, `+0000`, 0 to 6 fractional digits) | Accepted the same way on every supported Python | `test_timestamp_formats_identical_on_every_python_version` |
| Same `account_id` more than once | Counted once. Copies that disagree on team or history drop the player, in any order. Name and country come from the first usable copy | `test_repeated_account_counted_once`, `test_conflicting_repeats_exclude_the_player_in_any_order` |
| One copy of a player is usable, another isn't | The unusable copy is ignored and can't knock out the good one | `test_ineligible_duplicate_does_not_discard_an_eligible_player` |
| Invalid `account_id` (bool, float, text, list, object) | Player still counts; the ID just isn't used to spot duplicates | `test_invalid_account_ids_neither_crash_nor_hide_valid_players` |
| Persona or country missing or invalid | `null`; player still counts | `test_fixed_schema_with_null_for_missing_display_fields` |
| `/teams/{id}` fails (HTTP error, bad JSON, wrong shape) | That team's `wins`, `losses` and `rating` are `null`; ranking unchanged. One warning sums up the causes | `test_team_detail_failure_nulls_only_that_team` |
| Team details with no `team_id`, or the right one | Used | `test_matching_or_missing_detail_id_keeps_available_metadata` |
| Team details with a different or invalid `team_id` | Ignored with a warning, so another team's record can't leak in | `test_conflicting_or_invalid_detail_id_rejects_metadata_not_ranking` |
| No usable name from `/teams/{id}` | The `/proPlayers` team name if there's only one, otherwise `null` | `test_snapshot_team_name_used_only_when_unambiguous` |
| Odd numbers in team details | `wins` and `losses` must be non-negative integers. `rating` can be any finite number (negatives too), since I don't know its range. Booleans are rejected | `test_invalid_metadata_values_stay_null`, `test_negative_finite_rating_is_preserved_but_negative_counts_are_not` |
| `/proPlayers` fails or isn't a list | Exit 1, nothing written | `test_primary_failure_preserves_existing_output` |
| No team qualifies | Exit 1, nothing written: an empty ranking most likely means the data changed shape | `test_schema_drift_exits_1_without_output` |

In the snapshot I worked from (2026-10-20), 1,257 of 5,701 players had no team,
8 had unusable history, and 3 of the top 5 teams returned an empty body from
`/teams/{id}`.

## Design notes

More detail is in the comments in `template.py`.

- **One file, standard library only.** The YAML writer is about 30 lines and
  only handles this one format, including escaping the line breaks (NEL, LS, PS)
  that JSON leaves alone. If the format had to change shape, I'd switch to
  PyYAML rather than grow it.
- **Requests.** One at a time, at most one per second, which matches the free
  tier's limit of roughly 60 a minute. Timeouts, dropped connections, bodies
  that stop before their declared end, and HTTP 429/500/502/503/504 get up to
  three attempts. A `Retry-After` of up to 60 seconds is honored; a longer one
  fails that request. Certificate errors fail right away.
- **Rate limits.** If a 429 doesn't clear up, I stop asking for team details for
  the rest of the run. Those teams get `null` details, and the ranking is
  already done. This might skip a few that would have worked, but it saves
  quota.
- **Time limits.** The 15-second timeout is per socket operation, not per run,
  and `-n` has no cap. Asking for every team (about 2,400 in my snapshot) means
  about 40 minutes of detail requests; the plan is logged at INFO first.
- **Memory.** Responses are read in one go: `/proPlayers` is about 5 MB, and a
  normal run peaked around 70 MiB in my tests. There's no size cap. A full
  `read()` lets Python detect incomplete responses framed by `Content-Length`
  or chunked transfer encoding. Without that framing, an early disconnect can
  look like a normal end of response. A huge response can use a lot of memory.
- **Files.** No cache or temp files. A plain `open()` keeps `/dev/stdout`, FIFOs,
  symlinks, hard links and permissions working. A full disk or a kill mid-write
  can leave a partial file, which is fine for a report this small.
- **Logging.** WARNING means something is missing from the output, one line per
  kind of problem. Counts and team IDs go to INFO, each request to
  DEBUG.

## Tests

`test_template.py` has 61 tests covering the ranking rules, HTTP retries and
errors, the YAML output and the command line. Eight run `main()` in a separate
process against a local HTTP server. One feeds in 300 seeded batches of malformed rows and
checks that no batch is rejected, shuffling never changes the ranking, and
totals add up. Two cross-check the YAML with PyYAML and are skipped if it isn't
installed.

I also did a live `-n 2` run on 2026-10-20; the output parsed cleanly with both
PyYAML and Ruby's Psych.

Files: `template.py` (the script), `test_template.py`, `example.yaml`,
`requirements.txt` (lists no dependencies) and this README.
