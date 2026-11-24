#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 Monitor - local live display for a smart electricity meter read by a
GreenHESS "ADA P1 Meter", with optional Fronius inverters (Solar API v1).

* polls the P1 reader's local JSON endpoint every few seconds
* drops the cloud username / password / meter identifiers IMMEDIATELY
  (they are never stored, logged or served)
* keeps the recent history in RAM only (no SD card wear); writes one small
  snapshot file per day for the daily totals
* serves p1monitor.html (all skins) and a small JSON API

Only the Python standard library is used - nothing to install.
All addresses come from config.json (see config.example.json and README).
"""

import argparse
import collections
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "p1monitor.html")

DEFAULTS = {
    "p1_url": "",                 # e.g. http://<P1-READER-IP>:<PORT>/json
    "inverters": [],              # [{"name": "Fronius 1", "host": "<INVERTER-IP>"}]
    "listen": {"host": "0.0.0.0", "port": 8080},
    "poll_s": 5,                  # polling interval (s)
    "history_h": 6,               # hours of history kept in RAM
    "stale_s": 20,                # after this many seconds: "no fresh data"
    "inv_stale_s": 60,            # inverter considered offline (normal at night)
    "state_file": "p1state.json", # daily snapshot (relative to this folder)
    "ui": {},                     # passed to the web page (scales, titles)
}

# these fields are NEVER passed on (credentials, identifiers)
SECRET_KEYS = ("username", "password", "meter_serial_number", "client_id",
               "cosem_logical_device_name", "local_ip")

# placeholders from config.example.json - refuse to run with them
PLACEHOLDERS = ("<", ">", "192.0.2.", "PORT")


def load_config(path):
    if not os.path.isfile(path):
        sys.exit("config file not found: %s\n"
                 "  -> copy config.example.json to config.json and edit it "
                 "(see README)." % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except ValueError as e:
        sys.exit("config.json is not valid JSON: %s" % e)
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in user.items():
        if k.startswith("_"):
            continue  # "_comment" style keys
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    url = str(cfg["p1_url"])
    if not url or any(p in url for p in PLACEHOLDERS):
        sys.exit("p1_url is not set in %s - enter the address of your P1 reader, "
                 "e.g. http://<ip-address>:<port>/json" % path)
    invs = []
    for i, inv in enumerate(cfg["inverters"] or []):
        host = str(inv.get("host", "")).strip()
        if not host or any(p in host for p in PLACEHOLDERS):
            print("note: inverter #%d has no address - skipped" % (i + 1), flush=True)
            continue
        invs.append((str(inv.get("name") or "Inverter %d" % (i + 1)), host))
    cfg["inverters"] = invs
    sf = cfg["state_file"]
    cfg["state_file"] = sf if os.path.isabs(sf) else os.path.join(HERE, sf)
    return cfg


CFG = None          # filled in main()
_lock = threading.Lock()
_state = {"data": None, "t_ok": 0.0, "err": "", "fails": 0}
_hist = None
_inv = []
_day = {"date": "", "e_imp": 0.0, "e_exp": 0.0, "partial": True}


def _day_load():
    try:
        with open(CFG["state_file"], "r", encoding="utf-8") as fh:
            j = json.load(fh)
        _day.update(date=str(j["date"]), e_imp=float(j["e_imp"]), e_exp=float(j["e_exp"]),
                    partial=bool(j.get("partial", False)))
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _day_update(e_imp, e_exp):
    """On a new day, store the current meter reading (one write per day)."""
    today = time.strftime("%Y-%m-%d")
    if _day["date"] == today or e_imp <= 0:
        return
    lt = time.localtime()
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    # exact if we are just after midnight or the program ran through midnight
    partial = not ((lt.tm_hour == 0 and lt.tm_min < 10) or _day["date"] == yesterday)
    _day.update(date=today, e_imp=e_imp, e_exp=e_exp, partial=partial)
    try:
        tmp = CFG["state_file"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_day, fh)
        os.replace(tmp, CFG["state_file"])
    except OSError as e:
        print("state file write error:", e, flush=True)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts(s):
    """'260101120000W' -> '2026-01-01 12:00:00'"""
    s = str(s or "")
    if len(s) >= 12 and s[:12].isdigit():
        return "20%s-%s-%s %s:%s:%s" % (s[0:2], s[2:4], s[4:6], s[6:8], s[8:10], s[10:12])
    return ""


def _plugin(plugins, prefix):
    for k, v in (plugins or {}).items():
        if k.startswith(prefix):
            return v.get("value") if isinstance(v, dict) else v
    return None


def build(raw):
    """Clean, secret-free data packet from the raw /json answer."""
    for k in SECRET_KEYS:
        raw.pop(k, None)

    pl = raw.get("plugins") or {}
    imp = _f(raw.get("instantaneous_power_import"))
    exp = _f(raw.get("instantaneous_power_export"))

    pv_raw = _plugin(pl, "INV_activepower_")
    home_raw = _plugin(pl, "INV_homeconsumption_")
    has_inv = pv_raw is not None
    pv = _f(pv_raw)
    home = _f(home_raw) if home_raw is not None else max(0.0, pv + imp - exp)

    def ph(key_a, key_b, i):
        v = raw.get("%s%d" % (key_a, i))
        if v is None and key_b:
            v = raw.get("%s%d" % (key_b, i))
        return _f(v)

    q = raw.get("quarter_hour") or {}
    qi = q.get("import") or {}
    qe = q.get("export") or {}

    # inverters polled directly (if configured) take precedence
    with _lock:
        inv = inv_snapshot()
    if any(i["total"] > 0 for i in inv):
        has_inv = True
        pv = round(sum(i["p"] for i in inv), 3)
        home = round(max(0.0, pv + imp - exp), 3)
        day_yield = round(sum(i["day"] for i in inv), 3)
        total_yield = round(sum(i["total"] for i in inv), 1)
        inv_model = "%d× Fronius" % len(inv)
    else:
        day_yield = _f(_plugin(pl, "INV_dailyyield_"))
        total_yield = _f(_plugin(pl, "INV_totalyield_"))
        inv_model = str(_plugin(pl, "INV_model_") or "")

    e_imp_now = _f(raw.get("active_import_energy_total"))
    e_exp_now = _f(raw.get("active_export_energy_total"))
    _day_update(e_imp_now, e_exp_now)
    day_imp = round(max(0.0, e_imp_now - _day["e_imp"]), 3)
    day_exp = round(max(0.0, e_exp_now - _day["e_exp"]), 3)

    return {
        "day_imp": day_imp,
        "day_exp": day_exp,
        "day_cons": round(max(0.0, day_yield + day_imp - day_exp), 3),
        "day_partial": bool(_day["partial"]),
        "inv": inv,
        "ts": _ts(raw.get("timestamp")),
        "tariff": str(raw.get("current_tariff", "")),
        "breaker": str(raw.get("circuit_breaker_status", "")),
        "limiter": _f(raw.get("limiter_threshold")),
        "imp": imp, "exp": exp, "net": round(imp - exp, 3),
        "pv": pv, "home": home, "has_inv": has_inv,
        "day_yield": day_yield,
        "total_yield": total_yield,
        "soc": _f(_plugin(pl, "INV_batterysoc_")),
        "inv_model": inv_model,
        # phases: for current the more precise 'Bl' value if present
        "v": [ph("voltage_phase_l", None, i) for i in (1, 2, 3)],
        "a": [ph("current_phase_Bl", "current_phase_l", i) for i in (1, 2, 3)],
        "pi": [ph("instantaneous_power_import_l", None, i) for i in (1, 2, 3)],
        "pe": [ph("instantaneous_power_export_l", None, i) for i in (1, 2, 3)],
        "pf": [ph("power_factor_l", None, i) for i in (1, 2, 3)],
        "pf_total": _f(raw.get("power_factor")),
        "freq": _f(raw.get("frequency")),
        "e_imp": _f(raw.get("active_import_energy_total")),
        "e_imp_t1": _f(raw.get("active_import_energy_tariff_1")),
        "e_imp_t2": _f(raw.get("active_import_energy_tariff_2")),
        "e_exp": _f(raw.get("active_export_energy_total")),
        "e_exp_t1": _f(raw.get("active_export_energy_tariff_1")),
        "e_exp_t2": _f(raw.get("active_export_energy_tariff_2")),
        "qh": {
            "from": str(q.get("from", ""))[11:16],
            "to": str(q.get("to", ""))[11:16],
            "elapsed": int(_f(q.get("elapsed_seconds"))),
            "imp_kwh": _f(qi.get("energy_kwh")),
            "imp_avg": _f(qi.get("average_power_kw")),
            "imp_proj": _f(qi.get("projected_power_kw")),
            "exp_kwh": _f(qe.get("energy_kwh")),
            "exp_avg": _f(qe.get("average_power_kw")),
            "exp_proj": _f(qe.get("projected_power_kw")),
            "valid": bool(q.get("valid", False)),
        },
    }


def _inv_poll_one(inv):
    url = "http://%s/solar_api/v1/GetPowerFlowRealtimeData.fcgi" % inv["host"]
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        invs = ((j.get("Body") or {}).get("Data") or {}).get("Inverters") or {}
        p = day = total = year = 0.0
        for v in invs.values():
            p += _f(v.get("P"))
            day += _f(v.get("E_Day"))
            total += _f(v.get("E_Total"))
            year += _f(v.get("E_Year"))
        with _lock:
            inv.update(p=p / 1000.0, day=day / 1000.0, total=total / 1000.0, year=year / 1000.0,
                       t_ok=time.time(), date=time.strftime("%Y-%m-%d"), err="")
    except Exception as e:  # noqa: BLE001  (inverters sleep at night - normal)
        with _lock:
            inv["err"] = type(e).__name__


def inv_poller():
    while True:
        t0 = time.time()
        th = [threading.Thread(target=_inv_poll_one, args=(i,), daemon=True) for i in _inv]
        for t in th:
            t.start()
        for t in th:
            t.join(4)
        time.sleep(max(0.5, CFG["poll_s"] - (time.time() - t0)))


def inv_snapshot():
    """Per-inverter data with stale handling. Call with _lock held."""
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    out = []
    for i in _inv:
        fresh = i["t_ok"] and now - i["t_ok"] < CFG["inv_stale_s"]
        out.append({
            "name": i["name"],
            "ok": bool(fresh),
            "p": round(i["p"], 3) if fresh else 0.0,
            "day": round(i["day"], 3) if i["date"] == today else 0.0,
            "total": round(i["total"], 1),
            "year": round(i["year"], 1),
        })
    return out


def poller():
    last_err = None
    while True:
        t0 = time.time()
        try:
            with urllib.request.urlopen(CFG["p1_url"], timeout=4) as r:
                raw = json.loads(r.read().decode("utf-8", "replace"))
            raw.pop("password", None)      # immediately, before anything else
            raw.pop("username", None)
            d = build(raw)
            del raw
            with _lock:
                _state["data"] = d
                _state["t_ok"] = time.time()
                _state["err"] = ""
                _state["fails"] = 0
                _hist.append((int(t0), d["imp"], d["exp"], d["pv"], d["home"]))
            if last_err is not None:
                print("P1 reader reachable again", flush=True)
                last_err = None
        except Exception as e:  # noqa: BLE001
            msg = type(e).__name__ + ": " + str(e)[:120]
            with _lock:
                _state["err"] = msg
                _state["fails"] += 1
            if msg != last_err:            # log only on change
                print("P1 error:", msg, flush=True)
                last_err = msg
        time.sleep(max(0.5, CFG["poll_s"] - (time.time() - t0)))


def _page():
    """p1monitor.html with the "ui" part of config.json injected."""
    with open(HTML_FILE, "rb") as fh:
        body = fh.read()
    ui = json.dumps(CFG.get("ui") or {}, ensure_ascii=False).replace("</", "<\\/")
    return body.replace(b"/*P1CFG*/", ("Object.assign(CFG, %s);" % ui).encode("utf-8"), 1)


class Handler(BaseHTTPRequestHandler):
    server_version = "P1Monitor/1.0"

    def log_message(self, *args):  # no access log (no needless writes)
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                self._send(200, _page(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"p1monitor.html missing next to p1monitor.py",
                           "text/plain; charset=utf-8")
        elif u.path == "/api/now":
            with _lock:
                age = time.time() - _state["t_ok"] if _state["t_ok"] else None
                out = {
                    "ok": age is not None and age < CFG["stale_s"],
                    "age": None if age is None else round(age, 1),
                    "err": _state["err"],
                    "fails": _state["fails"],
                    "data": _state["data"],
                }
            self._json(out)
        elif u.path == "/api/history":
            q = parse_qs(u.query)
            try:
                n = max(50, min(2000, int(q.get("n", ["720"])[0])))
            except ValueError:
                n = 720
            with _lock:
                rows = list(_hist)
            step = max(1, len(rows) // n)
            rows = rows[::step]
            self._json({
                "t": [r[0] for r in rows],
                "imp": [round(r[1], 3) for r in rows],
                "exp": [round(r[2], 3) for r in rows],
                "pv": [round(r[3], 3) for r in rows],
                "home": [round(r[4], 3) for r in rows],
            })
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def main():
    global CFG, _hist, _inv
    ap = argparse.ArgumentParser(description="P1 Monitor web server")
    ap.add_argument("-c", "--config", default=os.path.join(HERE, "config.json"),
                    help="path of config.json (default: next to this script)")
    args = ap.parse_args()
    CFG = load_config(args.config)
    _hist = collections.deque(maxlen=int(CFG["history_h"] * 3600 // CFG["poll_s"]))
    _inv = [{"name": n, "host": h, "p": 0.0, "day": 0.0, "total": 0.0, "year": 0.0,
             "t_ok": 0.0, "date": "", "err": ""} for n, h in CFG["inverters"]]
    try:
        os.nice(10)  # low priority - other services on the same Pi come first
    except (AttributeError, OSError):
        pass
    _day_load()
    if _inv:
        threading.Thread(target=inv_poller, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    host, port = CFG["listen"]["host"], int(CFG["listen"]["port"])
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print("P1 Monitor running on port %d  (%d inverter(s) configured)" % (port, len(_inv)),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
