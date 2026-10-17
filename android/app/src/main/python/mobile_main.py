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


def start_gateway(files_dir: str, port: int = 17991) -> None:
    """Start the gateway in a daemon thread; returns immediately."""
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
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"host": "127.0.0.1", "port": port, "proxy": "",
                       "sessions_from_client": False}, f)

    # 3. import & run
    if files_dir not in sys.path:
        sys.path.insert(0, files_dir)

    import loomy2api.server as serve_mod
    from loomy2api.config import load_config

    cfg = load_config(cfg_path)
    cfg["port"] = port
    cfg["host"] = "127.0.0.1"
    # let the panel persist settings back to the same file it read
    cfg["_config_file"] = cfg_path

    t = threading.Thread(target=serve_mod.serve, args=(cfg,), daemon=True)
    t.start()
