"""Version-one cron dialect over croniter, with explicit wall-clock DST policy."""
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, CroniterError, croniter

from .errors import CoreError


_PART = re.compile(r"(\*|[0-9]+|[a-z]{3})(?:-([0-9]+|[a-z]{3}))?(?:/([0-9]+))?")
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = {
    3: {name: value for value, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)},
    4: {name: value for value, name in enumerate("sun mon tue wed thu fri sat".split())},
}


def _expression(expression):
    if not isinstance(expression, str) or len(expression.encode("utf-8")) > 256 or "\0" in expression:
        raise ValueError()
    fields = expression.lower().split()
    if len(fields) != 5:
        raise ValueError()
    compiled = []
    for index, field in enumerate(fields):
        minimum, maximum = _BOUNDS[index]
        values = []
        for part in field.split(","):
            match = _PART.fullmatch(part)
            if match is None:
                raise ValueError()
            start, end, step = match.groups()
            if step is not None and int(step) <= 0:
                raise ValueError()
            def number(value):
                result = int(value) if value.isascii() and value.isdigit() else _NAMES.get(index, {}).get(value, -1)
                if not minimum <= result <= maximum:
                    raise ValueError()
                return result
            if start == "*":
                if end is not None:
                    raise ValueError()
                value = "*"
            else:
                start = number(start)
                end = number(end) if end is not None else None
                if end is not None and end < start:
                    raise ValueError()
                value = str(start) + ("-" + str(end) if end is not None and end != start else "")
                # croniter 6.2.4 expands equal ranges to a full field and aliases
                # bare DOW 7/step to 0/step. Both are singletons in this dialect.
                if end == start or end is None and start == maximum:
                    step = None
            values.append(value + ("/" + str(int(step)) if step is not None else ""))
        compiled.append(",".join(values))
    # Non-strict expansion checks mature-parser syntax without incorrectly rejecting
    # impossible DOM combined with valid DOW: those are separate OR branches below.
    croniter.expand(" ".join(compiled))
    return " ".join(fields), compiled


def normalize_expression(expression):
    try:
        return _expression(expression)[0]
    except (CroniterError, ValueError, TypeError, OverflowError):
        raise CoreError("CRON_INVALID") from None


def next_due(expression, timezone="Europe/Moscow", *, after_utc):
    try:
        _, fields = _expression(expression)
        if (not isinstance(timezone, str) or not timezone or "\0" in timezone
                or not isinstance(after_utc, datetime) or after_utc.utcoffset() is None):
            raise ValueError()
        timezone.encode("utf-8")
        zone = ZoneInfo(timezone)
        after = after_utc.astimezone(UTC)
        wall_start = after.astimezone(zone).replace(tzinfo=None, fold=0)
        branches = [fields]
        if fields[2] != "*" and fields[4] != "*":
            # Native day_or=True can abandon both branches when DOM is impossible
            # (e.g. February 31 OR Monday). Let native calendars solve each branch.
            branches = [[*fields[:4], "*"], [*fields[:2], "*", *fields[3:]]]
        matches = []
        for branch in branches:
            iterator = croniter(" ".join(branch), wall_start, day_or=False, max_years_between_matches=8)
            try:
                while True:
                    wall = iterator.get_next(datetime)
                    if wall.year > wall_start.year + 8:
                        break
                    candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
                    if candidate.astimezone(zone).replace(tzinfo=None) != wall:
                        continue  # Nonexistent wall time; never shift it into the gap's end.
                    if candidate > after:
                        matches.append(candidate)
                        break
            except (CroniterBadDateError, ValueError, OverflowError):
                # A branch may exhaust Python's representable calendar before eight
                # years; another OR branch can still have a valid earlier match.
                continue
        if matches:
            return min(matches)
        raise ValueError()
    except (CroniterError, ZoneInfoNotFoundError, ValueError, TypeError, OverflowError):
        raise CoreError("CRON_INVALID") from None
