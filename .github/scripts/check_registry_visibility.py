"""Refuse publishing private repository code to an anonymously readable GHCR package."""

import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def verify_private(image: str) -> None:
    package = image.removeprefix("ghcr.io/")
    query = urlencode({"service": "ghcr.io", "scope": f"repository:{package}:pull"})
    try:
        with urlopen(f"https://ghcr.io/token?{query}", timeout=30) as response:
            token = json.load(response)["token"]
    except HTTPError as error:
        if error.code not in (401, 403):
            raise
        print("Anonymous package access denied.")
        return
    # A readable package is unsafe even if the next commit tag does not yet exist.
    request = Request(
        f"https://ghcr.io/v2/{package}/tags/list",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=30):
            raise RuntimeError(
                f"{image} is anonymously readable. Choose a private package via GHCR_PACKAGE "
                "before publishing private repository code."
            )
    except HTTPError as error:
        if error.code not in (401, 403):
            raise
        print("Anonymous package access denied.")


if __name__ == "__main__":
    verify_private(os.environ["IMAGE"])
