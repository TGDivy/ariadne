# Browser control operations

Ariadne's browser capability is an opt-in private Chromium daemon. It keeps login
profiles alive across Iris turns, exposes a compact semantic MCP surface only to
selected profiles, and provides a separate JSON administrative CLI. It is not a
public remote-browser service and does not grant new authority.

## Install the browser runtime

From the deployed Ariadne checkout, install Python dependencies and Playwright's
Chromium build and Linux libraries:

```bash
UV_SYSTEM_CERTS=true uv sync --locked --all-groups
uv run playwright install --with-deps chromium
```

The second command uses the host package manager for Chromium's shared-library
dependencies. Repeat it after a Playwright upgrade. A system Chromium can instead be
selected with `[browser].executable_path`, but the configured path must be a real
file and remains an operator choice.

`tests/test_browser_runtime.py` drives real Chromium against a local fixture site. It
skips itself with an explanatory reason when that build is absent, so a host without
Chromium still gets a green suite instead of an unrelated failure. Set
`ARIADNE_REQUIRE_BROWSER_TESTS=1` wherever the browser really is provisioned—the home
server, and CI once the step below exists—so a missing build fails loudly instead of
quietly reducing coverage.

CI does not yet install Chromium, so those integration tests currently skip on GitHub
Actions. Add this step to the `test` job in `.github/workflows/ci.yml` to run them
there:

```yaml
      - run: uv run playwright install --with-deps chromium
        # ...before `uv run pytest`, and set on that step:
        #   env:
        #     ARIADNE_REQUIRE_BROWSER_TESTS: "1"
```

## Configure private paths

Keep every path outside the source checkout and owned by the Unix account running
Ariadne:

```toml
[browser]
enabled = true
state = "~/.local/state/ariadne/browser/browser.sqlite3"
profiles = "~/.local/share/ariadne/browser/profiles"
artifacts = "~/.local/state/ariadne/browser/artifacts"
socket = "~/.local/state/ariadne/browser/browser.sock"
headless = false
takeover_url = "https://browser.home.private"
lease_seconds = 900
action_timeout_seconds = 30
observation_max_chars = 12000
observation_max_elements = 120
journal_retention_days = 14
journal_max_entries = 5000
```

The runtime creates directories as mode `0700`, the socket and state database as
`0600`, and retained screenshots/downloads as `0600`. `takeover_url` is descriptive:
Ariadne does not install or authenticate a desktop gateway. Point it at an existing
VNC/noVNC/RDP view available only through the owner's authenticated private network or
tunnel. Do not place credentials or access tokens in that URL.

Normal deployment is headful. Provide a stable `DISPLAY` and, where needed,
`XAUTHORITY` from the private desktop session used by the takeover view. Headless mode
exists for disposable CI profiles and does not provide human takeover.

## Run it as a user service

Run the browser daemon separately and before `ariadne serve`. A representative user
unit is:

```ini
[Unit]
Description=Ariadne private Chromium service
After=graphical-session.target network-online.target

[Service]
Type=simple
WorkingDirectory=/srv/ariadne
ExecStart=/srv/ariadne/.venv/bin/ariadne-browser --config /home/USER/.config/ariadne/config.toml serve
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
```

Use the real checkout, home, display, and optional `XAUTHORITY` paths. Do not put a
token in the unit. Start it and create an explicitly named profile:

```bash
systemctl --user enable --now ariadne-browser.service
ariadne-browser health
ariadne-browser profile create personal
ariadne-browser profile list
```

Open the private visual view and log into sites manually in `personal`. Passwords,
MFA values, and payment-card data remain in the browser/site; the model typing tool
rejects those fields. Do not run two Chromium processes against the same profile.

## Runtime contract

One task at a time owns a profile. Every operation renews a bounded lease; a competing
task gets truthful busy state. Expired reversible ownership can be recovered after a
crash without deleting the profile. Human takeover pauses all model mutations and
resuming it invalidates old semantic references. Takeover itself is never approval to
submit a consequential action.

The model inspects bounded page text and interactive semantic references, then uses a
fresh reference for one action. Page changes invalidate references. Screenshots are
private fallback evidence; coordinate interaction requires a screenshot from the
current revision and must not be used for uncertain final submission. Password and
payment values are excluded from observations, and the action journal contains only
task/time/origin/operation/outcome plus deliberately retained artifact paths—not DOM,
keystrokes, cookies, or request bodies.

Downloads stay inert in the private artifact directory until another explicit
Ariadne flow reads or shares them. Uploads must name one intended local file. Dialogs,
pop-ups, downloads, and new origins are surfaced. Ariadne dismisses unexpected browser
dialogs rather than silently accepting them.

## Backup and recovery

The SQLite database and profile directory are durable. The artifact directory is
bounded operational evidence and should normally be excluded from backup. If profiles
are backed up, stop the browser service first so Chromium's files are consistent, use
encrypted owner-controlled storage, and do not copy them into a Git repository.

After an ordinary crash, restart the daemon and reattach or start a task. It releases
only expired reversible/takeover leases. A consequential action whose response was
lost remains `uncertain` even after lease expiry. Inspect the site's definitive order,
application, message, or account history before resolving it; never retry merely
because the client timed out. Use `ariadne-browser journal --limit 100` for concise
recovery evidence and `ariadne-browser recovery list` for blocked operations. After
the owner has checked definitive site history, record exactly one outcome with
`ariadne-browser recovery resolve OPERATION_ID --outcome confirmed_succeeded` or
`--outcome not_submitted`; this releases the profile but never retries the action.
`ariadne-browser shutdown` performs a deliberate clean shutdown without deleting
profiles.

## Exact home-server smoke tests

These checks remain live-only; passing CI does not claim them:

1. Run the full repository checks and
   `ARIADNE_REQUIRE_BROWSER_TESTS=1 uv run pytest tests/test_browser_runtime.py` with a
   disposable profile. Confirm fixture navigation, form filling, upload/download,
   dialog, pop-up, screenshot, takeover, and persistence tests pass rather than skip.
2. Start the headful user service, run `ariadne-browser health`, and verify the socket
   is mode `0600`, private directories are `0700`, and the private view is unreachable
   outside the authenticated network/tunnel.
3. Create `personal`, manually sign into a harmless account through takeover, restart
   the daemon, and confirm the session remains signed in. Do not give credentials or
   MFA codes to Iris.
4. Start a disposable task, request takeover, verify agent mutation is rejected while
   the human controls the page, then release takeover and verify old references fail.
5. Start two disposable tasks against one profile and confirm the second receives
   busy state. Release or expire the first lease and confirm the second can proceed.
6. Browse one real low-risk site and confirm new origins, a pop-up, and a harmless
   download are surfaced and journalled without sensitive content.
7. On Waitrose, search and build a small temporary basket, exercise takeover, then
   remove/cancel it. Do not enter checkout or place an order as part of this browser
   capability smoke test.
8. Stop the daemon mid-way through a reversible disposable task, restart it, and
   confirm the profile and login survive. Exercise uncertain-final-action recovery
   only on the local fixture—not a real commitment.

Record the date, host revision, Chromium/Playwright versions, and pass/fail result in
private deployment notes. Do not commit screenshots, profiles, URLs containing private
identifiers, or live account details.
