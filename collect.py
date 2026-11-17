"""
Collects water readings for the NH Water Conditions map.
GitHub runs this twice a day and saves the results to data.json.
Uses only Python's built-in tools, so nothing needs installing.
"""
import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

USGS_KEY = os.environ.get("USGS_API_KEY", "").strip()
USGS = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
CWMS = "https://cwms-data.usace.army.mil/cwms-data"
CORPS_OFFICE = "NAE"  # Army Corps New England District
BBOX = [-72.60, 42.68, -70.58, 45.32]  # New Hampshire
CORPS_STATES = {"NH"}

errors = []
now = datetime.now(timezone.utc)


def get(url, headers=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nh-water-map", **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(5)
                continue
            raise


def usgs(path, params):
    if USGS_KEY:
        params["api_key"] = USGS_KEY
    params["f"] = "json"
    return get(f"{USGS}/{path}?" + urllib.parse.urlencode(params))


def parse_time(t):
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000, timezone.utc)
    return datetime.fromisoformat(str(t).replace("Z", "+00:00"))


def hourly(points):
    """Keep one reading per hour so the data file stays small."""
    out, last_hour = [], None
    for t, v in sorted(points):
        h = t.replace(minute=0, second=0, microsecond=0)
        if h != last_hour:
            out.append([int(t.timestamp()), round(v, 3)])
            last_hour = h
    return out


def change_24h(points):
    if len(points) < 2:
        return None
    points = sorted(points)
    t_last, v_last = points[-1]
    target = t_last - timedelta(hours=24)
    best = min(points, key=lambda p: abs((p[0] - target).total_seconds()))
    if abs((best[0] - target).total_seconds()) > 3 * 3600:
        return None
    return round(v_last - best[1], 3)


# ---------------- USGS ----------------
def collect_usgs():
    sites = {}
    for key, code in (("height", "00065"), ("temp", "00010"), ("flow", "00060")):
        try:
            data = usgs("latest-continuous/items", {"bbox": ",".join(map(str, BBOX)),
                                                      "parameter_code": code, "limit": 5000})
        except Exception as e:
            errors.append(f"USGS latest {key}: {e}")
            continue
        for f in data.get("features", []):
            p, g = f.get("properties", {}), f.get("geometry") or {}
            try:
                val = float(p.get("value"))
                lon, lat = g["coordinates"][:2]
            except Exception:
                continue
            sid = p.get("monitoring_location_id")
            s = sites.setdefault(sid, {"id": sid, "source": "USGS", "lat": lat, "lon": lon,
                                       "levelLabel": "Water level", "flowLabel": "Flow"})
            t = p.get("time")
            if s.get(key + "Time") and s[key + "Time"] > t:
                continue
            if key == "temp" and "c" in (p.get("unit_of_measure") or "degC").lower():
                val = val * 9 / 5 + 32
            s[key] = round(val, 2)
            s[key + "Time"] = t

    sites = {k: s for k, s in sites.items() if "height" in s or "temp" in s}
    for s in sites.values():
        time.sleep(0.3)
        try:
            loc = usgs(f"monitoring-locations/items/{urllib.parse.quote(s['id'])}", {})
            s["name"] = (loc.get("properties") or {}).get("monitoring_location_name") or s["id"]
        except Exception as e:
            s["name"] = s["id"]
        if "height" not in s:
            continue
        try:
            data = usgs("continuous/items", {"monitoring_location_id": s["id"], "parameter_code": "00065",
                                             "datetime": "P7D", "limit": 2000})
            pts = []
            for f in data.get("features", []):
                pr = f.get("properties", {})
                try:
                    pts.append((parse_time(pr["time"]), float(pr["value"])))
                except Exception:
                    pass
            s["change"] = change_24h(pts)
            s["series"] = hourly(pts)
        except Exception as e:
            errors.append(f"USGS history {s['id']}: {e}")
    return list(sites.values())


# ---------------- Army Corps ----------------
CORPS_JSON = {"Accept": "application/json;version=2"}


def corps_catalog(pattern):
    names, page = [], None
    for _ in range(20):
        params = {"office": CORPS_OFFICE, "like": pattern, "page-size": 500}
        if page:
            params["page"] = page
        data = get(f"{CWMS}/catalog/TIMESERIES?" + urllib.parse.urlencode(params), CORPS_JSON)
        for e in data.get("entries", []):
            n = e.get("name") or e.get("timeseries-id") or e.get("id")
            if n:
                names.append(n)
        page = data.get("next-page")
        if not page:
            break
    return names


def pick(names, params):
    """Best time series for a location: matching parameter, instantaneous, shortest interval."""
    order = ["15Minutes", "15Minute", "30Minutes", "1Hour", "6Hours", "1Day"]
    cands = [n for n in names if n.split(".")[1] in params]
    if not cands:
        return None
    def score(n):
        parts = n.split(".")
        par = params.index(parts[1])
        inst = 0 if parts[2] == "Inst" else 1
        iv = order.index(parts[3]) if parts[3] in order else 99
        return (par, inst, iv)
    return sorted(cands, key=score)[0]


def corps_series(name, days=7):
    params = {"name": name, "office": CORPS_OFFICE, "unit": "EN",
              "begin": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "page-size": 5000}
    data = get(f"{CWMS}/timeseries?" + urllib.parse.urlencode(params), CORPS_JSON)
    pts = []
    for row in data.get("values") or []:
        try:
            if row[1] is None:
                continue
            pts.append((parse_time(row[0]), float(row[1])))
        except Exception:
            pass
    return pts


def collect_corps():
    try:
        names = corps_catalog(r".*\.(Stage|Elev|Flow|Flow-Out|Flow-In|Temp-Water)\..*")
    except Exception as e:
        errors.append(f"Army Corps catalog: {e}")
        return []
    by_loc = {}
    for n in names:
        parts = n.split(".")
        if len(parts) == 6:
            by_loc.setdefault(parts[0], []).append(n)

    out = []
    for loc, tsnames in by_loc.items():
        time.sleep(0.3)
        try:
            info = get(f"{CWMS}/locations/{urllib.parse.quote(loc)}?" +
                       urllib.parse.urlencode({"office": CORPS_OFFICE}), CORPS_JSON)
        except Exception:
            continue
        state = (info.get("state-initial") or "").upper()
        lat, lon = info.get("latitude"), info.get("longitude")
        if state not in CORPS_STATES or lat is None or lon is None:
            continue
        s = {"id": f"USACE-{loc}", "source": "Army Corps", "lat": lat, "lon": lon,
             "name": info.get("public-name") or info.get("long-name") or loc,
             "corpsId": loc.lower()}

        level = pick(tsnames, ["Stage", "Elev"])
        if level:
            try:
                pts = corps_series(level)
                if pts:
                    pts.sort()
                    s["height"] = round(pts[-1][1], 2)
                    s["heightTime"] = pts[-1][0].isoformat()
                    s["change"] = change_24h(pts)
                    s["series"] = hourly(pts)
                    s["levelLabel"] = "Pool elevation" if level.split(".")[1] == "Elev" else "Water level"
            except Exception as e:
                errors.append(f"Army Corps {loc} level: {e}")

        flow = pick(tsnames, ["Flow-Out", "Flow", "Flow-In"])
        if flow:
            try:
                pts = corps_series(flow, days=2)
                if pts:
                    pts.sort()
                    s["flow"] = round(pts[-1][1])
                    s["flowTime"] = pts[-1][0].isoformat()
                    s["flowLabel"] = {"Flow-Out": "Dam release", "Flow-In": "Inflow"}.get(flow.split(".")[1], "Flow")
            except Exception as e:
                errors.append(f"Army Corps {loc} flow: {e}")

        temp = pick(tsnames, ["Temp-Water"])
        if temp:
            try:
                pts = corps_series(temp, days=2)
                if pts:
                    pts.sort()
                    s["temp"] = round(pts[-1][1], 1)
                    s["tempTime"] = pts[-1][0].isoformat()
            except Exception:
                pass

        if "height" in s or "flow" in s or "temp" in s:
            out.append(s)
    return out


if __name__ == "__main__":
    sites = collect_usgs()
    print(f"USGS: {len(sites)} gauges")
    corps = collect_corps()
    print(f"Army Corps: {len(corps)} stations")
    result = {"updated": now.isoformat(), "errors": errors[:50], "sites": sites + corps}
    with open("data.json", "w") as fh:
        json.dump(result, fh, separators=(",", ":"))
    for e in errors[:20]:
        print("Warning:", e)
