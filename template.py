"""Rank OpenDota teams by the summed recorded history of their pro players.

Design decisions (the task asks for reasoning in comments and docstrings):
* "Top n teams (by team_id)": team_id groups players; teams rank by summed
  experience, ties to the lower team_id so output is deterministic.
* Experience = as_of - full_history_time in integer microseconds (datetime's own
  resolution). Nothing is rounded, so the ranking follows the exact definition and
  each team total equals the sum of its listed players.
* team_id 0/null means "no current team" (INFO, not an error); any other
  non-positive-integer team_id is malformed (WARNING). Players with unusable
  history (missing, unparseable, before 2011 or in the future) are excluded;
  their teammates still count.
* /proPlayers decides the ranking, so its failure fails the run. /teams/{id}
  only adds name/wins/losses/rating. If it fails, wins/losses/rating are null and
  the name comes from /proPlayers when unambiguous (the task: ignore missing
  fields that don't affect experience), with a warning.
* Fixed schema: every key always present, null when unknown, plus as_of and the
  unit because the numbers grow with time. A tiny fixed-schema YAML writer keeps
  the standard library the only dependency.
* This is the assignment-defined metric, not a claim about OpenDota's upstream
  field semantics or a measure of career length. Snapshot observations live in README.
"""

from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import logging
import math
from pathlib import Path
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
API_URL = "https://api.opendota.com/api"
REQUEST_TIMEOUT = 15  # Per socket operation, not a whole-request deadline.
REQUEST_INTERVAL = 1.0  # Free tier allows about 60 requests/minute (observed headers).
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 60
TRANSIENT_HTTP = (429, 500, 502, 503, 504)
EARLIEST_HISTORY = datetime(2011, 1, 1, tzinfo=timezone.utc)  # Dota 2's first public matches: 2011.
# Explicit formats accept the same inputs on Python 3.9-3.15; fromisoformat does not.
TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
TEAM_FIELDS = ("team_name", "team_id", "wins", "losses", "rating", "team_experience")
PLAYER_FIELDS = ("personaname", "player_experience", "country_code")


class ApiError(Exception):
    """API/data failure: message for people, optional HTTP status for control flow."""

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def retry_after_seconds(value, now):
    """Delay requested by a Retry-After header in whole seconds, or None.

    RFC 9110 allows delta-seconds or an HTTP-date; a past date means "now" (0).
    An absent or unparseable value returns None, so the caller keeps its backoff.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.isdigit():
            return int(value)
        return max(0, math.ceil((parsedate_to_datetime(value) - now).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


class ApiClient:
    """Sequential, paced GETs with finite retries; no cache, key or global state.

    Defaults talk to the live API in real time; tests pass a loopback URL and fake
    sleep/clock instead of patching module state.
    """

    def __init__(self, base_url=API_URL, *, interval=REQUEST_INTERVAL, sleep=time.sleep,
                 clock=time.monotonic):
        self.base_url, self.interval = base_url, interval
        self.sleep, self.clock = sleep, clock
        self.next_request = 0.0

    def get(self, path):
        """Return decoded JSON, retrying only failures a retry can fix."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.sleep(max(0.0, self.next_request - self.clock()))  # Pace every attempt.
            self.next_request = self.clock() + self.interval
            LOG.debug("GET %s (attempt %d/%d)", path, attempt, MAX_ATTEMPTS)
            delay = 2 ** (attempt - 1)
            status = None  # Only this attempt's failure determines the final status.
            try:
                return self._fetch(path)
            except HTTPError as error:
                if error.fp:  # Release the socket; hand-built errors may have none.
                    error.close()
                status = error.code
                cause = "HTTP %d" % status
                if status not in TRANSIENT_HTTP:
                    raise ApiError("%s: %s" % (path, cause), status=status) from error
                hint = error.headers.get("Retry-After") if error.headers else None
                requested = retry_after_seconds(hint, datetime.now(timezone.utc))
                if requested is not None:
                    if requested > MAX_RETRY_DELAY:  # Never retry earlier than the server asked.
                        raise ApiError("%s: %s, Retry-After %ds; try later" % (path, cause, requested),
                                       status=status) from error
                    delay = requested
            except URLError as error:
                cause = str(error.reason)
                # A missing trust store is permanent: retrying only hides the cause.
                if isinstance(error.reason, ssl.SSLCertVerificationError):
                    raise ApiError("%s: %s (install CA certificates)" % (path, cause)) from error
            except (OSError, HTTPException) as error:
                cause = "%s: %s" % (type(error).__name__, error)
            if attempt == MAX_ATTEMPTS:
                raise ApiError("%s: %s (after %d attempts)" % (path, cause, MAX_ATTEMPTS), status=status)
            LOG.info("%s: %s; retrying in %d s", path, cause, delay)
            self.sleep(delay)

    def _fetch(self, path):
        request = Request(self.base_url + path, headers={
            "Accept": "application/json", "User-Agent": "DotaExperienceWorkSample/2.0"})
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            # A whole read raises IncompleteRead (retried) on a cut body; read(n) would not.
            body = response.read()
        if not body.strip():  # Observed live: unknown team IDs return HTTP 200, empty body.
            raise ApiError("%s: empty response" % path)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError) as error:  # Deeply nested JSON raises RecursionError.
            raise ApiError("%s: invalid JSON response" % path) from error


def positive_id(value):
    """JSON booleans and numeric strings are not valid API identifiers."""
    return type(value) is int and value > 0


def usable_text(value):
    """Non-empty text without lone surrogates, which strict YAML readers reject."""
    return (isinstance(value, str) and bool(value)
            and not any(0xd800 <= ord(c) <= 0xdfff for c in value))


def parse_time(text):
    """Parse an offset-aware ISO 8601 timestamp; naive or date-only values fail."""
    for pattern in TIME_FORMATS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    raise ValueError("unsupported timestamp")


def eligible_players(rows, as_of):
    """Return the countable (team_id, start, row) records and how many rows were
    skipped per reason ("conflicts" counts excluded players, not rows).

    A row with unusable history excludes only that player. Among eligible rows,
    repeats of a valid account_id count once (first row's display fields); conflicts
    on team or history exclude the player. Ineligible copies are ignored before
    conflict checks, so who counts, with which team and history, never depends on
    row order.
    """
    records, owners = [], {}  # owners: valid account_id -> index into records, None if conflicting
    counts = dict(teamless=0, malformed=0, unusable=0, repeats=0)
    for row in rows:
        team_id = row.get("team_id") if isinstance(row, dict) else "malformed row"
        if team_id is None or (type(team_id) is int and team_id == 0):
            counts["teamless"] += 1  # Normal: the player has no current team.
            continue
        if not positive_id(team_id):
            counts["malformed"] += 1
            continue
        try:
            start = parse_time(row.get("full_history_time"))
            if not EARLIEST_HISTORY <= start <= as_of:
                raise ValueError("history before Dota 2 existed or in the future")
        except (TypeError, ValueError):
            counts["unusable"] += 1
            continue
        account = row.get("account_id")  # Only valid IDs deduplicate: [] is unhashable, True == 1.
        if positive_id(account) and account in owners:
            counts["repeats"] += 1
            first = owners[account]
            if first is not None and records[first][:2] != (team_id, start):
                records[first] = owners[account] = None  # Conflicting rows: exclude the player.
            continue
        if positive_id(account):
            owners[account] = len(records)
        records.append((team_id, start, row))
    counts["conflicts"] = list(owners.values()).count(None)
    return [record for record in records if record], counts


def select_teams(rows, count, as_of):
    """Rank teams by the summed experience of their eligible players; return the top
    `count` in the fixed output schema, with wins/losses/rating still null.

    A team's total is the experience of its known eligible players, never an estimate.
    """
    if not isinstance(rows, list):
        raise ApiError("/proPlayers: expected a JSON array")
    records, counts = eligible_players(rows, as_of)
    teams = {}  # Built only from kept rows, so excluded rows cannot lend a team name.
    for team_id, start, row in records:
        team = teams.setdefault(team_id, {"names": set(), "players": []})
        if usable_text(row.get("team_name")):
            team["names"].add(row["team_name"])
        team["players"].append({
            "personaname": row["personaname"] if usable_text(row.get("personaname")) else None,
            "player_experience": (as_of - start) // timedelta(microseconds=1),
            "country_code": row["country_code"] if usable_text(row.get("country_code")) else None,
        })
    LOG.info("%d of %d player records have no team_id (0 or null); not ranked",
             counts["teamless"], len(rows))
    if counts["repeats"]:
        LOG.info("%d rows repeat an account_id already seen", counts["repeats"])
    if counts["malformed"]:
        LOG.warning("Skipped %d malformed records or team_id values", counts["malformed"])
    if counts["unusable"]:
        LOG.warning("Excluded %d players with missing or unusable full_history_time; "
                    "their teams rank on the remaining players", counts["unusable"])
    if counts["conflicts"]:
        LOG.warning("Excluded %d players whose repeated rows disagree on team or history",
                    counts["conflicts"])
    if not teams:  # An empty ranking is never a valid answer: likely an API change.
        raise ApiError("/proPlayers: no eligible teams in %d records" % len(rows))
    totals = {tid: sum(p["player_experience"] for p in t["players"]) for tid, t in teams.items()}
    selected = sorted(totals, key=lambda tid: (-totals[tid], tid))[:count]
    if len(selected) < count:
        LOG.warning("Requested %d teams; only %d eligible teams exist", count, len(selected))
    LOG.info("Ranked %d teams; fetching details for %d (about %d s at the free-tier pace)",
             len(teams), len(selected), len(selected) * REQUEST_INTERVAL)
    result = []
    for tid in selected:
        names = teams[tid]["names"]
        players = sorted(teams[tid]["players"], key=lambda p: (
            -p["player_experience"], p["personaname"] or "", p["country_code"] or ""))
        result.append({"team_name": names.pop() if len(names) == 1 else None,  # unambiguous only
                       "team_id": tid, "wins": None, "losses": None, "rating": None,
                       "team_experience": totals[tid], "players": players})
    return result


def build_report(client, count, as_of):
    """Rank from /proPlayers first; team details never change the ranking."""
    teams = select_teams(client.get("/proPlayers"), count, as_of)
    missing, limited = {}, False
    for team in teams:
        try:
            if limited:  # Conservatively avoid further quota use after unresolved rate limiting.
                raise ApiError("skipped after HTTP 429", status=429)
            details = client.get("/teams/%d" % team["team_id"])
            if not isinstance(details, dict):
                raise ApiError("not a JSON object")
            identity = details.get("team_id")
            # Missing identity is allowed; an explicit invalid/conflicting one is not.
            if identity is not None:
                if not positive_id(identity):
                    raise ApiError("invalid team_id in team details")
                if identity != team["team_id"]:
                    raise ApiError("team_id mismatch in team details")
        except ApiError as error:
            limited = limited or error.status == 429
            missing.setdefault(str(error).split(": ", 1)[-1], []).append(team["team_id"])
            continue
        if usable_text(details.get("name")):  # The endpoint name beats the snapshot name.
            team["team_name"] = details["name"]
        for field in ("wins", "losses"):
            if type(details.get(field)) is int and details[field] >= 0:
                team[field] = details[field]
        rating = details.get("rating")
        if type(rating) is int or (type(rating) is float and math.isfinite(rating)):
            team["rating"] = rating
    if missing:
        LOG.warning("No team details for %d of %d selected teams (%s); their wins, losses and "
                    "rating are null", sum(map(len, missing.values())), len(teams),
                    "; ".join("%s: %d" % (cause, len(ids)) for cause, ids in missing.items()))
        LOG.info("Teams without details: %s", missing)
    return {"as_of": as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "experience_unit": "microseconds", "teams": teams}


def yaml_scalar(value):
    """Encode one fixed-schema scalar. JSON quoting is YAML double-quoting, except
    YAML also breaks lines on NEL/LS/PS and forbids C1 and U+FFFE/U+FFFF: escape them."""
    if value is None:
        return "null"
    if isinstance(value, str):
        value.encode("utf-8")  # Defensive: lone surrogates are not valid scalars.
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, float) and "e" in text and "." not in text.split("e")[0]:
        text = text.replace("e", ".0e")  # YAML 1.1 readers need a dot to see a float.
    return "".join("\\u%04x" % ord(c) if (0x7f <= ord(c) <= 0x9f or c in "\u2028\u2029\ufffe\uffff")
                   else c for c in text)


def render_yaml(report):
    """Emit only this fixed schema; deliberately not a general YAML serializer."""
    lines = ["as_of: " + yaml_scalar(report["as_of"]),
             "experience_unit: " + yaml_scalar(report["experience_unit"]), "teams:"]
    def item(record, fields, indent):  # One block-sequence entry of "key: scalar" lines.
        for index, key in enumerate(fields):
            lines.append(indent + ("- " if index == 0 else "  ") + key + ": " + yaml_scalar(record[key]))
    for team in report["teams"]:
        item(team, TEAM_FIELDS, "  ")
        lines.append("    players:")
        for player in team["players"]:
            item(player, PLAYER_FIELDS, "      ")
    return "\n".join(lines) + "\n"


def write_output(text, destination):
    """Write the finished report once, as UTF-8 regardless of the locale.

    Fetching/computation/rendering failures leave existing output untouched;
    optional detail failures are handled before this point. A plain open() keeps
    device, FIFO, link and permission semantics. A disk-full or kill
    mid-write can leave a partial file: accepted for a few-KB report.
    """
    data = text.encode("utf-8")
    if destination == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    with open(destination, "wb") as stream:
        stream.write(data)


def parse_args(argv=None):
    """Keep the template's arguments, defaults and help text; validate before any request.

    Deliberate differences: the template's FileType("w") opens and truncates the
    destination while parsing, so a run that later failed would destroy an existing
    report; here the path stays text and is opened once the report is complete
    (write_output). -n must be positive, and -l also accepts lowercase.
    """
    parser = ArgumentParser(description="Find the DOTA 2 teams with the most combined player *experience",
                            epilog="*Experience is defined as the length of a player's recorded history. "
                                   "Values are reported in microseconds.")
    parser.add_argument("output", nargs="?", default="-",
                        help="YAML destination; '-' or omitted writes to stdout")
    parser.add_argument("-n", "--num-teams", type=int, default=5,
                        help="number of teams in output (default: %(default)s)")
    parser.add_argument("-l", "--loglevel", type=str.upper,
                        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"], default="WARNING",
                        help="Only output log messages of this severity or above. Writes to stderr. "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)
    if args.num_teams <= 0:
        parser.error("--num-teams must be a positive integer")
    if args.output != "-" and (Path(args.output).is_dir() or not Path(args.output).parent.is_dir()):
        parser.error("output must be a file in an existing directory: %s" % args.output)
    return args


def main(argv=None, client=None):
    """Run the CLI. `client` defaults to the live API; tests inject a loopback one."""
    args = parse_args(argv)
    logging.basicConfig(level=args.loglevel, stream=sys.stderr, format="%(levelname)s: %(message)s")
    as_of = datetime.now(timezone.utc)  # One reference time for all players.
    try:
        report = build_report(ApiClient() if client is None else client, args.num_teams, as_of)
        write_output(render_yaml(report), args.output)
    except (ApiError, OSError, UnicodeError) as error:
        LOG.error("%s", error)
        return 1
    LOG.info("Wrote %d teams", len(report["teams"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
