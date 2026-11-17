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


def pct_from_table(value, table):
    """Estimate where a value falls (0-100) given [(percentile, value)] pairs."""
    pts = sorted({(p, v) for p, v in table if v is not None}, key=lambda x: x[1])
    if len(pts) < 3:
        return None
    if value <= pts[0][1]:
        return max(0.0, pts[0][0] - 1)
    if value >= pts[-1][1]:
        return min(100.0, pts[-1][0] + 1)
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        if v1 <= value <= v2:
            return p1 if v2 == v1 else p1 + (p2 - p1) * (value - v1) / (v2 - v1)
    return None


def pct_from_history(value, history):
    if len(history) < 50:
        return None
    below = sum(1 for h in history if h < value)
    return 100.0 * below / len(history)


def category(pct):
    if pct is None:
        return None
    if pct < 10: return "much-below"
    if pct < 25: return "below"
    if pct <= 75: return "normal"
    if pct <= 90: return "above"
    return "much-above"


def walk_stats(node, ctx, out):
    """Pull day-of-year statistics out of the USGS reply, whatever its exact layout."""
    if isinstance(node, str):
        t = node.strip()
        if t[:1] in "[{":
            try:
                walk_stats(json.loads(t), ctx, out)
            except ValueError:
                pass
        return
    if isinstance(node, list):
        for n in node:
            walk_stats(n, ctx, out)
        return
    if not isinstance(node, dict):
        return
    ctx = dict(ctx)
    for k in ("monitoring_location_id", "parameter_code", "time_of_year_type"):
        if node.get(k):
            ctx[k] = node[k]
    if "percentiles" in node and isinstance(node.get("values"), list) and isinstance(node["percentiles"], list):
        for pc, v in zip(node["percentiles"], node["values"]):
            out.append((ctx, pc, v))
    if "value" in node and ("computation" in node or "percentile" in node or "computation_type" in node):
        comp = str(node.get("computation") or node.get("computation_type") or "").lower()
        pc = node.get("percentile")
        if pc is None:
            pc = {"median": 50, "minimum": 0, "maximum": 100}.get(comp)
        if pc is not None and comp not in ("arithmetic_mean", "mean"):
            out.append((ctx, pc, node["value"]))
    for v in node.values():
        if isinstance(v, (dict, list, str)):
            walk_stats(v, ctx, out)


def usgs_normals(site_ids):
    """Day-of-year percentiles for today's date, for flow and water level."""
    today = now.astimezone(timezone(timedelta(hours=-5))).strftime("%m-%d")
    tables = {}
    for i in range(0, len(site_ids), 20):
        batch = site_ids[i:i + 20]
        params = [("monitoring_location_id", sid) for sid in batch]
        params += [("parameter_code", "00060"), ("parameter_code", "00065"),
                   ("start_date", today), ("end_date", today), ("normal_type", "DOY")]
        try:
            data = get("https://api.waterdata.usgs.gov/statistics/v0/observationNormals?" +
                       urllib.parse.urlencode(params), {"X-Api-Key": USGS_KEY} if USGS_KEY else None)
        except Exception as e:
            errors.append(f"USGS normals: {e}")
            continue
        rows = []
        walk_stats(data, {}, rows)
        if not rows and i == 0:
            print("USGS normals sample reply:", json.dumps(data)[:600])
        for ctx, pc, v in rows:
            if ctx.get("time_of_year_type", "day_of_year") != "day_of_year":
                continue
            try:
                tables.setdefault((ctx.get("monitoring_location_id"), ctx.get("parameter_code")), []).append(
                    (float(pc), float(v)))
            except (TypeError, ValueError):
                pass
        time.sleep(0.5)
    return tables


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

    # Reuse gauge names from the last run so we don't look them up every time
    old_names = {}
    try:
        with open("data.json") as fh:
            for o in json.load(fh).get("sites", []):
                if o.get("name") and o.get("name") != o.get("id"):
                    old_names[o["id"]] = o["name"]
    except Exception:
        pass

    start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with_history = 0
    for s in sites.values():
        s["name"] = old_names.get(s["id"])
        if not s["name"]:
            time.sleep(0.3)
            try:
                loc = usgs(f"monitoring-locations/items/{urllib.parse.quote(s['id'])}", {})
                s["name"] = (loc.get("properties") or {}).get("monitoring_location_name") or s["id"]
            except Exception:
                s["name"] = s["id"]
        if "height" not in s:
            continue
        time.sleep(0.3)
        try:
            pts = []
            data = usgs("continuous/items", {"monitoring_location_id": s["id"], "parameter_code": "00065",
                                             "datetime": f"{start}/{end}", "limit": 10000})
            for _ in range(5):
                for f in data.get("features", []):
                    pr = f.get("properties", {})
                    try:
                        pts.append((parse_time(pr["time"]), float(pr["value"])))
                    except Exception:
                        pass
                nxt = next((l.get("href") for l in data.get("links", []) if l.get("rel") == "next"), None)
                if not nxt:
                    break
                data = get(nxt)
            if pts:
                with_history += 1
            s["change"] = change_24h(pts)
            s["series"] = hourly(pts)
        except Exception as e:
            errors.append(f"USGS history {s['id']}: {e}")
    print(f"USGS history: got 7 days of readings for {with_history} gauges")

    tables = usgs_normals(list(sites.keys()))
    for s in sites.values():
        for basis, code, key in (("flow", "00060", "flow"), ("level", "00065", "height")):
            if s.get(key) is None or (s["id"], code) not in tables:
                continue
            pct = pct_from_table(s[key], tables[(s["id"], code)])
            if pct is not None:
                s["normal"] = {"pct": round(pct), "cat": category(pct), "basis": basis, "source": "USGS long-term records"}
                break
    print(f"USGS normals: found history for {len(tables)} gauge readings, "
          f"{sum(1 for s in sites.values() if 'normal' in s)} gauges compared")
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


def corps_series(name, days=7, end=None):
    end = end or now
    params = {"name": name, "office": CORPS_OFFICE, "unit": "EN",
              "begin": (end - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "page-size": 5000}
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
                    # Normal for this time of year: readings within a week of today's date in each of the past 10 years
                    history, years = [], 0
                    for y in range(1, 11):
                        try:
                            mid = now.replace(year=now.year - y)
                        except ValueError:
                            mid = now.replace(year=now.year - y, day=28)
                        try:
                            past = corps_series(level, days=14, end=mid + timedelta(days=7))
                        except Exception:
                            past = []
                        if past:
                            years += 1
                            history += [v for _, v in past]
                        time.sleep(0.2)
                    pct = pct_from_history(s["height"], history) if years >= 3 else None
                    if pct is not None:
                        s["normal"] = {"pct": round(pct), "cat": category(pct), "basis": "level",
                                       "source": f"Army Corps readings, {years} past years"}
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


# ---------------- NHDES (state lakes and dams) ----------------
DES = "https://nhdes.rtiamanzi.org/api"


def des_values(ts_id, start, end):
    url = f"{DES}/timeseries/{ts_id}/values/?" + urllib.parse.urlencode({
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"), "end": end.strftime("%Y-%m-%dT%H:%M:%S")})
    data = get(url)
    rows = data.get("results", data) if isinstance(data, dict) else data
    pts = []
    for r in rows or []:
        try:
            if not isinstance(r, dict) or r.get("num_value") is None:
                continue
            pts.append((parse_time(r["datetime"]), float(r["num_value"])))
        except Exception:
            pass
    return sorted(pts)


def des_kind(param):
    """Sort an NHDES reading type into level, temperature, flow, or something we skip."""
    code = (param.get("code") or "").upper()
    name = (param.get("name") or "").lower()
    if code in ("PELV", "LELV", "STG") or "elevation" in name or "lake level" in name or "stage" in name:
        if "forecast" in name or "target" in name or "rule" in name:
            return None
        return "level"
    if "water" in name and "temp" in name:
        return "temp"
    if code in ("QIN", "QOUT") or "discharge" in name or "outflow" in name:
        return "flow"
    return None


def collect_nhdes():
    try:
        params = get(f"{DES}/parameters/")
        params = params.get("results", params) if isinstance(params, dict) else params
        pinfo = {p["id"]: p for p in params if isinstance(p, dict) and p.get("id")}
        stations = get(f"{DES}/locations/?layer__code=STATIONS")
        stations = stations.get("results", stations) if isinstance(stations, dict) else stations
    except Exception as e:
        errors.append(f"NHDES station list: {e}")
        return []

    out = []
    for st in stations or []:
        if not isinstance(st, dict):
            continue
        pr = st.get("properties") or {}
        code = pr.get("code")
        if not code or pr.get("enabled") is False:
            continue
        try:
            coords = st["geometry"]["coordinates"]
            lon, lat = (coords[0] if isinstance(coords[0], list) else coords)[:2]
        except Exception:
            continue
        time.sleep(0.2)
        try:
            tslist = get(f"{DES}/timeseries/sparse/?" + urllib.parse.urlencode({"location__code": code}))
            tslist = tslist.get("results", tslist) if isinstance(tslist, dict) else tslist
        except Exception as e:
            errors.append(f"NHDES {code} list: {e}")
            continue

        best = {}
        for ts in tslist or []:
            if not isinstance(ts, dict) or not ts.get("id"):
                continue
            tcode = (ts.get("code") or "").upper()
            if ".USGS." in tcode or ".NWS." in tcode or ".USACE." in tcode:
                continue  # already on the map from the original agency
            param = pinfo.get(ts.get("parameter")) or {"name": ts.get("name", ""), "code": tcode.split(".")[2] if tcode.count(".") >= 2 else ""}
            kind = des_kind(param)
            if kind and kind not in best:
                best[kind] = (ts["id"], param)
        if not best:
            continue

        s = {"id": f"NHDES-{code}", "source": "NHDES", "lat": lat, "lon": lon,
             "name": pr.get("name") or code, "desCode": code}
        if "level" in best:
            ts_id, param = best["level"]
            try:
                pts = des_values(ts_id, now - timedelta(days=7), now)
                if pts:
                    s["height"] = round(pts[-1][1], 2)
                    s["heightTime"] = pts[-1][0].isoformat()
                    s["change"] = change_24h(pts)
                    s["series"] = hourly(pts)
                    pname = (param.get("name") or "").lower()
                    s["levelLabel"] = "Lake level" if ("elev" in pname or "lake" in pname) else "Water level"
                    history, years = [], 0
                    for y in range(1, 11):
                        try:
                            mid = now.replace(year=now.year - y)
                        except ValueError:
                            mid = now.replace(year=now.year - y, day=28)
                        try:
                            past = des_values(ts_id, mid - timedelta(days=7), mid + timedelta(days=7))
                        except Exception:
                            past = []
                        if past:
                            years += 1
                            history += [v for _, v in past]
                        time.sleep(0.2)
                    pct = pct_from_history(s["height"], history) if years >= 3 else None
                    if pct is not None:
                        s["normal"] = {"pct": round(pct), "cat": category(pct), "basis": "level",
                                       "source": f"NHDES readings, {years} past years"}
            except Exception as e:
                errors.append(f"NHDES {code} level: {e}")
        if "temp" in best:
            ts_id, param = best["temp"]
            try:
                pts = des_values(ts_id, now - timedelta(days=2), now)
                if pts:
                    v = pts[-1][1]
                    unit = ((param.get("unit") or {}).get("code") or "").upper()
                    if unit in ("C", "DEGC", "DEG C", "CEL", "CELSIUS") or (not unit.startswith("F") and v < 40 and "F" not in unit):
                        v = v * 9 / 5 + 32
                    s["temp"] = round(v, 1)
                    s["tempTime"] = pts[-1][0].isoformat()
            except Exception as e:
                errors.append(f"NHDES {code} temp: {e}")
        if "flow" in best:
            ts_id, param = best["flow"]
            try:
                pts = des_values(ts_id, now - timedelta(days=2), now)
                if pts:
                    s["flow"] = round(pts[-1][1])
                    s["flowTime"] = pts[-1][0].isoformat()
                    s["flowLabel"] = "Dam outflow" if (param.get("code") or "").upper() == "QOUT" else "Flow"
            except Exception as e:
                errors.append(f"NHDES {code} flow: {e}")
        if any(k in s for k in ("height", "temp", "flow")):
            out.append(s)
    return out


if __name__ == "__main__":
    import traceback

    def safe(label, fn):
        try:
            return fn()
        except Exception as e:
            errors.append(f"{label} skipped: {e}")
            print(f"Warning: {label} failed and was skipped. Details:")
            traceback.print_exc()
            return []

    sites = safe("USGS", collect_usgs)
    print(f"USGS: {len(sites)} gauges")
    corps = safe("Army Corps", collect_corps)
    print(f"Army Corps: {len(corps)} stations")
    des = safe("NHDES", collect_nhdes)
    print(f"NHDES: {len(des)} stations "
          f"({sum(1 for x in des if 'temp' in x)} with water temp, {sum(1 for x in des if 'normal' in x)} compared to normal)")
    all_sites = sites + corps + des
    if not all_sites:
        print("Nothing collected this time; keeping the previous data.json.")
    else:
        result = {"updated": now.isoformat(), "errors": errors[:50], "sites": all_sites}
        with open("data.json", "w") as fh:
            json.dump(result, fh, separators=(",", ":"))
    for e in errors[:20]:
        print("Warning:", e)
