# Jobhunter

A local-first job-search workspace with a terminal interface, a browser view and a self-contained, password-gated demo. The public demo uses synthetic profiles and fictional listings only. It does not include a real CV, candidate profile, application history, contacts, or notes.

## Demo behavior

- Select one or more sample portals. AMS is selected by default.
- Select a synthetic profile. The fictional industrial-operations profile is a generic example, not copied from a person's CV.
- Parse local HTML fixtures and show the three newest matching examples for each selected portal.
- Filter and sort results, record the workflow stage, bookmark listings, add browser-local notes, and undo the last stage change.
- Generate and revise a fictional application-draft template, keep local versions, copy/download a draft, and export the local tracker.
- Receive one minimal Telegram visit alert at most once per 10-minute window when the demo page is opened. The alert does not include a visitor name, IP address or page content.

**Live portal scraping is disabled.** The parser operates on locally generated synthetic HTML; it makes no requests to job portals. At the time this demo was prepared, the reviewed AMS robots rules and karriere.at terms did not permit automated collection for this use. The terms for other portals have not been reviewed. Do not enable live collection unless an official feed/API or written authorization permits it.

The public browser view is a demo subset of the TUI workflow. Stage tracking, bookmarks, notes, drafts and versions are local to each visitor's browser and are not written to the server. It does not submit applications, contact employers or call an AI model.

## Run locally

Requires Python 3.10+ and Node.js for the JavaScript syntax check.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r api/requirements.txt
export JOBHUNTER_PASSCODE="$(python3 -c 'import getpass; print(getpass.getpass())')"
export JOBHUNTER_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export JOBHUNTER_ALLOWED_ORIGINS="http://127.0.0.1:4173"
export JOBHUNTER_COOKIE_SECURE=false # loopback-only HTTP development
uvicorn api.app:app --host 127.0.0.1 --port 8768
```

In another terminal, serve the static page:

```sh
python3 -m http.server 4173 --bind 127.0.0.1 --directory web
```

Open `http://127.0.0.1:4173/`. The development `web/config.js` points to the local API. The Telegram alert is unavailable unless `TELEGRAM_BOT_TOKEN` and `TELEGRAM_HOME_CHANNEL` are present in the API process environment.

## Tests

```sh
python3 -B -m unittest discover -s tests -v
node --check web/app.js
sh -n ui/jobhunter ui/jobui
```

## Deploy to an existing website/API checkout

`deploy/publish-site.py` integrates the named frontend files and API router into an existing site/API Git checkout. It does not commit, push, restart services or touch secret files.

```sh
JOBHUNTER_SITE_REPO=/path/to/site-api-checkout \
JOBHUNTER_PUBLIC_API_URL=https://api.example.test/api/v1/jobhunter \
python3 deploy/publish-site.py
```

The integration adds `jobhunter/` static assets, mounts `api/jobhunter.py`, and adds nginx rate limits for login and visit requests. Review the exact changed paths before committing. The site/API checkout must allow the website origin in its CORS settings. Its API environment must set `JOBHUNTER_PASSCODE`, `JOBHUNTER_SESSION_SECRET`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_HOME_CHANNEL`. Keep all of these in the server's protected environment file; never put them in this repository, the static page or GitHub Actions.

The four-digit demo code is a lightweight access barrier, not strong authentication. Keep the API rate limit enabled and do not expose personal application data through this demo.

## Project structure

- `tools/`: sanitized terminal UI, feed, application-draft and tracker tools.
- `ui/`: shared interface definition, schema, sample context and launchers.
- `ams/`: portable tracker implementation. Runtime state belongs outside the public repository.
- `api/`: fixture-only demo API, signed short-lived session cookie, rate-limited visit alert.
- `web/`: static browser demo.
- `tests/`: unit tests for the TUI utilities, demo API and deployment integration.
- `deploy/`: integration helper for an existing site/API repository.

The project deliberately excludes personal profiles, CVs, applications, generated packets, scanner state and credentials. The `.gitignore` keeps common runtime/private paths out of future commits; still review the staged file list and run a secret/PII scan before publishing changes.

## License

MIT. See `LICENSE`.
