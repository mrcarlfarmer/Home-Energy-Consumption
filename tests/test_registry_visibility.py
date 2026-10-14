import io
import runpy
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_registry_visibility.py"


def response(body: bytes) -> MagicMock:
    result = MagicMock()
    result.__enter__.return_value = io.BytesIO(body)
    return result


@pytest.mark.parametrize("status", [401, 403])
def test_private_package_denies_anonymous_token(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    verify = runpy.run_path(str(SCRIPT))["verify_private"]
    request = MagicMock(side_effect=HTTPError("https://ghcr.io/token", status, "Denied", {}, None))
    monkeypatch.setitem(verify.__globals__, "urlopen", request)
    verify("ghcr.io/owner/package")
    request.assert_called_once()


@pytest.mark.parametrize("status", [401, 403])
def test_private_package_denies_anonymous_tag_listing(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    verify = runpy.run_path(str(SCRIPT))["verify_private"]
    request = MagicMock(
        side_effect=[
            response(b'{"token":"anonymous-fixture"}'),
            HTTPError("https://ghcr.io/v2/owner/package/tags/list", status, "Denied", {}, None),
        ]
    )
    monkeypatch.setitem(verify.__globals__, "urlopen", request)
    verify("ghcr.io/owner/package")
    assert request.call_args.args[0].full_url.endswith("/owner/package/tags/list")


def test_public_package_is_blocked_before_a_new_commit_tag_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify = runpy.run_path(str(SCRIPT))["verify_private"]
    request = MagicMock(
        side_effect=[
            response(b'{"token":"anonymous-fixture"}'),
            response(b'{"tags":[]}'),
        ]
    )
    monkeypatch.setitem(verify.__globals__, "urlopen", request)
    with pytest.raises(RuntimeError, match="Choose a private package via GHCR_PACKAGE"):
        verify("ghcr.io/owner/package")


@pytest.mark.parametrize("status", [404, 429, 500])
def test_registry_errors_do_not_look_like_private_access(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    verify = runpy.run_path(str(SCRIPT))["verify_private"]
    request = MagicMock(side_effect=HTTPError("https://ghcr.io/token", status, "Failure", {}, None))
    monkeypatch.setitem(verify.__globals__, "urlopen", request)
    with pytest.raises(HTTPError):
        verify("ghcr.io/owner/package")
