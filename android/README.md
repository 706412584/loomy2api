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

## Public tunnel (Cloudflare)

Settings → 公网穿透 exposes the gateway through Cloudflare Tunnel, so the API
is reachable from anywhere without a public IP or router port-forwarding.

- **临时公网地址 (quick)**: starts `cloudflared tunnel --url
  http://127.0.0.1:<port>` and shows the generated
  `https://*.trycloudflare.com` URL. The URL changes on every start.
- **指定域名 (named)**: runs `cloudflared tunnel run --token …`. Create the
  tunnel in Cloudflare Zero Trust, add a Public Hostname pointing at
  `http://127.0.0.1:<port>`, then paste the token and the hostname here.
- Enabling the tunnel with no API key configured generates one automatically;
  without it the public URL would expose both the model API and the panel.
- The choice is persisted. `GatewayService` reconnects the tunnel after a
  process restart or reboot.

### The cloudflared binary

The APK ships a cross-compiled `cloudflared` as
`app/src/main/jniLibs/arm64-v8a/libcloudflared.so` (arm64-v8a only, ~29 MB;
the APK grows by ~10 MB compressed). It lives in `jniLibs` because Android 10+
refuses to execute files in app-private storage — the installer extracts it to
`nativeLibraryDir`, which is executable. `useLegacyPackaging = true` in
`build.gradle.kts` is required for that extraction.

The stock Linux release does **not** work: cloudflared's edge discovery uses
SRV lookups, and Go's SRV resolver always reads `/etc/resolv.conf`, which
Android does not provide. The Android build uses cgo for ordinary lookups, and
`TunnelManager` resolves the edge hosts through Android's resolver and passes
them as `--edge <ip>:7844`.

Rebuild it with:

```bash
NDK=/path/to/android-ndk ./tools/build-cloudflared.sh
```

cloudflared is Apache-2.0 licensed; see `tools/cloudflared-LICENSE.txt`.

## Notes

- The gateway binds to loopback by default. To reach it from another device on
  the LAN, set `"host": "0.0.0.0"` in the app's `config.json`
  (`/data/data/com.loomy.gw/files/config.json`).
- Account data and config live in the app's private directory and are removed
  on uninstall.
