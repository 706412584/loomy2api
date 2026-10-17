"""Android entry point: run the loomy2api gateway inside the app process.

Chaquopy mounts app files at /data/data/com.loomy.gw/files; we point the
gateway's cwd there so config.json / accounts.json / logs land in app storage.

The loomy2api package ships in APK assets under "loomy2api/" and is unpacked
to <files>/loomy2api on first run.
"""

import os
import shutil
import sys
import threading


def _copy_asset_dir(mgr, asset_path: str, dest: str) -> None:
    """Recursively copy an assets/ directory tree to app files storage."""
    os.makedirs(dest, exist_ok=True)
    for name in mgr.list(asset_path):
        src = f"{asset_path}/{name}"
        dst = os.path.join(dest, name)
        try:
            children = mgr.list(src)
        except Exception:
            children = None
        if children:
            _copy_asset_dir(mgr, src, dst)
        else:
            # chaquopy maps bytearray <-> byte[]: read(bytearray) dispatches to
            # InputStream.read(byte[]) and returns the count.
            buf = bytearray(65536)
            fin = mgr.open(src)
            try:
                with open(dst, "wb") as fout:
                    while True:
                        n = fin.read(buf)
                        if n <= 0:
                            break
                        fout.write(bytes(buf[:n]))
            finally:
                fin.close()


#: Set once the HTTP server thread has been started.  Chaquopy keeps the
#: interpreter alive across service restarts in the same process, so a second
#: start_gateway() would otherwise try to bind the port twice and crash.
#: ``server`` is the bound HTTPServer so a host change can close it cleanly.
_started = {"port": 0, "thread": None, "server": None, "host": ""}
_start_lock = threading.Lock()
#: Serialises whole start/rebind operations: the watchdog, the service start
#: and the UI's LAN toggle can otherwise race and bind the port twice.
_op_lock = threading.Lock()


def _is_serving(port: int, timeout: float = 2.0) -> bool:
    """Cheap liveness probe against our own loopback server."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def restart_gateway(port: int = 17991) -> None:
    """Called by the Android watchdog when /health stops answering.

    The Python objects from the previous attempt are still in memory and cannot
    be cleanly torn down (the HTTP server thread may be wedged), so we clear the
    bookkeeping and start a fresh server thread.  The old socket is left to the
    kernel; if it is still bound the new bind fails, which the caller sees as a
    continued unhealthy state and retries.
    """
    global _started
    with _op_lock:
        with _start_lock:
            old = _started
            _started = {"port": 0, "thread": None, "server": None, "host": ""}
        # Close the old listener when it is still healthy — otherwise rebinding
        # on a different interface (127.0.0.1 <-> 0.0.0.0) fails with
        # EADDRINUSE.
        srv = old.get("server")
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception as exc:
                print(f"[mobile_main] old server close failed: {exc}", flush=True)
        print("[mobile_main] restart requested", flush=True)
        start_gateway(os.getcwd(), port)


def bind_host(host: str, port: int = 17991) -> None:
    """Rebind the gateway on another interface (LAN toggle) and restart it.

    Persists ``host`` to config.json first so the change survives a process
    restart, then restarts the server on the new address.
    """
    import json
    cfg_path = os.path.join(os.getcwd(), "config.json")
    try:
        stored = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                stored = json.load(f)
        stored["host"] = "0.0.0.0" if str(host).strip() == "0.0.0.0" else "127.0.0.1"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(stored, f)
    except Exception as exc:
        print(f"[mobile_main] config write failed: {exc}", flush=True)
    restart_gateway(port)


def start_gateway(files_dir: str, port: int = 17991) -> None:
    """Start the gateway in a daemon thread; returns immediately.

    Idempotent: if this process is already serving on ``port`` the call is a
    no-op, so a service restart (START_STICKY) does not try to bind twice.
    """
    with _start_lock:
        if _started["port"] == port and _is_serving(port):
            print("[mobile_main] gateway already serving; skip start", flush=True)
            return
    os.chdir(files_dir)

    # 1. unpack the Python package from assets when the APK's copy is newer.
    #    The marker stores the asset build tag so APK upgrades re-extract
    #    changed .py files / index.html.
    pkg_dir = os.path.join(files_dir, "loomy2api")
    marker = os.path.join(pkg_dir, "unpacked.version")
    from java import jclass
    bridge = jclass("com.loomy.gw.AssetBridge")
    mgr = bridge.assets()
    try:
        f = mgr.open("loomy2api/unpacked.version")   # Java stream: no __enter__
        chunks = []
        while True:
            b = f.read()                             # no-arg read = ONE byte int
            if b < 0:
                break
            chunks.append(b)
        f.close()
        apk_version = bytes(chunks).decode().strip()
    except Exception as exc:
        apk_version = "1"
        print(f"[mobile_main] asset stamp read failed: {exc}", flush=True)
    disk_version = ""
    if os.path.exists(marker):
        with open(marker) as f:
            disk_version = f.read().strip()
    print(f"[mobile_main] stamp apk={apk_version} disk={disk_version!r}",
          flush=True)
    if disk_version != apk_version:
        _copy_asset_dir(mgr, "loomy2api", pkg_dir)
        with open(marker, "w") as f:
            f.write(apk_version or "1")
        print("[mobile_main] re-unpacked package from assets", flush=True)

    # 2. seed config.json (and keep the stored port in sync with the app's
    #    constant, so an upgraded APK with a different port does not leave a
    #    misleading stale value on disk)
    cfg_path = os.path.join(files_dir, "config.json")
    import json
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                stored = json.load(f)
        except Exception:
            stored = {}
        if stored.get("port") != port:
            stored["port"] = port
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(stored, f)
    else:
        stored = {}
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"host": "127.0.0.1", "port": port, "proxy": "",
                       "sessions_from_client": False}, f)

    # Bind 0.0.0.0 when the user enabled LAN access in the app's settings.
    # This used to be forced to loopback here, which silently ignored the
    # setting: the phone displayed a LAN URL that nothing was listening on.
    stored_host = str(stored.get("host") or "127.0.0.1").strip()
    listen_on = stored_host if stored_host in ("127.0.0.1", "0.0.0.0") else "127.0.0.1"

    # 3. import & run
    if files_dir not in sys.path:
        sys.path.insert(0, files_dir)

    import loomy2api.server as serve_mod
    from loomy2api.config import load_config

    cfg = load_config(cfg_path)
    cfg["port"] = port
    cfg["host"] = listen_on
    # The lite build ships without the SMS-registration modules, so the panel
    # endpoint and button must disappear with them.  Detect the module rather
    # than trusting a build flag: an upgraded full APK over a lite install (or
    # vice versa) then self-corrects.
    cfg["sms_registration"] = os.path.exists(
        os.path.join(pkg_dir, "fysms.py")) and os.path.exists(
        os.path.join(pkg_dir, "smsreg.py"))
    # let the panel persist settings back to the same file it read
    cfg["_config_file"] = cfg_path

    bound = {}
    bound_event = threading.Event()
    boot_error = {}

    def on_server(httpd):
        bound["server"] = httpd
        bound_event.set()

    def run():
        try:
            serve_mod.serve(cfg, on_server=on_server)
        except Exception as exc:                     # noqa: BLE001
            boot_error["error"] = str(exc)
            print(f"[mobile_main] gateway serve failed: {exc}", flush=True)
            bound_event.set()                        # do not block the caller

    t = threading.Thread(target=run, daemon=True, name="gateway-http")
    t.start()
    # Wait for the bind so a caller that immediately toggles the host sees a
    # real server to close (and a bind failure is reported, not hidden).
    bound_event.wait(timeout=15)
    if boot_error:
        raise RuntimeError(f"gateway bind failed: {boot_error['error']}")
    with _start_lock:
        _started["port"] = port
        _started["thread"] = t
        _started["host"] = listen_on
        _started["server"] = bound.get("server")
