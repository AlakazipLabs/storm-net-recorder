# Notifications

The recorder does not know what Telegram, Slack, or email are. It builds an alert object
and hands it to whichever transport `config.toml` names.

## The payload

Identical in every mode:

```json
{
  "source": "storm-net-recorder",
  "level": "warn",
  "title": "Severe Thunderstorm Warning — recording started",
  "text": "Severe Thunderstorm Warning for Somewhere County\nUntil 18:00. Recording and transcribing 147.180M.\nTranscript: /home/you/.local/share/stormnet/transcripts/stormnet_2026-07-06.txt",
  "ts": "2026-07-06T18:41:02.115377+00:00",
  "dedup_key": "stormnet:urn:oid:2.49.0.1.840.0.abc123"
}
```

`level` is `info`, `warn`, or `crit`, mapped from the event by `nws.trigger_events`.
`dedup_key` is stable for a given alert, so a consumer that has already delivered one can
drop repeats. `text` is truncated to `notify.text_max` before it is handed over.

You get an alert when a warning starts, when recording is deferred because you hold the
receiver, and once when everything expires.

## mode = "none"

Alerts are written to the log and nothing else. This is the default, and it is a
reasonable permanent choice if you read transcripts after the fact rather than wanting a
phone buzz during.

## mode = "command"

```toml
[notify]
mode = "command"
command = ["/usr/local/bin/my-notifier", "--from", "stormnet"]
```

The program is executed with the alert JSON on stdin. It must be a **list** of arguments,
not a string — no shell is involved, so alert text containing quotes or semicolons cannot
turn into shell syntax. A non-zero exit is logged as a failed notification. The program is
killed after 30 seconds.

A minimal example:

```sh
#!/bin/sh
# my-notifier — read the alert, do something with it
python3 -c 'import json,sys; a=json.load(sys.stdin); print(a["level"], a["title"])'
```

## mode = "webhook"

```toml
[notify]
mode = "webhook"
url = "https://example.com/hooks/stormnet"
```

An HTTPS POST with `Content-Type: application/json`. Plain HTTP is rejected at config
load: these alerts describe where you are and what you are monitoring, and they should
not cross the network in the clear. Any 2xx counts as delivered.

## mode = "spool"

```toml
[notify]
mode = "spool"
spool_dir = "~/.local/share/my-bot/spool"
```

One JSON file per alert, written atomically — unique temp name, fsync, rename — into a
directory that your own process watches, consumes, and deletes. The directory is created
mode 0700 and the files 0600.

Filenames are millisecond-epoch plus a sequence number, so a lexical sort is arrival
order.

This mode exists because it is the right shape for a home setup that already has a
notification path. The process holding your bot token stays separate, keeps its
credentials to itself, and this daemon never sees them. It also means a notification
backlog survives a restart of either side.

## Failures

A notification failure never propagates. It is logged, `send()` returns `False`, and the
watcher carries on — recording matters more than telling you about it.
