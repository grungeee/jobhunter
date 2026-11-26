#!/usr/bin/env python3
"""Integrate the public Jobhunter demo into an existing website/API repo.

Required environment variables:
  JOBHUNTER_SITE_REPO        path to the existing website/API Git checkout
  JOBHUNTER_PUBLIC_API_URL   HTTPS API base ending in /api/v1/jobhunter

The script only copies named frontend files and patches the router include and
nginx rate limits. It does not commit, push, deploy, or modify any secrets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_FILES = ("index.html", "app.js", "style.css")


def fail(message: str) -> None:
    raise SystemExit(f"publish-site: {message}")


def repo_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True,
    )
    if result.returncode:
        fail(f"not a Git checkout: {path}")
    return Path(result.stdout.strip()).resolve()


def validate_api_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        fail("JOBHUNTER_PUBLIC_API_URL must be an HTTPS URL without embedded credentials")
    if parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/api/v1/jobhunter":
        fail("JOBHUNTER_PUBLIC_API_URL must end exactly in /api/v1/jobhunter")
    return value.rstrip("/")


def patch_api_app(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    import_line = "from api.jobhunter import router as jobhunter_router\n"
    include_line = "app.include_router(jobhunter_router)\n"
    import_anchor = "from starlette.middleware.trustedhost import TrustedHostMiddleware\n"
    if import_line not in text:
        if text.count(import_anchor) != 1:
            fail("could not find a unique trusted-host import anchor in api/app.py")
        text = text.replace(import_anchor, import_anchor + import_line, 1)
    if include_line not in text:
        matches = list(re.finditer(r"^app = FastAPI\([^\n]*\)\n", text, re.MULTILINE))
        if len(matches) != 1:
            fail("could not find a unique FastAPI app declaration in api/app.py")
        match = matches[0]
        text = text[:match.end()] + include_line + text[match.end():]
    if text.count(import_line) != 1 or text.count(include_line) != 1:
        fail("router include is duplicated")
    path.write_text(text, encoding="utf-8")


def patch_nginx(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    old_auth = "location ~ ^/api/v1/(register|login|account/) {"
    new_auth = "location ~ ^/api/v1/(register|login|account/.*|jobhunter/login)$ {"
    auth_marker = old_auth if old_auth in text else new_auth
    if text.count(auth_marker) != 1:
        fail("could not find a unique API auth rate-limit location")
    block_start = text.index(auth_marker) + len(auth_marker)
    block_end = text.find("}", block_start)
    if block_end < 0:
        fail("could not read the API auth rate-limit location")
    auth_zones = re.findall(r"limit_req\s+zone=([A-Za-z0-9_-]+)", text[block_start:block_end])
    if len(auth_zones) != 1:
        fail("could not identify the rate-limit zone used for API authentication")
    auth_zone_name = auth_zones[0]

    visits_zone_name = "jobhunter_visits"
    visits_zone = f"limit_req_zone $binary_remote_addr zone={visits_zone_name}:10m rate=6r/m;"
    visit_zone_lines = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("limit_req_zone $binary_remote_addr zone=")
        and line.strip().split("zone=", 1)[1].split(":", 1)[0] == visits_zone_name
    ]
    if visit_zone_lines and visit_zone_lines != [visits_zone]:
        fail("an existing Jobhunter visit rate-limit zone has a different definition")
    if not visit_zone_lines:
        newline = chr(10)
        auth_zone_lines = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith("limit_req_zone $binary_remote_addr zone=")
            and line.strip().split("zone=", 1)[1].split(":", 1)[0] == auth_zone_name
        ]
        if len(auth_zone_lines) != 1:
            fail("could not find the unique rate-limit zone used for API authentication")
        text = text.replace(auth_zone_lines[0], auth_zone_lines[0] + newline + visits_zone, 1)

    if old_auth in text:
        if text.count(old_auth) != 1:
            fail("auth rate-limit location is ambiguous")
        text = text.replace(old_auth, new_auth, 1)
    elif new_auth not in text:
        fail("could not find the API auth rate-limit location")

    proxy_prefix = "proxy_pass http://"
    proxy_targets = {
        line.strip()[len(proxy_prefix):-1]
        for line in text.splitlines()
        if line.strip().startswith(proxy_prefix) and line.strip().endswith(";")
    }
    if len(proxy_targets) != 1:
        fail("could not identify a unique API upstream in nginx config")
    api_upstream = next(iter(proxy_targets))
    newline = chr(10)
    visit_block = newline.join((
        "    location = /api/v1/jobhunter/visit {",
        f"        limit_req zone={visits_zone_name} burst=4 nodelay;",
        "        limit_req_status 429;",
        f"        proxy_pass http://{api_upstream};",
        "        access_log off;",
        "    }",
        "",
        "",
    ))
    if "location = /api/v1/jobhunter/visit" not in text:
        if text.count(new_auth) != 1:
            fail("could not insert visitor location before auth rate limiting")
        text = text.replace(new_auth, visit_block + new_auth, 1)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_repo", nargs="?", help="existing website/API repository path")
    args = parser.parse_args()
    site_arg = args.site_repo or os.environ.get("JOBHUNTER_SITE_REPO")
    api_url_arg = os.environ.get("JOBHUNTER_PUBLIC_API_URL", "")
    if not site_arg:
        fail("set JOBHUNTER_SITE_REPO or pass the site repository path")
    if not api_url_arg:
        fail("set JOBHUNTER_PUBLIC_API_URL in the deployment environment")
    api_url = validate_api_url(api_url_arg)
    requested = Path(site_arg).expanduser().resolve()
    site = repo_root(requested)
    nginx_configs = sorted((site / "deploy").glob("nginx-*-api.conf"))
    if not (site / "api" / "app.py").is_file() or len(nginx_configs) != 1:
        fail("checkout must contain api/app.py and exactly one deploy/nginx-*-api.conf")
    nginx_config = nginx_configs[0]

    target_web = site / "jobhunter"
    target_web.mkdir(parents=True, exist_ok=True)
    for name in FRONTEND_FILES:
        source = ROOT / "web" / name
        if not source.is_file():
            fail(f"missing frontend file: {source}")
        shutil.copy2(source, target_web / name)
    config = "/* Generated by deploy/publish-site.py. Do not place secrets here. */\n"
    config += f"window.JOBHUNTER_API_BASE = {json.dumps(api_url)};\n"
    (target_web / "config.js").write_text(config, encoding="utf-8")

    source_api = ROOT / "api" / "jobhunter.py"
    if not source_api.is_file():
        fail("missing api/jobhunter.py")
    shutil.copy2(source_api, site / "api" / "jobhunter.py")
    patch_api_app(site / "api" / "app.py")
    patch_nginx(nginx_config)

    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(site / "api" / "app.py"), str(site / "api" / "jobhunter.py")],
        text=True, capture_output=True,
    )
    if compile_result.returncode:
        fail(compile_result.stderr.strip() or "Python syntax check failed")
    print("Integrated Jobhunter into the existing site/API checkout:")
    for path in (
        target_web / "index.html", target_web / "app.js", target_web / "style.css", target_web / "config.js",
        site / "api" / "jobhunter.py", site / "api" / "app.py", nginx_config,
    ):
        print(f"  {path.relative_to(site)}")
    print("No commit, push, deploy, or secret-file changes were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
