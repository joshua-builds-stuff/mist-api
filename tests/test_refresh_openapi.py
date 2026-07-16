from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import refresh_openapi, spec_cache


def _spec_bytes(
    *, version: str = "2026.07", marker: str = "current", openapi: str = "3.1.0"
) -> bytes:
    document = {
        "openapi": openapi,
        "info": {"title": "Mist API", "version": version},
        "paths": {"/api/v1/self": {"get": {"operationId": "getSelf"}}},
        "components": {"schemas": {"Self": {"type": "object"}}},
        "x-test-marker": marker,
    }
    return json.dumps(document, sort_keys=True).encode()


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        content_length: int | None = None,
        url: str = refresh_openapi.SPEC_URL,
    ) -> None:
        self._stream = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
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
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls = []

    def open(self, request, *, timeout: int):  # noqa: ANN001
        self.calls.append((request, timeout))
        return self.response


class SpecCacheTests(unittest.TestCase):
    def test_platform_cache_roots(self) -> None:
        home = Path(tempfile.gettempdir()).resolve() / "home" / "tester"
        windows_cache = home / "local-cache"
        xdg_cache = home / "xdg-cache"
        cases = (
            (
                {
                    "os_name": "nt",
                    "platform": "win32",
                    "environ": {"LOCALAPPDATA": str(windows_cache)},
                },
                windows_cache,
            ),
            (
                {"os_name": "posix", "platform": "darwin", "environ": {}},
                home / "Library" / "Caches",
            ),
            (
                {
                    "os_name": "posix",
                    "platform": "linux",
                    "environ": {"XDG_CACHE_HOME": str(xdg_cache)},
                },
                xdg_cache,
            ),
            (
                {"os_name": "posix", "platform": "linux", "environ": {}},
                home / ".cache",
            ),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    spec_cache._default_cache_root(home=home, **arguments), expected
                )

    def test_override_is_exact_absolute_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            expected = Path(temporary_directory) / "custom.json"
            with mock.patch.dict(
                os.environ, {"MIST_OPENAPI_PATH": str(expected)}, clear=False
            ):
                self.assertEqual(spec_cache.resolve_spec_path(), expected)

    def test_explicit_path_takes_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            explicit = root / "explicit.json"
            environment = root / "environment.json"
            with mock.patch.dict(
                os.environ, {"MIST_OPENAPI_PATH": str(environment)}, clear=False
            ):
                self.assertEqual(spec_cache.resolve_spec_path(str(explicit)), explicit)

    def test_override_rejects_relative_and_installed_paths(self) -> None:
        values = (
            "relative-cache.json",
            str(spec_cache._INSTALL_ROOT / "reference" / "cache.json"),
        )
        for value in values:
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"MIST_OPENAPI_PATH": value}, clear=False),
            ):
                with self.assertRaises(ValueError):
                    spec_cache.resolve_spec_path()

    def test_read_version_handles_malformed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bad.json"
            path.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(spec_cache.read_version(path))
            self.assertIsNone(spec_cache.read_version(path.with_name("missing.json")))

    def test_validation_rejects_non_mist_openapi_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "other-api.json"
            document = json.loads(_spec_bytes())
            document["info"]["title"] = "Unrelated API"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                spec_cache.SpecValidationError, "Mist API document"
            ):
                spec_cache.validate_spec(path)


class RefreshTests(unittest.TestCase):
    def test_private_file_mode_has_windows_310_fallback(self) -> None:
        path = Path(tempfile.gettempdir()) / "private-cache.json"
        with (
            mock.patch.object(refresh_openapi.os, "fchmod", None, create=True),
            mock.patch.object(refresh_openapi.os, "chmod") as chmod,
        ):
            refresh_openapi._make_file_private(123, path)
        chmod.assert_called_once_with(path, stat.S_IRUSR | stat.S_IWUSR)

    def test_source_is_hardcoded_official_https(self) -> None:
        self.assertTrue(refresh_openapi.SPEC_URL.startswith("https://www.juniper.net/"))

    def test_redirect_handler_blocks_insecure_and_foreign_redirects(self) -> None:
        handler = refresh_openapi._OfficialHttpsRedirectHandler()
        malicious_urls = (
            "http://www.juniper.net/spec.json",
            "https://juniper.net/spec.json",
            "https://www.juniper.net.evil.example/spec.json",
            "https://www.juniper.net@evil.example/spec.json",
        )
        for url in malicious_urls:
            with self.subTest(url=url), self.assertRaises(refresh_openapi.RefreshError):
                handler.redirect_request(None, None, 302, "Found", {}, url)

    def test_final_response_url_is_checked_before_cache_writes(self) -> None:
        response = _FakeResponse(
            _spec_bytes(), url="https://attacker.example/mist-openapi.json"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "new" / "spec.json"
            with self.assertRaisesRegex(
                refresh_openapi.RefreshError, "redirect blocked"
            ):
                refresh_openapi.refresh_spec(cache_path, opener=_FakeOpener(response))
            self.assertFalse(cache_path.parent.exists())

    def test_non_json_content_type_is_rejected(self) -> None:
        response = _FakeResponse(_spec_bytes(), content_type="text/html")
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "new" / "spec.json"
            with self.assertRaisesRegex(refresh_openapi.RefreshError, "non-JSON"):
                refresh_openapi.refresh_spec(cache_path, opener=_FakeOpener(response))
            self.assertFalse(cache_path.parent.exists())

    def test_content_length_over_limit_is_rejected_before_cache_writes(self) -> None:
        response = _FakeResponse(b"", content_length=17)
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch.object(refresh_openapi, "MAX_SPEC_BYTES", 16),
        ):
            cache_path = Path(temporary_directory) / "new" / "spec.json"
            with self.assertRaisesRegex(refresh_openapi.RefreshError, "size limit"):
                refresh_openapi.refresh_spec(cache_path, opener=_FakeOpener(response))
            self.assertFalse(cache_path.parent.exists())

    def test_streamed_download_over_limit_is_rejected_and_cleaned_up(self) -> None:
        response = _FakeResponse(b"x" * 17)
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch.object(refresh_openapi, "MAX_SPEC_BYTES", 16),
            mock.patch.object(refresh_openapi, "_DOWNLOAD_CHUNK_SIZE", 4),
        ):
            cache_path = Path(temporary_directory) / "cache" / "spec.json"
            with self.assertRaisesRegex(refresh_openapi.RefreshError, "size limit"):
                refresh_openapi.refresh_spec(cache_path, opener=_FakeOpener(response))
            self.assertFalse(cache_path.exists())
            self.assertEqual(list(cache_path.parent.iterdir()), [])

    def test_invalid_json_and_structure_never_replace_valid_cache(self) -> None:
        invalid_documents = (
            b"not-json",
            json.dumps([]).encode(),
            _spec_bytes(openapi="2.0"),
            json.dumps(
                {
                    "openapi": "3.1.0",
                    "info": {"version": "2026.07"},
                    "paths": {},
                    "components": {"schemas": {}},
                }
            ).encode(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "spec.json"
            original = _spec_bytes(marker="original")
            for index, body in enumerate(invalid_documents):
                with self.subTest(index=index):
                    cache_path.write_bytes(original)
                    response = _FakeResponse(body)
                    with self.assertRaises(refresh_openapi.RefreshError):
                        refresh_openapi.refresh_spec(
                            cache_path, opener=_FakeOpener(response)
                        )
                    self.assertEqual(cache_path.read_bytes(), original)
                    self.assertEqual(
                        [
                            path
                            for path in cache_path.parent.iterdir()
                            if path != cache_path
                        ],
                        [],
                    )

    def test_same_version_changed_content_is_replaced(self) -> None:
        """Regression: the old refresher discarded same-version downloads."""
        old_content = _spec_bytes(version="2026.07", marker="old")
        new_content = _spec_bytes(version="2026.07", marker="new")
        response = _FakeResponse(new_content, content_length=len(new_content))
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "spec.json"
            cache_path.write_bytes(old_content)
            result = refresh_openapi.refresh_spec(
                cache_path, opener=_FakeOpener(response)
            )
            self.assertEqual(result.version, "2026.07")
            self.assertEqual(cache_path.read_bytes(), new_content)
            metadata_path = refresh_openapi._metadata_path(cache_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(
                metadata,
                {
                    "byte_length": len(new_content),
                    "fetched_at": metadata["fetched_at"],
                    "openapi_version": "2026.07",
                    "sha256": hashlib.sha256(new_content).hexdigest(),
                    "source_url": refresh_openapi.SPEC_URL,
                },
            )
            self.assertRegex(
                metadata["fetched_at"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$",
            )

    def test_metadata_failure_keeps_installed_spec_and_removes_stale_metadata(
        self,
    ) -> None:
        old_content = _spec_bytes(version="2026.06", marker="old")
        new_content = _spec_bytes(version="2026.07", marker="new")
        response = _FakeResponse(new_content)
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "spec.json"
            metadata_path = refresh_openapi._metadata_path(cache_path)
            cache_path.write_bytes(old_content)
            metadata_path.write_text('{"stale": true}', encoding="utf-8")
            real_replace = os.replace

            def fail_metadata_replace(source, destination):  # noqa: ANN001
                if Path(destination) == metadata_path:
                    raise OSError("metadata disk error")
                return real_replace(source, destination)

            with mock.patch.object(
                refresh_openapi.os, "replace", side_effect=fail_metadata_replace
            ):
                result = refresh_openapi.refresh_spec(
                    cache_path, opener=_FakeOpener(response)
                )

            self.assertEqual(cache_path.read_bytes(), new_content)
            self.assertFalse(metadata_path.exists())
            self.assertIn("metadata update failed", result.metadata_warning or "")


class CliTests(unittest.TestCase):
    def test_offline_missing_cache_does_no_network_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "uncreated" / "spec.json"
            output = io.StringIO()
            with (
                mock.patch.object(
                    refresh_openapi, "resolve_spec_path", return_value=cache_path
                ),
                mock.patch.object(
                    refresh_openapi,
                    "refresh_spec",
                    side_effect=AssertionError("network path must not run"),
                ),
                redirect_stdout(output),
            ):
                result = refresh_openapi.main(["--offline"])
            self.assertEqual(result, 1)
            self.assertFalse(cache_path.parent.exists())
            self.assertIn("no valid cached", output.getvalue())

    def test_offline_reports_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "spec.json"
            cache_path.write_bytes(_spec_bytes(version="2026.07"))
            output = io.StringIO()
            with (
                mock.patch.object(
                    refresh_openapi, "resolve_spec_path", return_value=cache_path
                ),
                mock.patch.object(
                    refresh_openapi,
                    "refresh_spec",
                    side_effect=AssertionError("network path must not run"),
                ),
                redirect_stdout(output),
            ):
                result = refresh_openapi.main(["--offline"])
            self.assertEqual(result, 0)
            self.assertIn("version 2026.07", output.getvalue())
            self.assertIn("(offline)", output.getvalue())

    def test_refresh_failure_reports_cached_fallback_and_is_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "spec.json"
            cache_path.write_bytes(_spec_bytes(version="2026.06"))
            output = io.StringIO()
            with (
                mock.patch.object(
                    refresh_openapi, "resolve_spec_path", return_value=cache_path
                ),
                mock.patch.object(
                    refresh_openapi,
                    "refresh_spec",
                    side_effect=refresh_openapi.RefreshError("network unavailable"),
                ),
                redirect_stdout(output),
            ):
                result = refresh_openapi.main([])
            self.assertEqual(result, 1)
            rendered = output.getvalue().strip()
            self.assertEqual(len(rendered.splitlines()), 1)
            self.assertIn("refresh failed", rendered)
            self.assertIn("cached version 2026.06 remains available", rendered)

    def test_refresh_failure_does_not_treat_malformed_cache_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "spec.json"
            cache_path.write_text("bad json", encoding="utf-8")
            output = io.StringIO()
            with (
                mock.patch.object(
                    refresh_openapi, "resolve_spec_path", return_value=cache_path
                ),
                mock.patch.object(
                    refresh_openapi,
                    "refresh_spec",
                    side_effect=refresh_openapi.RefreshError("network unavailable"),
                ),
                redirect_stdout(output),
            ):
                result = refresh_openapi.main([])
            self.assertEqual(result, 1)
            self.assertIn("no valid cached document", output.getvalue())


if __name__ == "__main__":
    unittest.main()
