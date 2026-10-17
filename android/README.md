# Loomy2API for Android

Native (Jetpack Compose) client that runs the Python gateway **inside the app**
via [Chaquopy](https://chaquo.com/chaquopy/), so a phone can serve the same
OpenAI/Anthropic-compatible API as the desktop gateway.

The Python package is **not** copied into this directory: the Gradle task
`syncPythonPackage` copies `../loomy2api` (the repository root package) into
`../loomy2api-pkg` on every build. Edit the Python code in the repository root
only — there is nothing to keep in sync by hand.

## Build

Requirements: JDK 17, Android SDK 34, and a CPython **3.10** interpreter (Chaquopy
compiles the bytecode with it). Point `chaquopy.buildPython` in
`app/build.gradle.kts` at your interpreter if it is not at the default path.

```bash
cd android
./gradlew :app:assembleRelease     # or: gradle :app:assembleRelease
adb install -r app/build/outputs/apk/release/app-release.apk
```

Create `local.properties` with `sdk.dir=/path/to/android-sdk` (Android Studio
does this automatically).

### Release signing

`loomy-release.jks` is **not** in git (it would let anyone sign updates).
Place your own keystore at `android/loomy-release.jks`, or pass the passwords
explicitly:

```bash
gradle :app:assembleRelease \
  -PloomyStorePassword=… -PloomyKeyPassword=… -PloomyKeyAlias=…
```

Without a keystore the release build is signed with the debug key — fine for
local testing, not for distribution.

## What the app does

- Runs `GatewayService` (foreground service) which boots Chaquopy and starts the
  gateway on **127.0.0.1:17991** (deliberately different from the desktop
  gateway's 17990, so both can run side by side).
- Native Compose UI with five tabs — 首页 / 账号 / 模型 / 记录 / 设置 — talking
  to the in-process gateway over loopback HTTP.
- Account actions, SMS login, import/export, model catalogue, call records and
  a live view of `gateway.log`.
- The old WebView panel is still available as `PanelActivity` (not the launcher
  entry point).

## Notes

- The gateway binds to loopback by default. To reach it from another device on
  the LAN, set `"host": "0.0.0.0"` in the app's `config.json`
  (`/data/data/com.loomy.gw/files/config.json`).
- Account data and config live in the app's private directory and are removed
  on uninstall.
