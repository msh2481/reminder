reminder
========

![Logo](logo.webp)

Setting up the Google Calendar API
----------------------------------

1. Go to https://console.cloud.google.com/ and either select an existing project or create a new one (e.g "personal-stuff"), then select it.
2. Left sidebar > "APIs & Services" > Library > Search for and enable the Google Calendar API
2. Left sidebar > "Credentials" > "Create Credentials" > "OAuth Client ID"
3. If your consent screen isn't configured, click the "Configure consent screen" button. Pick between Internal and External per your preference.
4. Configure the consent screen:
    * App name: gcal-tui (or whatever you want)
    * User support email: (your email)
    * Developer contact information: (your email)
5. Once at the scopes screen, click "Add or Remove Scopes". Type `calendar.events` and select the googleapis.com URL that appears. Click the checkbox to the left of the item you've just added.
6. Save and continue. Add your email to the Test Users list. Save and continue.
7. You're almost done. Click 'Back to Dashboard', then go back to the Credentials screen.
8. Click "Create credentials" > "OAuth Client ID" > "Desktop app"
9. On the "OAuth client created" screen that pops up, click "Download JSON".

Save the file as `credentials.json` in the root of the project.

Running the MVP
---------------

This MVP is split into two processes:
- a foreground **daemon** that talks to Google Calendar and listens on a Unix socket
- short-lived **CLI commands** that send one JSON request and print a response

Start the daemon (foreground)
-----------------------------

In one terminal:

```bash
uv run python main.py start
```

Query the next N events (next 30 days)
-------------------------------------

In another terminal:

```bash
uv run python main.py next 5
```

Trigger a reminder (spawns iTerm2)
---------------------------------

```bash
uv run python main.py test
uv run python main.py test --important
```

When triggered, the daemon:
- plays `beep.wav` (via `afplay`)
- spawns a new iTerm2 window to run `main.py show <id> [--important]`

Notes
-----
- Socket path: `/tmp/remind.sock`
- Acknowledgements are **in-memory only** for MVP (they reset when the daemon stops).
- macOS terminal spawning currently targets **iTerm2** via `osascript`.
