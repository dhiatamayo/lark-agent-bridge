"""Lark document reader — resolve a Lark/Feishu URL and fetch its content.

Reuses the app credentials from the environment (``LARK_APP_ID`` /
``LARK_APP_SECRET``) so no extra setup is needed beyond granting the app these
scopes:

    wiki:wiki:readonly           (resolve wiki node -> obj_token)
    docx:document:readonly       (read docx content)
    sheets:spreadsheet:readonly  (read sheet metadata)

Usage:
    python -m lark_agent_bridge.lark.docs <lark-url-or-token>
"""

from __future__ import annotations

import os
import re
import sys

import requests

LARK_DOMAIN_HOST = {
    "lark": "https://open.larksuite.com",
    "feishu": "https://open.feishu.cn",
}

# A Lark URL looks like:
#   https://<host>/wiki/<node_token>?...     -> wiki node (needs resolving)
#   https://<host>/docx/<document_id>?...    -> docx document (direct)
#   https://<host>/sheets/<spreadsheet>?...  -> spreadsheet (direct)
URL_RE = re.compile(r"/(wiki|docx|docs|sheets|base|file)/([A-Za-z0-9]+)")


class LarkError(RuntimeError):
    pass


def _base_url() -> str:
    return LARK_DOMAIN_HOST.get(os.getenv("LARK_DOMAIN", "lark"), LARK_DOMAIN_HOST["lark"])


def _check(resp: requests.Response) -> dict:
    """Raise on HTTP or Lark business error; return the JSON body."""
    resp.raise_for_status()
    body = resp.json()
    if body.get("code", 0) != 0:
        raise LarkError(f"Lark API error {body.get('code')}: {body.get('msg')}")
    return body


def tenant_access_token() -> str:
    resp = requests.post(
        f"{_base_url()}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": os.environ["LARK_APP_ID"], "app_secret": os.environ["LARK_APP_SECRET"]},
        timeout=30,
    )
    return _check(resp)["tenant_access_token"]


def parse_url(url_or_token: str) -> tuple[str, str]:
    """Return ``(kind, token)``. A bare token is assumed to be a wiki node."""
    m = URL_RE.search(url_or_token)
    if m:
        return m.group(1), m.group(2)
    return "wiki", url_or_token.strip()


def resolve_wiki_node(token: str, access_token: str) -> tuple[str, str]:
    """Resolve a wiki node_token to its underlying ``(obj_type, obj_token)``."""
    resp = requests.get(
        f"{_base_url()}/open-apis/wiki/v2/spaces/get_node",
        params={"token": token},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    node = _check(resp)["data"]["node"]
    return node["obj_type"], node["obj_token"]


def read_docx(document_id: str, access_token: str) -> str:
    resp = requests.get(
        f"{_base_url()}/open-apis/docx/v1/documents/{document_id}/raw_content",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    return _check(resp)["data"]["content"]


def read_docx_title(document_id: str, access_token: str) -> str:
    resp = requests.get(
        f"{_base_url()}/open-apis/docx/v1/documents/{document_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    return _check(resp)["data"]["document"].get("title", "(untitled)")


def read_sheet_meta(spreadsheet_token: str, access_token: str) -> str:
    resp = requests.get(
        f"{_base_url()}/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    ss = _check(resp)["data"]["spreadsheet"]
    return f"Spreadsheet: {ss.get('title')} (token={spreadsheet_token}, url={ss.get('url')})"


def fetch(url_or_token: str) -> str:
    token_kind, token = parse_url(url_or_token)
    access_token = tenant_access_token()

    # Wiki links wrap another object; resolve to the real doc first.
    if token_kind == "wiki":
        obj_type, obj_token = resolve_wiki_node(token, access_token)
    else:
        obj_type, obj_token = token_kind, token

    if obj_type in ("docx", "doc"):
        title = read_docx_title(obj_token, access_token)
        content = read_docx(obj_token, access_token)
        return f"# {title}\n\n{content}"
    if obj_type in ("sheet", "sheets"):
        return read_sheet_meta(obj_token, access_token)
    return f"Unsupported object type: {obj_type} (obj_token={obj_token})"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    # Load .env from the repo root if credentials aren't already set.
    if "LARK_APP_ID" not in os.environ:
        from ..config import default_env_path, load_dotenv
        load_dotenv(default_env_path())
    try:
        print(fetch(argv[1]))
    except (requests.HTTPError, LarkError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
