"""Timezone and measurement quality analysis; never fills gaps or invents units."""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from statistics import median
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc


def day_bounds(day, zone="Europe/Madrid"):
    """Half-open local civil day: 23, 24 or 25 hours, calculated in UTC."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time(), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def sample_time(value, source_zone, day=None):
    """Interpret explicit UTC/offsets or documented local dates; reject DST ambiguity."""
    if not isinstance(value, str) or not value:
        return None, "MISSING_TIME"
    try:
        if value.isascii() and value.isdigit() and len(value) == 10:
            return datetime.fromtimestamp(int(value), UTC), None
        text = value
        if day and len(text) in {5, 8} and ":" in text:
            text = day + "T" + text
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC), None
        if not source_zone:
            return None, "SOURCE_TIMEZONE_UNAVAILABLE"
        tz = ZoneInfo(source_zone)
        candidates = set()
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=tz, fold=fold).astimezone(UTC)
            if candidate.astimezone(tz).replace(tzinfo=None) == parsed:
                candidates.add(candidate)
        if not candidates:
            return None, "NONEXISTENT_LOCAL_TIME"
        if len(candidates) > 1:
            return None, "AMBIGUOUS_LOCAL_TIME"
        return candidates.pop(), None
    except (ValueError, OverflowError, OSError, KeyError):
        return None, "INVALID_TIME"


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        converted = float(number) if number.is_finite() else None
        return converted if converted is not None and math.isfinite(converted) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def daily_date_quality(values, first, last):
    """A server total matching its list length does not mean all requested days exist."""
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    expected = {str(first + timedelta(days=i)) for i in range((last - first).days + 1)}
    counts, issues = Counter(), Counter()
    for value in values:
        try:
            day = str(date.fromisoformat(value))
        except (ValueError, TypeError):
            issues["INVALID_OR_MISSING_DATE"] += 1
            continue
        if day not in expected:
            issues["DATE_OUTSIDE_REQUESTED_INTERVAL"] += 1
            continue
        counts[day] += 1
    return {"requested_dates": sorted(expected), "returned_dates": sorted(counts),
        "missing_dates": sorted(expected - counts.keys()),
        "duplicate_dates": sorted(key for key, count in counts.items() if count > 1),
        "issues": dict(issues)}


def extract_device_samples(data, requested_keys, source_zone, day):
    """Both documented string-map arrangements, restricted to requested measures."""
    samples, issues = [], Counter()
    for row in data.get("dataList") or []:
        stamp, error = sample_time(row.get("time"), source_zone, day)
        if stamp is not None and source_zone and day:
            left, right = day_bounds(day, source_zone)
            if not left <= stamp.timestamp() < right:
                stamp, error = None, "OUTSIDE_REQUESTED_DAY"
        if error:
            issues[error] += 1
        measurements = {}
        for item in row.get("itemList") or []:
            if item is None:
                issues["NULL_MEASUREMENT"] += 1
                continue
            if "key" in item:
                if item.get("key") in requested_keys:
                    measurements[item["key"]] = (item.get("value"), item.get("unit"))
            else:
                for key in requested_keys:
                    if key in item:
                        measurements[key] = (item[key], None)
        for key in requested_keys:
            raw, unit = measurements.get(key, (None, None))
            samples.append({"timestamp": stamp.isoformat() if stamp else None,
                "raw_time": row.get("time"), "key": key, "value": numeric(raw),
                "raw_value": raw, "unit": unit})
    return samples, dict(issues)


def analyze_samples(samples, start, end, *, expected_step=300):
    """Sample averages are explicitly not energy or averages over unobserved time."""
    issues, buckets, metric_times = Counter(), defaultdict(list), defaultdict(list)
    seen = set()
    units = defaultdict(set)
    missing = Counter()
    for sample in samples:
        key, value = sample["key"], sample.get("value")
        if sample.get("timestamp") is None:
            issues["UNRESOLVED_TIME"] += 1
            continue
        stamp = datetime.fromisoformat(sample["timestamp"]).astimezone(UTC)
        seconds = stamp.timestamp()
        if not start <= seconds < end:
            issues["OUTSIDE_REQUESTED_INTERVAL"] += 1
            continue
        identity = (seconds, key)
        if identity in seen:
            issues["DUPLICATE_TIMESTAMP_AND_KEY"] += 1
            continue
        seen.add(identity)
        if value is None:
            missing[key] += 1
            continue
        if key in {"SOC", "BMSSOC", "batterySOC"} and not 0 <= value <= 100:
            issues["SOC_OUT_OF_RANGE"] += 1
            continue
        unit = sample.get("unit")
        if unit:
            units[key].add(unit)
        else:
            issues["MISSING_UNIT"] += 1
        local = stamp.astimezone(MADRID)
        # Keep the UTC offset: the repeated autumn hour has two different labels.
        hour = local.replace(minute=0, second=0, microsecond=0).isoformat()
        buckets[(hour, key, unit)].append((seconds, value))
        metric_times[key].append(seconds)
    hours = []
    for (hour, key, unit), pairs in sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1], str(x[0][2]))):
        pairs.sort()
        values = [value for _, value in pairs]
        # Difference is an observed counter span only; never fabricate boundary values.
        counter_span = None
        reset = False
        if unit == "kWh":
            reset = any(b[1] < a[1] for a, b in zip(pairs, pairs[1:]))
            if reset:
                issues["COUNTER_DECREASE_WITHIN_HOUR"] += 1
            elif len(pairs) >= 2:
                counter_span = values[-1] - values[0]
        hours.append({"hour_madrid": hour, "key": key, "unit": unit,
            "samples": len(pairs), "min": min(values), "max": max(values),
            "sample_mean": sum(values) / len(values),
            "first_sample_utc": datetime.fromtimestamp(pairs[0][0], UTC).isoformat(),
            "last_sample_utc": datetime.fromtimestamp(pairs[-1][0], UTC).isoformat(),
            "observed_span_seconds": pairs[-1][0] - pairs[0][0],
            "counter_change_observed_kwh": counter_span,
            "complete_hour_energy_kwh": None})
    coverage = {}
    for key, times in metric_times.items():
        ordered = sorted(set(times))
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        # All missing time counts, including leading/trailing and gaps inside window.
        boundaries = [ordered[0] - start] + gaps + [end - ordered[-1]]
        coverage[key] = {"samples": len(ordered), "missing_values": missing[key],
            "first_utc": datetime.fromtimestamp(ordered[0], UTC).isoformat(),
            "last_utc": datetime.fromtimestamp(ordered[-1], UTC).isoformat(),
            "median_step_seconds": median(gaps) if gaps else None,
            "max_gap_seconds": max(boundaries),
            "gaps_over_2_expected_steps": sum(gap > 2 * expected_step for gap in boundaries),
            "expected_samples_at_5_minutes": (end - start) // expected_step,
            "observed_units": sorted(units[key]),
            "unit_consistent": len(units[key]) == 1 if units[key] else None}
        if len(units[key]) > 1:
            issues["INCONSISTENT_UNIT:" + key] += 1
    return {"coverage": coverage, "issues": dict(issues), "hours": hours,
            "missing_by_key": dict(missing), "fills_or_interpolations": 0,
            "power_integrated_to_energy": False}
