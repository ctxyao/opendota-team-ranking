# The problem this code solves

> **Note on this document.** The original exercise was distributed as a hiring
> work sample. This is a paraphrased restatement of its requirements, written to
> describe the problem without reproducing the source document or identifying the
> company that issued it. The requirements below are complete enough to
> understand, run and evaluate the solution in this repository.

## Background

[OpenDota](https://docs.opendota.com/) publishes a free, public, read-only HTTP
API for Dota 2 data. The relevant parts for this exercise are two endpoints:

| Endpoint | Returns |
| --- | --- |
| `/proPlayers` | a JSON array of professional players, one object each |
| `/teams/{team_id}` | a single team object, or an empty body for unknown IDs |

The API is usable without an account or API key on its free tier. It is also
irregular: some fields are missing, some values are of unexpected types, and some
known team IDs return `HTTP 200` with an empty body.

## Definitions

The exercise fixes two definitions that the implementation must follow:

- **Player experience** is the amount of time that has passed since the start of
  that player's recorded history. The start time is available as the player's
  `full_history_time` field.
- **Team experience** is the sum of the experience of all professional players
  that belong to the same team, grouped by `team_id`.

## What to build

A Python 3 command-line script that:

1. Starts from the `/proPlayers` endpoint and queries `/teams/{team_id}` as
   needed for additional team data.
2. Takes **three or more arguments**: the number of teams to include in the
   output, the output file destination, and the logging verbosity.
3. Uses Python's `logging` module, writing log messages directly to stderr.
4. Writes **YAML** for the top *n* teams by `team_id`, sorted by team experience,
   containing for each team:
   - team name, team ID, wins, losses, rating, team experience
   - and for each player on that team: personaname, player experience, country code.

## Guidance the exercise gave

- **On bugs and missing data.** "There will be some inconsistencies and missing
  data in the API's responses. If the missing data compromises the team
  experience calculations, exclude the offending teams or players. Otherwise,
  simply ignore the missing fields."
- **On unexplained decisions.** "When making design decisions for issues not
  acknowledged in this prompt, use your best judgement and explain your reasoning
  in comments and docstrings."
- **On dependencies.** Open-source packages were permitted, with dependencies
  declared in any conventional format. The team that issued the exercise also
  stated a preference for a simple, complete and correct solution over a complex
  one, and for a minimal environment with fewer third-party libraries.
- **On the argument parser.** A sample argument-parsing skeleton was supplied,
  and replacing or rewriting it was explicitly permitted. The interface it
  defined — a positional output destination defaulting to stdout, `-n/--num-teams`
  defaulting to 5, and `-l/--loglevel` defaulting to `WARNING` — is preserved by
  this solution.

## How this repository answers it

See [`README.md`](./README.md) for usage, the output contract, and the policy for
every kind of missing or inconsistent data. In short:

- A single 380-line file using only the Python standard library.
- Integer microseconds for experience, so nothing is rounded and every team total
  is exactly the sum of its players.
- `/proPlayers` decides the ranking; `/teams/{id}` only adds supplementary fields
  and never changes the order.
- Every missing value is written as `null`, never invented or filled in.
- `test_template.py` holds 61 tests, including eight end-to-end runs against a
  local HTTP server, so the whole suite runs offline without an API key.

## Data note

This repository deliberately contains **no captured API responses**. The test
suite generates its own fixtures and serves them from a loopback HTTP server, so
nothing here depends on a stored snapshot, and no data about real players is
redistributed.
