# storm-net-recorder

When the National Weather Service issues a severe thunderstorm or tornado warning for
your area, this starts recording your local SKYWARN net off an RTL-SDR, transcribes it
locally with Whisper, and stops on its own when the warning expires.

You do not have to be home. You do not have to remember. The transcript is waiting
afterward, timestamped, searchable, and never leaves your machine.

```
NWS alert poll ─▶ warning matches? ─▶ notify ─┐
                                              ├─▶ rtl_fm (squelched) ─▶ chunker ─▶ whisper ─▶ transcript
                          end time + grace ◀───┘
```

## Is this what you were looking for?

Plain statements of what it does, in case you arrived here searching for one of them:

- Automatically record a ham radio repeater when a severe weather warning is issued
- Trigger an RTL-SDR recording from the NWS `api.weather.gov` alerts endpoint
- Transcribe amateur radio net audio locally with whisper.cpp, offline
- Get Whisper to stop mangling callsigns and local place names (seed the decoder — see
  `whisper.vocab_prompt`)
- Run `rtl_fm` unattended under launchd and have it stop by itself
- Share one SDR dongle between a background service and your own listening, without
  either killing the other
- Find the right gain and squelch for a distant repeater by measurement rather than guesswork
  (`tools/analyze_gain_ladder.py`)

What it is **not**: a scanner, a decoder for digital modes, an APRS or SAME/EAS decoder, or
anything that transmits. It listens to one analog FM frequency and writes text.

## Why this exists

Storm nets happen exactly when you are busiest. The information on them — hail size at a
specific intersection, rotation someone can actually see, which roads are underwater — is
perishable and almost never written down. Recording by hand means being at the radio
before the net starts, which is precisely when you are moving patio furniture.

The trigger is the NWS alert polygon for your point, so the tape starts on the same
signal that starts the net.

## What makes it safe to leave running

**It stops by itself.** The recorder watches an end-time file that the watcher keeps
updated, so overlapping warnings extend the session and the last one to expire ends it,
plus a grace period. Nothing runs forever because a warning was cancelled oddly.

**It yields the receiver to you.** If you are already listening by hand, the watcher does
not take the dongle. It notes the end time, tells you once that it is not recording, and
starts the moment your session ends. `tools/listen.sh` refuses to interrupt an active
recording unless you pass `FORCE=1`. One receiver, two claimants, no surprises — a
monitoring tool that kills your radio during a tornado warning is worse than no tool.

**It never holds a credential.** Notifications go out through a transport you choose,
including one that just drops a JSON file in a directory for some other process of yours
to pick up. This daemon has no tokens to leak.

**A failed notification is not a failed recording.** Notifier errors are logged and
swallowed. Losing an alert is bad; crashing the watcher mid-storm is worse.

## Requirements

- Python 3.11+ (no Python dependencies for the daemon — it is standard library only)
- An RTL-SDR dongle and [librtlsdr](https://github.com/librtlsdr/librtlsdr) (`rtl_fm`)
- [ffmpeg](https://ffmpeg.org/)
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (`whisper-cli`) and a model file
- `sox` if you want `tools/listen.sh` to play audio
- A 2 m/70 cm antenna that can actually hear your repeater. This is the part people
  underestimate; see *Antenna first* below.

## Setup

```bash
git clone https://github.com/AlakazipLabs/storm-net-recorder.git
cd storm-net-recorder
python3 -m venv venv
source venv/bin/activate
pip install .

cp config.example.toml config.toml
$EDITOR config.toml          # your point, your repeater, your contact address
```

Fetch a Whisper model once (`medium.en` is 1.5 GB and is what this was tuned against;
`small.en` is 466 MB and noticeably worse on weak net audio):

```bash
mkdir -p ~/.local/share/stormnet/models
curl -L -o ~/.local/share/stormnet/models/ggml-medium.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin
```

Then confirm you are actually ready:

```bash
stormnet-watch --check
```

`--check` is the whole pre-flight. It validates the config, resolves `rtl_fm`, `ffmpeg`
and `whisper-cli`, and confirms the model file and the recorder script are both really
there — exiting non-zero with a `NOT READY` list if anything is missing. Run it now, on a
clear day. The alternative is discovering a missing piece while a tornado warning is
active, which is the failure mode this whole project exists to avoid.

## Configuration

Everything site-specific lives in `config.toml`. The example file explains each field;
the four that matter most:

| Field | What it does |
|---|---|
| `location.latitude` / `longitude` | The point NWS alerts are checked against |
| `nws.contact` | Your email. api.weather.gov requires a contact in the User-Agent |
| `sdr.frequency` | The repeater output carrying your net |
| `whisper.vocab_prompt` | Local callsigns and place names, seeded into the decoder |

That last one is the single biggest accuracy win available. Whisper transcribing raw
amateur radio audio will mangle callsigns and county names it has no reason to expect.
Give it the vocabulary and the same audio comes back materially cleaner. It is also
completely specific to you, which is why it is a config field and not a constant.

By default only Severe Thunderstorm and Tornado **Warnings** trigger. Watches are
deliberately excluded: a watch means conditions are favorable, not that a net is running,
and you would be transcribing an empty channel for six hours.

## Running it

Manually, in a terminal:

```bash
stormnet-watch
```

As a background service on macOS, edit and install the launchd template:

```bash
cp launchd/com.example.stormnet-watch.plist ~/Library/LaunchAgents/
$EDITOR ~/Library/LaunchAgents/com.example.stormnet-watch.plist   # paths, label
launchctl load ~/Library/LaunchAgents/com.example.stormnet-watch.plist
```

Transcripts land in `<paths.home>/transcripts/stormnet_<date>.txt`, one block per audio
chunk, headed with the time that chunk started.

## Notifications

Four modes, set `notify.mode` in the config: `none` logs only; `command` executes a
program you name with the alert JSON on stdin; `webhook` POSTs the JSON over HTTPS;
`spool` atomically writes one JSON file per alert into a directory for a separate process
to consume and delete. See [docs/notify.md](docs/notify.md) for the payload and examples.

`spool` is there because the useful pattern is decoupling. Whatever holds your bot
credentials stays a separate process, and this one stays credential-free.

## Antenna first

Before tuning anything in software, be honest about whether your antenna hears the
repeater. On the setup this was built for, the channel measured 4–6 dB of speech-to-hiss —
antenna-limited, with software already exhausted. No gain setting, filter, or denoiser
recovers a signal that is not arriving.

`tools/analyze_gain_ladder.py` will tell you where you actually stand. Capture the same
signal at several gain settings with `tools/capture.sh`, run the analyzer over them, and
read the SNR proxy column. Two things usually surprise people: maximum gain is not best
gain, because past a point the tuner amplifies noise faster than signal; and run-to-run
drift of a couple of dB is normal, so only compare captures from one sitting.

The defaults in `config.example.toml` (gain 42, squelch 50, with `-F 9 -E deemp`) are a
measured starting point from one indoor antenna, not universal truth. Measure yours.

## Listening by hand

```bash
sh tools/listen.sh 147.180M monitor    # a distant repeater or net
sh tools/listen.sh 162.475M wx         # NOAA weather broadcast
sh tools/listen.sh 462.5625M           # a handheld a few feet away (default profile)
```

The default profile is deliberately deaf to distant signals — low gain so a nearby
transmitter cannot clip the front end, squelch up so hiss stays quiet. Point it at a
repeater and you will hear nothing and conclude the radio is broken. Use `monitor` for
anything you are not transmitting yourself, and expect hiss between transmissions; that
is squelch working.

If your dongle lives on a different machine, set `STORMNET_SSH_HOST=user@host` and the
capture half runs there while audio plays locally.

## Limitations

- **English-first.** The default Whisper models and the noise-marker filtering assume it.
- **One receiver, one frequency.** A single dongle cannot cover the net and NOAA at once.
- **No speaker labels.** Output is timestamped blocks of text, not a roster.
- **Whisper on weak audio is imperfect** and gets worse as the signal does. Transcripts
  are a searchable record, not an official log — for anything that matters, the audio in
  `captures/` is the ground truth.
- **macOS-oriented.** The launchd template is macOS; the Python and shell are portable,
  but nothing else has been exercised on Linux.
- **US only.** It is built on api.weather.gov.

## Tests

```bash
python3 -m unittest discover -s tests
```

Fully mocked: no network, no SDR, no whisper, no ffmpeg.

## Contact

Issues and pull requests are welcome. For anything that does not belong in a
public issue: **github@youngnetwork.org**

## License

MIT — see [LICENSE](LICENSE).
No strings beyond the license file, and no policing. If this saves you an
afternoon, that is the whole point. Credit is appreciated and never demanded; if
you build something better on top of it, that is the best outcome available.

Amateur radio operation is subject to your own licensing and your local band plan.
Recording and republishing net traffic is a courtesy question as much as a legal one;
ask your net control before publishing transcripts of their net.
