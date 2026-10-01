# Testing mobile apps

QAJev tests apps on the **iOS Simulator** and on an **Android emulator**: native apps, and websites in the
device's own browser (Safari, Chrome). It uses the same plain-language goals, checks and reports as everywhere
else.

## How it works

- **QAJev starts its own device.** On iOS it makes a throwaway **clone** of a simulator, boots it without a window,
  and deletes it afterwards. On Android it boots an emulator in **read-only** mode, so nothing it does is saved to
  your virtual device. Your own simulators, emulators and phones are never touched.
- **Jev reads text, never pixels.** QAJev reads each screen from the platform's **accessibility tree** (the same
  information VoiceOver and TalkBack use) and describes it as text plus labelled actions: buttons, cells, switches,
  links, text fields. Apps with good accessibility labels are the easiest to test, and a control with no label is a
  real finding.
- **Input goes into the device only:** taps, swipes, the back button and typing, never your Mac's mouse or
  keyboard.
- **Verdicts come from the screen**: which texts it shows, plus app crashes (Android) and your expectations.

## Safety on a device

A native app is not a web page: QAJev **cannot block its network writes**. So, on top of the usual hidden list
(sign out, delete, pay, subscribe...):

- **sign in / sign up / "Continue with Google"** buttons are hidden: signing in is the person's job;
- **Restore purchases** is hidden (it can raise a store sign-in);
- **record, voice and microphone** controls are hidden, and every permission is revoked for the app before it
  starts (the iOS Simulator's microphone is your Mac's real one). On a microphone or speech prompt, only
  **Don't Allow** is ever offered;
- **password and other secure fields** are never offered to Jev, so it never types into them;
- the Android emulator runs with **no audio** at all.

Test against a **test build and a test account** whenever a flow could change data. Against a production backend,
stick to signed-out flows.

## Running it

A target is `ios:` or `android:` followed by the app's bundle id / package name, or a web address:

```bash
# A native app (already installed on the simulator or emulator you name)
qajev play ios:com.example.app --device "iPhone 17" \
  --goal "Open the settings. Stop when the privacy policy link is visible." --expect-text "Privacy"
qajev play android:com.example.app --device Pixel_8 --goal "..." --expect-text "..."

# A website in the device's browser (your Mac's localhost works on both)
qajev play ios:http://127.0.0.1:8765/ --goal "Find the Pro price. Stop when it is visible." \
  --expect-text '$29 per month'
qajev play android:https://example.com/ --goal "..." --expect-text "..."
```

`--install path/to/app.apk` (or a simulator `.app`) installs the app on the throwaway device first, so nothing of it
outlives the run (`install:` in a suite). `--device` picks the iOS simulator to clone (its name or UDID) or the Android virtual device to boot; without it
QAJev uses the first iPhone simulator, or the first virtual device. A suite works too (`qajev play ios:... --suite
app.yaml`), with `device:` in the file; see [Games](games.md#a-whole-session-menus-and-real-time-play) for the
format.

Besides the app's own controls, Jev can always **scroll down**, **scroll up**, and on Android press **back**.

## Websites in a real device browser (opt-in)

Every website test already runs in a **phone view** in Chrome (see [Writing tests](writing-tests.md#desktop-and-phone)).
Some problems only show in a phone's real browser: Safari's own rendering and quirks, the real keyboard, the
browser's toolbars. To catch those too, add real device browsers:

```bash
qajev run shop.yaml --real-devices android        # also Chrome on a read-only Android emulator
qajev run --project shop --real-devices android
```

or `real_devices: [ios, android]` in a suite or project, or `QAJEV_REAL_DEVICES=ios`. Each device browser runs the
same scenarios as one session after the Chrome runs, named `... (iOS Safari)` / `... (Android Chrome)` in the same
report. Choose the simulator or virtual device with `QAJEV_IOS_DEVICE` / `QAJEV_ANDROID_AVD`.

What differs from Chrome, and why:

- **iOS Safari cannot be checked yet.** On the iOS Simulator, the accessibility reader QAJev uses (idb) sees only
  Safari's own controls, not the page, which Safari draws in another process. `--real-devices ios` runs, but its
  copies come out `harness` with that reason, never as a failure of your site. Native iOS apps are not affected.

- **Read-only only.** A device browser has no in-page guard, so scenarios that change data (`mode: mutate`) are
  skipped there (the report says why), and write-like buttons (save, submit, send, delete...) are hidden from Jev.
- **Only the site, not the browser.** The browser's own controls (address bar, tabs, share, bookmarks) are hidden
  from Jev, so it can only move through the site's own links and buttons.
- **Text checks only.** `text` and `visible` carry over; `url`, `js` and `fetch` need the page's internals, which a
  device browser does not expose, so they run on desktop and phone view only.
- **Slower and heavier.** Booting a device takes a minute or more, so it is a separate, opt-in run.

## Requirements

- **iOS:** Xcode with an iOS simulator, and [idb](https://fbidb.io) (`brew install idb-companion` and
  `pipx install fb-idb`).
- **Android:** the Android SDK (emulator and platform-tools) and at least one virtual device. Set `ANDROID_HOME` if
  the SDK is not in `~/Library/Android/sdk`.
- The app installed on the simulator or virtual device QAJev starts from.

## Good to know

- Booting a device is heavy: QAJev runs one at a time, like every run (see [Jobs](jobs-and-top.md)).
- If a run dies, the next run deletes the simulator clone or stops the emulator it left behind.
- Never plug a phone in and point QAJev at it: QAJev only drives the emulators and simulator clones it started.
