# Permissions

Clueless asks macOS for up to three permissions. The walkthrough requests each
one and shows a live level meter, so you can see it working before a real call.

| Permission | Used for | Required? |
|---|---|---|
| Microphone | Your side of the conversation | Yes |
| System Audio Recording | The other people on the call | Yes |
| Screen Recording | Reading text off your screen with `⌘⌥4` | Optional |

Change them later in **System Settings → Privacy & Security**. The walkthrough
has buttons that open each pane.

## Why system audio is a separate prompt

macOS 26 records other apps' audio through a *process tap*, and that needs its
own grant: **"Clueless" would like access to record your system audio.** The
capture runs in a small helper app, `CluelessTap.app`, so the prompt and its
entry in System Settings may show either name.

There are two helpers, one per stream, launched in two different ways:

| Stream | How it starts | Which grant it uses |
|---|---|---|
| Microphone | As a child process of Clueless | Clueless's Microphone grant |
| System audio | Through LaunchServices, as its own app | The helper's own System Audio grant |

That split isn't arbitrary. macOS attributes a permission request to the
*responsible process*, the top-level app the request came from:

- Asked for by a child process, the microphone grant is inherited from
  Clueless, which is what you want.
- macOS won't prompt for system audio when a child process asks. The helper
  has to be launched as its own app to get a prompt.

## Silent failures, and how they are caught

**macOS doesn't report a denied audio permission as an error.** It delivers
audio buffers full of zeros. Everything appears to work and nothing is heard.
Clueless guards against this:

- The walkthrough's meters show real levels, so a denied permission shows up as
  a flat meter, not as a silent failure during a call.
- The system-audio check plays a short sound while it listens, so it
  doesn't depend on something else happening to be playing.
- A tap that receives only zeros reports `SILENT` (exit code 7) rather than
  success.

## Developers: things that break permissions

These only matter when running from source.

- **Starting Electron from a terminal.** The terminal becomes the responsible
  process, so the app gets the terminal's permissions instead of its own. Most
  terminals declare microphone access but not system audio, so the mic works
  and system audio stays silent. Use `npm run start:ls`, which launches through
  LaunchServices.
- **Ad-hoc signing.** With no stable signing identity, macOS keys the grant to
  the exact binary hash, and asks again after every rebuild. Run
  `native/setup-signing.sh` once.
- **`open -a` instead of `open -n -a`.** LaunchServices reuses a running
  instance and ignores the new arguments.
- **A prompt that never appears.** While a permission dialog is open, the
  helper waits inside `AudioDeviceStart`. A helper that seems to hang after
  "created aggregate device" is waiting for you to answer a dialog, which may be
  behind other windows.

To reset a grant and test the prompt again:

```bash
tccutil reset AudioCapture com.local.clueless.tap
tccutil reset Microphone com.clueless.app
```

To check both streams from the command line, the check must itself be started
through LaunchServices:

```bash
cd overlay
open -n -a node_modules/electron/dist/Electron.app --args "$PWD" --check-permissions
```

## Screen Recording

Screen context is captured once, when you turn it on with `⌘⌥4`, never
continuously. Reading the text uses Apple Vision on-device and needs no
permission. Only taking the screenshot does. Without the permission, macOS
returns a black image rather than an error, so Clueless reports "no text found
— grant Screen Recording" instead of quietly sending an empty screen.
