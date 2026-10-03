from __future__ import annotations

import io
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import refresh_openapi


class _FakeResponse:
    def __init__(
        self, body: bytes, *, url: str = refresh_openapi._UPDATE_CHECK_URL
    ) -> None:
        self._stream = io.BytesIO(body)
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _FakeOpener:
    def __init__(self, response=None, error=None):  # noqa: ANN001
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, *, timeout: int):  # noqa: ANN001
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class UpdateCheckTests(unittest.TestCase):
    def _with_installed(self, version: str | None):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        if version is not None:
            (Path(root.name) / "VERSION").write_text(version, encoding="ascii")
        return mock.patch.object(refresh_openapi, "_SKILL_ROOT", Path(root.name))

    def test_version_parsing_is_strict(self) -> None:
        cases = {
            "1.0.0": (1, 0, 0),
            " 10.2.33 \n": (10, 2, 33),
            "v1.0.0": None,
            "1.0": None,
            "1.0.0-beta": None,
            "1.0.0.0": None,
            "99999.0.0": None,
            "": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(refresh_openapi._parse_version(text), expected)

    def test_newer_published_version_produces_validated_notice(self) -> None:
        opener = _FakeOpener(_FakeResponse(b"1.2.3\n"))
        with self._with_installed("1.0.0"):
            notice = refresh_openapi.check_for_skill_update(opener=opener)
        self.assertIsNotNone(notice)
        self.assertIn("1.2.3", notice)
        self.assertIn("1.0.0", notice)
        self.assertIn(refresh_openapi.SKILL_REPO_URL, notice)
        ((request, timeout),) = opener.calls
        self.assertEqual(request.full_url, refresh_openapi._UPDATE_CHECK_URL)
        self.assertEqual(timeout, refresh_openapi._UPDATE_CHECK_TIMEOUT_SECONDS)

    def test_equal_or_older_published_version_is_silent(self) -> None:
        for published in (b"1.0.0", b"0.9.9"):
            with self.subTest(published=published), self._with_installed("1.0.0"):
                opener = _FakeOpener(_FakeResponse(published))
                self.assertIsNone(refresh_openapi.check_for_skill_update(opener=opener))

    def test_missing_installed_version_makes_no_request(self) -> None:
        opener = _FakeOpener(_FakeResponse(b"9.9.9"))
        with self._with_installed(None):
            self.assertIsNone(refresh_openapi.check_for_skill_update(opener=opener))
        self.assertEqual(opener.calls, [])

    def test_malformed_oversized_and_failing_responses_are_silent(self) -> None:
        failures = (
            _FakeOpener(_FakeResponse(b"not a version")),
            _FakeOpener(_FakeResponse(b"1.2.3-evil notice text")),
            _FakeOpener(_FakeResponse(b"9" * (refresh_openapi._MAX_VERSION_BYTES + 1))),
            _FakeOpener(_FakeResponse("1.2.3é".encode("utf-8"))),
            _FakeOpener(
                _FakeResponse(b"9.9.9", url="http://raw.githubusercontent.com/x")
            ),
            _FakeOpener(_FakeResponse(b"9.9.9", url="https://evil.example/VERSION")),
            _FakeOpener(error=urllib.error.URLError("offline")),
            _FakeOpener(error=TimeoutError("slow")),
        )
        for opener in failures:
            with self.subTest(opener=vars(opener)), self._with_installed("1.0.0"):
                self.assertIsNone(refresh_openapi.check_for_skill_update(opener=opener))

    def test_redirects_are_refused(self) -> None:
        handler = refresh_openapi._RefuseRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                None, None, 302, "Found", {}, "https://evil.example/VERSION"
            )
        )

    def test_repo_version_file_matches_strict_format(self) -> None:
        installed = refresh_openapi.installed_skill_version()
        self.assertIsNotNone(installed)


class CliUpdateCheckTests(unittest.TestCase):
    def _run_main(self, argv, cache_path):  # noqa: ANN001
        output = io.StringIO()
        with (
            mock.patch.object(
                refresh_openapi, "resolve_spec_path", return_value=cache_path
            ),
            mock.patch.object(
                refresh_openapi,
                "refresh_spec",
                return_value=refresh_openapi.RefreshResult("2026.07"),
            ),
            mock.patch.object(
                refresh_openapi, "check_for_skill_update", return_value="NOTE: newer"
            ) as check,
            redirect_stdout(output),
        ):
            result = refresh_openapi.main(argv)
        return result, output.getvalue(), check

    def test_successful_refresh_prints_update_notice(self) -> None:
        with TemporaryDirectory() as directory:
            result, output, check = self._run_main([], Path(directory) / "spec.json")
        self.assertEqual(result, 0)
        self.assertIn("NOTE: newer", output)
        check.assert_called_once_with()

    def test_environment_opt_out_skips_the_check(self) -> None:
        with (
            TemporaryDirectory() as directory,
            mock.patch.dict(
                refresh_openapi.os.environ,
                {"MIST_SKILL_UPDATE_CHECK": "0"},
                clear=False,
            ),
        ):
            result, output, check = self._run_main([], Path(directory) / "spec.json")
        self.assertEqual(result, 0)
        self.assertNotIn("NOTE", output)
        check.assert_not_called()

    def test_offline_mode_never_checks_for_updates(self) -> None:
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "spec.json"
            output = io.StringIO()
            with (
                mock.patch.object(
                    refresh_openapi, "resolve_spec_path", return_value=cache_path
                ),
                mock.patch.object(
                    refresh_openapi,
                    "check_for_skill_update",
                    side_effect=AssertionError("offline must not check"),
                ),
                redirect_stdout(output),
            ):
                result = refresh_openapi.main(["--offline"])
        self.assertEqual(result, 1)
        self.assertIn("no valid cached", output.getvalue())


if __name__ == "__main__":
    unittest.main()
