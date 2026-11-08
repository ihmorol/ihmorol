#!/usr/bin/env python3
"""Render GitHub's last 30 daily contribution counters as light/dark SVGs.

Requires GITHUB_TOKEN for the API, or --input-json with a captured GraphQL
response (for example, from gh api graphql). Fixtures need no token. Only
calendar dates and aggregate counters are queried; no repository details.
"""

import argparse
import html
import json
import os
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError


QUERY = """query($username: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $username) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}"""
FILENAMES = ("github-contribution-graph.svg", "github-contribution-graph-dark.svg")
COLORS = {
    "light": ("#F8FAFC", "#0F172A", "#475569", "#CBD5E1", "#0F766E"),
    "dark": ("#0F172A", "#E2E8F0", "#94A3B8", "#334155", "#2DD4BF"),
}


class ContributionError(Exception):
    """An actionable error that is safe to print without response details."""


def iso_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Expected YYYY-MM-DD.")
    return parsed


def decode_json(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ContributionError("GitHub response is not valid JSON.") from None


def fetch_calendar(username: str, end: date, now: datetime) -> object:
    """Make at most three 20-second requests; never expose API error bodies."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ContributionError("Set GITHUB_TOKEN or use --input-json.")
    start = end - timedelta(days=29)
    to = min(f"{end}T23:59:59Z", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    body = json.dumps({
        "query": QUERY,
        "variables": {"username": username, "from": f"{start}T00:00:00Z", "to": to},
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "github-contribution-graph",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return decode_json(response.read())
        except HTTPError as error:
            status = error.code
            error.close()
            if status != 429 and not 500 <= status <= 599:
                raise ContributionError(f"GitHub request failed (HTTP {status}).") from None
            message = f"GitHub request failed after 3 attempts (HTTP {status})."
        except URLError:
            message = "Unable to reach GitHub after 3 attempts."
        except (OSError, HTTPException, ValueError):
            raise ContributionError("GitHub request failed; no graph was published.") from None
        if attempt == 2:
            raise ContributionError(message) from None
        time.sleep(2 ** attempt)
    raise ContributionError("GitHub request failed.")


def parse_calendar(payload: object, end: date) -> list[tuple[date, int]]:
    """Require every requested date exactly once; never infer zero counts."""
    start = end - timedelta(days=29)
    if not isinstance(payload, dict):
        raise ContributionError("Invalid GraphQL response structure.")
    if "errors" in payload and payload["errors"] != []:
        raise ContributionError("GitHub returned GraphQL errors; check access and username.")
    try:
        weeks = payload["data"]["user"]["contributionsCollection"][
            "contributionCalendar"
        ]["weeks"]
        if not isinstance(weeks, list):
            raise TypeError
        counters = {}
        for week in weeks:
            records = week["contributionDays"]
            if not isinstance(records, list):
                raise TypeError
            for record in records:
                day = iso_date(record["date"])
                count = record["contributionCount"]
                if type(count) is not int or count < 0:
                    raise ValueError
                # GitHub may include the surrounding days of partial weeks.
                if not start <= day <= end:
                    continue
                if day in counters:
                    raise ContributionError("Duplicate contribution date in the requested window.")
                counters[day] = count
    except (KeyError, TypeError, ValueError):
        raise ContributionError("Invalid contribution calendar structure, date, or count.") from None
    if len(counters) != 30:
        raise ContributionError("Expected all 30 daily contribution records; refusing incomplete data.")
    return sorted(counters.items())


def render_svg(days: list[tuple[date, int]], username: str, theme: str) -> str:
    background, text, secondary, grid, line = COLORS[theme]
    start, end = days[0][0], days[-1][0]
    total = sum(count for _, count in days)
    maximum = max(count for _, count in days)
    last = days[-1][1]
    username = html.escape(username)

    # Integer ticks, a zero baseline, and a nonzero scale even for an empty month.
    step = max(1, (maximum + 3) // 4)
    magnitude = 10 ** (len(str(step)) - 1)
    step = next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= step)
    ceiling = max(step, ((maximum + step - 1) // step) * step)
    left, right, top, bottom = 72, 868, 104, 240
    points = [
        (left + i * (right - left) / 29, bottom - count / ceiling * (bottom - top))
        for i, (_, count) in enumerate(days)
    ]
    coordinates = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    area = f"M {left},{bottom} L {coordinates} L {right},{bottom} Z"
    title = f"GitHub contributions for @{username}: {total:,} in 30 days"
    description = (
        f"{total:,} GitHub contributions from {start} through {end}, inclusive. "
        f"Daily counters for @{username}; last day {last:,}; daily maximum "
        f"{maximum:,}. The vertical axis starts at zero; straight lines join "
        "the 30 daily values. Calendar counters only, with no repository details. "
        "Dates are UTC. Today's counts may change."
    )
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="320" '
        'viewBox="0 0 900 320" role="img" '
        'aria-labelledby="chart-title chart-description" '
        'style="max-width:100%;height:auto;font-family:system-ui,Segoe UI,sans-serif">',
        f'<title id="chart-title">{title}</title>',
        f'<desc id="chart-description">{description}</desc>',
        f'<rect width="900" height="320" rx="12" fill="{background}"/>',
        f'<g fill="{text}">',
        '<text x="32" y="36" font-size="26" font-weight="600">GitHub contributions</text>',
        f'<text x="868" y="36" text-anchor="end" font-size="20">{start} – {end}</text>',
        f'<text x="32" y="72" font-size="22">{total:,} total · {last:,} last day · '
        f'{maximum:,} daily max</text>',
        '</g>',
        f'<g fill="{secondary}" font-size="20">',
    ]
    for value in range(0, ceiling + 1, step):
        y = bottom - value / ceiling * (bottom - top)
        parts.extend([
            f'<line x1="{left}" x2="{right}" y1="{y:.2f}" y2="{y:.2f}" '
            f'stroke="{grid}" stroke-width="1"/>',
            f'<text x="{left - 12}" y="{y + 7:.2f}" text-anchor="end">{value:,}</text>',
        ])
    for i in (0, 6, 12, 18, 24, 29):
        x = points[i][0]
        anchor = "start" if i == 0 else "end" if i == 29 else "middle"
        label = days[i][0].strftime("%b %d")
        parts.extend([
            f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{bottom}" y2="{bottom + 6}" '
            f'stroke="{grid}"/>',
            f'<text x="{x:.2f}" y="272" text-anchor="{anchor}">{label}</text>',
        ])
    parts.extend([
        '</g>',
        f'<path d="{area}" fill="{line}" fill-opacity="0.12"/>',
        f'<polyline points="{coordinates}" fill="none" stroke="{line}" '
        'stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>',
        f'<circle cx="{points[-1][0]:.2f}" cy="{points[-1][1]:.2f}" r="4" fill="{line}"/>',
        f'<text x="32" y="306" fill="{secondary}" font-size="20">'
        f"As of {end} UTC · Today's counts may change.</text>",
        '</svg>',
    ])
    return "\n".join(parts) + "\n"


def write_graphs(output_dir: Path, graphs: dict[str, str]) -> None:
    """Stage both complete SVGs before atomically replacing individual files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for filename, svg in graphs.items():
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=output_dir,
                prefix=".contributions-", suffix=".tmp", delete=False,
            ) as temporary:
                source = Path(temporary.name)
                staged.append((source, output_dir / filename))
                temporary.write(svg)
            # Closed files also work with os.replace on Windows.
        for source, destination in staged:
            os.replace(source, destination)
    finally:
        for source, _ in staged:
            source.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="ihmorol")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--input-json", type=Path, help="Read a GraphQL response instead of the API")
    parser.add_argument("--end-date", type=iso_date, help="Inclusive YYYY-MM-DD (default: UTC today)")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    end = args.end_date or now.date()
    try:
        if end < date.min + timedelta(days=29):
            raise ContributionError("End date must allow a complete 30-day window.")
        payload = (
            decode_json(args.input_json.read_bytes())
            if args.input_json else fetch_calendar(args.username, end, now)
        )
        days = parse_calendar(payload, end)
        graphs = {
            filename: render_svg(days, args.username, theme)
            for filename, theme in zip(FILENAMES, COLORS)
        }
        write_graphs(args.output_dir, graphs)
    except (ContributionError, OSError) as error:
        message = str(error) if isinstance(error, ContributionError) else "Unable to read or write graph files."
        print(f"Contribution graph: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
