"""Run with python -m unittest discover -s .github/scripts -v."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET

import generate_contribution_graph as graph


SCRIPT = Path(__file__).with_name("generate_contribution_graph.py")
END = date(2026, 11, 8)
NS = {"svg": "http://www.w3.org/2000/svg"}
FILENAMES = (
    "github-contribution-graph.svg",
    "github-contribution-graph-dark.svg",
)


def calendar_payload(end=END, counts=None):
    """A GraphQL response with one counter per day; no repository details."""
    counts = list(range(30)) if counts is None else counts
    start = end - timedelta(days=29)
    days = [
        {"date": (start + timedelta(days=i)).isoformat(), "contributionCount": n}
        for i, n in enumerate(counts)
    ]
    return {
        "data": {
            "user": {
                "contributionsCollection": {
                    "contributionCalendar": {
                        "weeks": [
                            {"contributionDays": days[i : i + 7]}
                            for i in range(0, len(days), 7)
                        ]
                    }
                }
            }
        }
    }


def weeks_of(payload):
    return payload["data"]["user"]["contributionsCollection"][
        "contributionCalendar"
    ]["weeks"]


class CalendarTests(unittest.TestCase):
    def test_valid_data_is_sorted_and_keeps_daily_counts(self):
        payload = calendar_payload()
        weeks_of(payload).reverse()
        weeks_of(payload)[0]["contributionDays"].reverse()
        days = graph.parse_calendar(payload, END)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[0], (date(2026, 10, 10), 0))
        self.assertEqual(days[-1], (END, 29))
        self.assertEqual(sum(n for _, n in days), 435)

    def test_partial_week_dates_outside_window_are_filtered(self):
        payload = calendar_payload()
        weeks_of(payload)[0]["contributionDays"][:0] = [
            {"date": "2026-10-09", "contributionCount": 99}
        ]
        weeks_of(payload)[-1]["contributionDays"].append(
            {"date": "2026-11-09", "contributionCount": 99}
        )
        days = graph.parse_calendar(payload, END)
        self.assertEqual(len(days), 30)
        self.assertEqual(sum(n for _, n in days), 435)

    def test_missing_first_middle_or_last_day_is_not_padded(self):
        for week_index, day_index in ((0, 0), (2, 3), (-1, -1)):
            with self.subTest(week=week_index, day=day_index):
                payload = calendar_payload()
                weeks_of(payload)[week_index]["contributionDays"].pop(day_index)
                with self.assertRaises(graph.ContributionError):
                    graph.parse_calendar(payload, END)

    def test_duplicate_date_is_rejected_even_with_thirty_records(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                payload = calendar_payload()
                days = weeks_of(payload)[0]["contributionDays"]
                if replace:
                    days[1] = days[0].copy()
                else:
                    days.append(days[0].copy())
                with self.assertRaises(graph.ContributionError):
                    graph.parse_calendar(payload, END)

    def test_rejects_negative_boolean_and_other_noninteger_counts(self):
        for count in (-1, True, False, 1.0, "2", None, [], {}):
            with self.subTest(count=count):
                payload = calendar_payload()
                weeks_of(payload)[0]["contributionDays"][0]["contributionCount"] = count
                with self.assertRaises(graph.ContributionError):
                    graph.parse_calendar(payload, END)

    def test_invalid_or_noncanonical_dates_are_rejected(self):
        for day in ("2026-02-30", "20261010", "2026-W41-6", "10/10/2026", None, 1):
            with self.subTest(day=day):
                payload = calendar_payload()
                weeks_of(payload)[0]["contributionDays"][0]["date"] = day
                with self.assertRaises(graph.ContributionError):
                    graph.parse_calendar(payload, END)

    def test_malformed_graphql_shapes_fail_with_safe_errors(self):
        payloads = [None, [], {}, {"data": None}, {"data": []}, {"data": {}}]
        for field in ("user", "contributionsCollection", "contributionCalendar", "weeks"):
            for bad in (None, [], "secret response", 1):
                payload = calendar_payload()
                parent = payload["data"]
                for key in ("user", "contributionsCollection", "contributionCalendar", "weeks"):
                    if key == field:
                        parent[key] = bad
                        break
                    parent = parent[key]
                payloads.append(payload)
        for bad in (None, {}, {"contributionDays": None}, {"contributionDays": {}}, {"contributionDays": [None]}, {"contributionDays": [{}]}):
            payload = calendar_payload()
            weeks_of(payload)[0] = bad
            payloads.append(payload)
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(graph.ContributionError) as caught:
                    graph.parse_calendar(payload, END)
                self.assertNotIn("secret response", str(caught.exception))

    def test_graphql_errors_rejected_without_echoing_details(self):
        for errors in ([{"message": "secret response"}], "secret response", {}, None, False):
            with self.subTest(errors=errors):
                payload = calendar_payload()
                payload["errors"] = errors
                with self.assertRaises(graph.ContributionError) as caught:
                    graph.parse_calendar(payload, END)
                self.assertNotIn("secret response", str(caught.exception))
        payload = calendar_payload()
        payload["errors"] = []
        self.assertEqual(len(graph.parse_calendar(payload, END)), 30)

    def test_thirty_day_windows_cross_month_year_and_leap_day(self):
        cases = (
            (date(2026, 1, 10), date(2025, 12, 12)),
            (date(2024, 3, 1), date(2024, 2, 1)),
            (date(2024, 2, 29), date(2024, 1, 31)),
            (date(2025, 3, 1), date(2025, 1, 31)),
            (date(2026, 5, 1), date(2026, 4, 2)),
        )
        for end, start in cases:
            with self.subTest(end=end):
                days = graph.parse_calendar(calendar_payload(end), end)
                self.assertEqual(len(days), 30)
                self.assertEqual(days[0][0], start)
                self.assertEqual(days[-1][0], end)
                self.assertTrue(all(b[0] - a[0] == timedelta(days=1) for a, b in zip(days, days[1:])))


class SvgTests(unittest.TestCase):
    def test_light_and_dark_are_static_accessible_xml_with_requested_palette(self):
        days = graph.parse_calendar(calendar_payload(), END)
        palettes = {
            "light": ("#F8FAFC", "#0F172A", "#475569", "#CBD5E1", "#0F766E"),
            "dark": ("#0F172A", "#E2E8F0", "#94A3B8", "#334155", "#2DD4BF"),
        }
        for theme, palette in palettes.items():
            with self.subTest(theme=theme):
                raw = graph.render_svg(days, '<script>&"name', theme)
                svg = ET.fromstring(raw)
                self.assertEqual(svg.attrib["viewBox"], "0 0 900 320")
                self.assertEqual(svg.attrib["role"], "img")
                self.assertIn("max-width:100%", svg.attrib["style"])
                title = svg.find("svg:title", NS)
                description = svg.find("svg:desc", NS)
                self.assertEqual(svg.attrib["aria-labelledby"].split(), [title.attrib["id"], description.attrib["id"]])
                self.assertIn('<script>&"name', title.text)
                self.assertIn("435", description.text)
                self.assertIn("2026-10-10", description.text)
                self.assertIn("2026-11-08", description.text)
                visible = " ".join(node.text or "" for node in svg.findall(".//svg:text", NS))
                self.assertIn("435 total", visible)
                self.assertIn("29 last day", visible)
                self.assertIn("29 daily max", visible)
                self.assertIn("UTC", visible)
                self.assertIn("Today's counts may change", visible)
                self.assertNotIn("public repo commits", visible)
                for color in palette:
                    self.assertIn(color, raw)
                self.assertEqual(svg.find("svg:rect", NS).attrib["fill"], palette[0])
                self.assertEqual(svg.find("svg:polyline", NS).attrib["stroke"], palette[-1])
                self.assertLess(float(svg.find("svg:path", NS).attrib["fill-opacity"]), 0.3)
                self.assertNotIn("href=", raw)
                for forbidden in ("script", "foreignObject", "image", "animate"):
                    self.assertIsNone(svg.find(f".//svg:{forbidden}", NS))
                ticks = [node.text for node in svg.findall(".//svg:text", NS) if node.attrib.get("y") == "272"]
                self.assertEqual(ticks, ["Oct 10", "Oct 16", "Oct 22", "Oct 28", "Nov 03", "Nov 08"])

    def test_sharp_daily_spikes_are_exact_straight_segments_with_zero_baseline(self):
        counts = [0, 8, 0] + [0] * 26 + [4]
        days = graph.parse_calendar(calendar_payload(counts=counts), END)
        svg = ET.fromstring(graph.render_svg(days, "ihmorol", "light"))
        points = [tuple(map(float, point.split(","))) for point in svg.find("svg:polyline", NS).attrib["points"].split()]
        self.assertEqual(len(points), 30)
        self.assertEqual(points[0], (72.0, 240.0))
        self.assertEqual(points[1][1], 104.0)
        self.assertEqual(points[2][1], 240.0)
        self.assertEqual(points[-1], (868.0, 172.0))
        self.assertTrue(all(a[0] < b[0] for a, b in zip(points, points[1:])))
        area = svg.find("svg:path", NS).attrib["d"]
        self.assertTrue(area.startswith("M 72,240 L "))
        self.assertTrue(area.endswith(" L 868,240 Z"))
        self.assertTrue(set(character for character in area if character.isalpha()) <= {"M", "L", "Z"})

    def test_all_zero_calendar_stays_on_baseline_without_nan(self):
        days = graph.parse_calendar(calendar_payload(counts=[0] * 30), END)
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                svg = ET.fromstring(graph.render_svg(days, "ihmorol", theme))
                points = svg.find("svg:polyline", NS).attrib["points"].split()
                self.assertEqual(len(points), 30)
                self.assertTrue(all(float(point.split(",")[1]) == 240 for point in points))
                self.assertIn("0 GitHub contributions", svg.find("svg:desc", NS).text)
                self.assertIn("daily maximum 0", svg.find("svg:desc", NS).text)


class NetworkTests(unittest.TestCase):
    def test_api_cli_posts_calendar_only_query_with_utc_dates_and_env_token(self):
        now = datetime(2026, 11, 8, 12, 34, 56, tzinfo=timezone.utc)
        for explicit_end, expected_to in ((None, "2026-11-08T12:34:56Z"), ("2026-11-07", "2026-11-07T23:59:59Z")):
            with self.subTest(end=explicit_end), tempfile.TemporaryDirectory() as directory:
                end = date.fromisoformat(explicit_end) if explicit_end else END
                seen = []

                def open_response(request, timeout):
                    seen.append((request, timeout))
                    return io.BytesIO(json.dumps(calendar_payload(end)).encode())

                errors = io.StringIO()
                argv = ["--output-dir", directory, "--username", 'name"&<test>']
                if explicit_end:
                    argv.extend(["--end-date", explicit_end])
                with patch.dict("os.environ", {"GITHUB_TOKEN": "token-not-for-output"}, clear=True), patch("urllib.request.urlopen", side_effect=open_response), patch.object(graph, "datetime") as clock, patch("sys.stderr", errors):
                    clock.now.return_value = now
                    status = graph.main(argv)
                self.assertEqual(status, 0, errors.getvalue())
                self.assertEqual(len(seen), 1)
                request, timeout = seen[0]
                self.assertEqual(request.full_url, "https://api.github.com/graphql")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer token-not-for-output")
                self.assertEqual(request.get_header("Content-type"), "application/json")
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 30)
                body = json.loads(request.data)
                self.assertEqual(body["variables"]["username"], 'name"&<test>')
                expected_from = "2026-10-09T00:00:00Z" if explicit_end else "2026-10-10T00:00:00Z"
                self.assertEqual(body["variables"]["from"], expected_from)
                self.assertEqual(body["variables"]["to"], expected_to)
                query = body["query"]
                self.assertNotIn('name"&<test>', query)
                self.assertIn("contributionsCollection(from: $from, to: $to)", query)
                for field in ("user(login: $username)", "contributionCalendar", "weeks", "contributionDays", "date", "contributionCount"):
                    self.assertIn(field, query)
                self.assertNotIn("repository", query.lower())
                for filename in FILENAMES:
                    svg = (Path(directory) / filename).read_text(encoding="utf-8")
                    self.assertNotIn("token-not-for-output", svg)
                    self.assertIn('@name"&<test>', ET.fromstring(svg).find("svg:title", NS).text)
                self.assertNotIn("token-not-for-output", errors.getvalue())

    def run_network_case(self, outcomes):
        """Fake only the HTTP boundary; exercise CLI, validation, and real files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in FILENAMES:
                (root / filename).write_text("previous", encoding="utf-8")
            errors = io.StringIO()
            with patch.dict("os.environ", {"GITHUB_TOKEN": "secret-token"}, clear=True), patch("urllib.request.urlopen", side_effect=outcomes) as request, patch("time.sleep") as sleep, patch("sys.stderr", errors):
                status = graph.main(["--output-dir", directory, "--end-date", "2026-11-08"])
            files = [(root / name).read_text(encoding="utf-8") for name in FILENAMES]
            self.assertNotIn("secret-token", errors.getvalue())
            self.assertNotIn("secret response", errors.getvalue())
            self.assertTrue(all(0 < call.args[0] <= 5 for call in sleep.call_args_list))
            self.assertEqual(sorted(p.name for p in root.iterdir()), sorted(FILENAMES))
            return status, request.call_count, sleep.call_count, files

    def test_transient_errors_retry_then_publish_valid_calendar(self):
        for code in (429, 500, 502, 503):
            with self.subTest(code=code):
                error = HTTPError("https://api.github.com/graphql", code, "secret response", {}, None)
                status, calls, sleeps, files = self.run_network_case([
                    error, URLError("secret-token secret response"),
                    io.BytesIO(json.dumps(calendar_payload()).encode()),
                ])
                self.assertEqual((status, calls, sleeps), (0, 3, 2))
                self.assertTrue(all("<svg " in svg for svg in files))

    def test_transient_failures_stop_after_three_attempts_and_keep_graphs(self):
        for error in (URLError("secret response"), HTTPError("https://api.github.com/graphql", 503, "secret response", {}, None)):
            with self.subTest(error=type(error).__name__):
                status, calls, sleeps, files = self.run_network_case([error] * 3)
                self.assertEqual((status, calls, sleeps), (1, 3, 2))
                self.assertEqual(files, ["previous", "previous"])

    def test_nonretryable_http_errors_do_not_retry_or_publish(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                error = HTTPError("https://api.github.com/graphql", code, "secret-token secret response", {}, None)
                status, calls, sleeps, files = self.run_network_case([error])
                self.assertEqual((status, calls, sleeps), (1, 1, 0))
                self.assertEqual(files, ["previous", "previous"])

    def test_invalid_success_response_does_not_retry_or_publish(self):
        for raw in (b"not JSON: secret response", b' {"data":{"user":null}}', b'{"errors":[{"message":"secret response"}]}', b"null"):
            with self.subTest(raw=raw):
                status, calls, sleeps, files = self.run_network_case([io.BytesIO(raw)])
                self.assertEqual((status, calls, sleeps), (1, 1, 0))
                self.assertEqual(files, ["previous", "previous"])

    def test_missing_token_fails_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as request, patch("sys.stderr", errors):
                status = graph.main(["--output-dir", directory])
            self.assertEqual(status, 1)
            self.assertEqual(request.call_count, 0)
            self.assertIn("GITHUB_TOKEN", errors.getvalue())
            self.assertEqual(list(Path(directory).iterdir()), [])


class CommandLineTests(unittest.TestCase):
    def test_fixture_creates_both_graphs_with_real_counts_and_date_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "calendar.json"
            fixture.write_text(json.dumps(calendar_payload()), encoding="utf-8")
            output = root / "dist"
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(SCRIPT),
                    "--input-json",
                    str(fixture),
                    "--end-date",
                    "2026-11-08",
                    "--output-dir",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for filename in FILENAMES:
                with self.subTest(filename=filename):
                    svg = ET.parse(output / filename).getroot()
                    title = svg.find("svg:title", NS).text
                    description = svg.find("svg:desc", NS).text
                    self.assertIn("GitHub contributions", title)
                    self.assertIn("ihmorol", title)
                    self.assertIn("435", title)
                    self.assertIn("2026-10-10", description)
                    self.assertIn("2026-11-08", description)

    def test_bad_fixtures_preserve_both_previous_graphs_and_hide_details(self):
        missing_day = calendar_payload()
        weeks_of(missing_day)[0]["contributionDays"].pop()
        bad_inputs = (
            b"not json: secret response",
            b"\xff",
            b"null",
            json.dumps({"errors": [{"message": "secret response"}]}).encode(),
            json.dumps(missing_day).encode(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "dist"
            output.mkdir()
            for filename in FILENAMES:
                (output / filename).write_text("last successful graph", encoding="utf-8")
            fixture = root / "calendar.json"
            for raw in bad_inputs:
                with self.subTest(raw=raw):
                    fixture.write_bytes(raw)
                    errors = io.StringIO()
                    with patch("sys.stderr", errors):
                        status = graph.main([
                            "--input-json", str(fixture), "--end-date", "2026-11-08",
                            "--output-dir", str(output),
                        ])
                    self.assertEqual(status, 1)
                    self.assertNotIn("secret response", errors.getvalue())
                    self.assertTrue(errors.getvalue())
                    self.assertEqual(sorted(p.name for p in output.iterdir()), sorted(FILENAMES))
                    for filename in FILENAMES:
                        self.assertEqual((output / filename).read_text(encoding="utf-8"), "last successful graph")

    def test_render_and_staging_failure_leave_both_previous_files_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "calendar.json"
            fixture.write_text(json.dumps(calendar_payload()), encoding="utf-8")
            output = root / "dist"
            output.mkdir()
            for filename in FILENAMES:
                (output / filename).write_text("previous", encoding="utf-8")
            argv = ["--input-json", str(fixture), "--output-dir", str(output), "--end-date", "2026-11-08"]
            with patch.object(graph, "render_svg", side_effect=["first complete SVG", graph.ContributionError("Rendering failed.")]), patch("sys.stderr", io.StringIO()):
                self.assertEqual(graph.main(argv), 1)
            original_temporary_file = tempfile.NamedTemporaryFile
            attempts = 0

            def fail_second_temporary_file(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise OSError("sensitive file path")
                return original_temporary_file(*args, **kwargs)

            with patch("tempfile.NamedTemporaryFile", side_effect=fail_second_temporary_file), patch("sys.stderr", io.StringIO()):
                self.assertEqual(graph.main(argv), 1)
            self.assertEqual(attempts, 2)
            self.assertEqual(sorted(p.name for p in output.iterdir()), sorted(FILENAMES))
            for filename in FILENAMES:
                self.assertEqual((output / filename).read_text(encoding="utf-8"), "previous")

    def test_invalid_end_dates_fail_cleanly_before_output(self):
        for end in ("2026-02-30", "20261108", "0001-01-01"):
            with self.subTest(end=end), tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory) / "calendar.json"
                fixture.write_text(json.dumps(calendar_payload()), encoding="utf-8")
                output = Path(directory) / "dist"
                result = subprocess.run(
                    [sys.executable, "-I", str(SCRIPT), "--input-json", str(fixture), "--end-date", end, "--output-dir", str(output)],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
