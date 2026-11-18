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


def median_from_table(table):
    pts = sorted({(p, v) for p, v in table if v is not None})
    for p, v in pts:
        if p == 50:
            return v
    lo = [x for x in pts if x[0] < 50]
    hi = [x for x in pts if x[0] > 50]
    if lo and hi:
        (p1, v1), (p2, v2) = lo[-1], hi[0]
        return v1 + (v2 - v1) * (50 - p1) / (p2 - p1)
    return None


def median(values):
    v = sorted(values)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


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


# ---------------- Monthly baseline (average for this month over the past 10 years) ----------------
LOCAL = now.astimezone(timezone(timedelta(hours=-5)))
MONTH = LOCAL.month
MONTH_KEY = f"{LOCAL.year}-{MONTH:02d}"

PREV, PREV_ACCESS = {}, None
try:
    with open("data.json") as fh:
        _old = json.load(fh)
        PREV = {o["id"]: o for o in _old.get("sites", []) if isinstance(o, dict) and o.get("id")}
        PREV_ACCESS = _old.get("access")
except Exception:
    pass


def water_type_from_name(name, default="river"):
    """Lake (circle on the map) or river (square), judged from a station's name."""
    n = f" {(name or '').upper()} "
    if any(w in n for w in (" LAKE", " POND", "RESERVOIR", " DAM ", " DAM,", " DAM(", " BAY ")):
        return "lake"
    if any(w in n for w in ("RIVER", "BROOK", "STREAM", "CREEK", " R ", " BR ")):
        return "river"
    return default


def month_window(years_back):
    y = LOCAL.year - years_back
    start = datetime(y, MONTH, 1, tzinfo=timezone.utc)
    end = datetime(y + (MONTH == 12), MONTH % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def baseline_from_points(pts):
    """Average of each past year's average for this month (each year counts equally)."""
    by_year = {}
    for t, v in pts:
        if t.month == MONTH and t.year < LOCAL.year:
            by_year.setdefault(t.year, []).append(v)
    good = {y: vals for y, vals in by_year.items() if len(vals) >= 10}
    if len(good) < 3:
        return {"month": MONTH_KEY, "avg": None}
    yearly = [sum(v) / len(v) for v in good.values()]
    return {"month": MONTH_KEY, "avg": round(sum(yearly) / len(yearly), 2), "years": len(good)}


def cached_baseline(site_id):
    b = (PREV.get(site_id) or {}).get("baseline")
    return b if isinstance(b, dict) and b.get("month") == MONTH_KEY else None


def yearly_baseline(fetch):
    """fetch(start, end) -> points; called once per past year."""
    pts = []
    for yb in range(1, 11):
        start, end = month_window(yb)
        try:
            pts += fetch(start, end)
        except Exception:
            pass
        time.sleep(0.2)
    return baseline_from_points(pts)


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
        s["waterType"] = (PREV.get(s["id"]) or {}).get("waterType")
        if not s["name"] or not s["waterType"]:
            time.sleep(0.3)
            try:
                loc = usgs(f"monitoring-locations/items/{urllib.parse.quote(s['id'])}", {})
                props = loc.get("properties") or {}
                s["name"] = props.get("monitoring_location_name") or s["name"] or s["id"]
                code = str(props.get("site_type_code") or "").upper()
                if code.startswith("LK"):
                    s["waterType"] = "lake"
                elif code.startswith("ES") or code.startswith("OC"):
                    s["waterType"] = "tidal"
                elif code.startswith("ST"):
                    s["waterType"] = "river"
                else:
                    s["waterType"] = water_type_from_name(s["name"])
            except Exception:
                s["name"] = s["name"] or s["id"]
                s["waterType"] = water_type_from_name(s["name"])
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

    RAW_BUDGET = 40  # gauges per run that may use raw 15-minute history (about 10 requests each)
    computed = raw_used = 0
    start10 = datetime(LOCAL.year - 10, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end10 = month_window(0)[0].strftime("%Y-%m-%dT%H:%M:%SZ")

    def raw_month(sid, st, en):
        data = usgs("continuous/items", {"monitoring_location_id": sid, "parameter_code": "00065",
                                         "datetime": st.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" + en.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "limit": 10000, "properties": "time,value"})
        pts = []
        for f in data.get("features", []):
            pr = f.get("properties", {})
            try:
                pts.append((parse_time(pr["time"]), float(pr["value"])))
            except Exception:
                pass
        return pts

    for s in sites.values():
        if s.get("height") is None:
            continue
        b = cached_baseline(s["id"])
        # 1) quick try: USGS daily averages of water level, when the gauge has them
        if b is None:
            computed += 1
            b = {"month": MONTH_KEY, "avg": None, "via": "daily"}
            try:
                data = usgs("daily/items", {"monitoring_location_id": s["id"], "parameter_code": "00065",
                                            "statistic_id": "00003", "datetime": f"{start10}/{end10}", "limit": 10000})
                pts = []
                for f in data.get("features", []):
                    pr = f.get("properties", {})
                    try:
                        pts.append((parse_time(pr["time"]), float(pr["value"])))
                    except Exception:
                        pass
                got = baseline_from_points(pts)
                if got.get("avg") is not None:
                    b = {**got, "via": "daily"}
            except Exception as e:
                errors.append(f"USGS daily {s['id']}: {e}")
            time.sleep(0.3)
        # 2) otherwise: raw readings for each past year's month, a batch of gauges per run
        if b.get("avg") is None and b.get("via") != "raw":
            if raw_used < RAW_BUDGET:
                raw_used += 1
                got = yearly_baseline(lambda st, en, sid=s["id"]: raw_month(sid, st, en))
                b = {**got, "via": "raw"}
            else:
                b = {"month": MONTH_KEY, "avg": None, "via": "daily", "pending": True}
        s["baseline"] = b
    have = sum(1 for x in sites.values() if (x.get("baseline") or {}).get("avg") is not None)
    pending = sum(1 for x in sites.values() if (x.get("baseline") or {}).get("pending"))
    print(f"USGS monthly averages: {have} gauges have one; {raw_used} worked out from raw readings this run; "
          f"{pending} still waiting for a later run")
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
        s["waterType"] = "lake" if (level and level.split(".")[1] == "Elev") else water_type_from_name(s["name"])
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
                    b = cached_baseline(s["id"])
                    if b is None:
                        b = yearly_baseline(lambda st, en: corps_series(level, days=(en - st).days, end=en))
                    s["baseline"] = b
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


def des_list(url, max_pages=50):
    """Get a list from NHDES, whether it comes plain, wrapped, or split into pages."""
    items, pages = [], 0
    while url and pages < max_pages:
        data = get(url)
        pages += 1
        nxt = None
        while isinstance(data, dict):
            nxt = nxt or (data.get("next") if isinstance(data.get("next"), str) else None)
            for key in ("features", "results", "data", "items"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = [data] if data.get("id") else []
        items += [d for d in data if isinstance(d, dict)]
        url = nxt
    return items


def des_values(ts_id, start, end):
    url = f"{DES}/timeseries/{ts_id}/values/?" + urllib.parse.urlencode({
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"), "end": end.strftime("%Y-%m-%dT%H:%M:%S")})
    rows = des_list(url, max_pages=20)
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
        params = des_list(f"{DES}/parameters/")
        pinfo = {p["id"]: p for p in params if p.get("id")}
        stations = des_list(f"{DES}/locations/?layer__code=STATIONS")
        print(f"NHDES: station list has {len(stations)} stations, {len(pinfo)} reading types")
        if not stations:
            print("NHDES station list sample reply:", json.dumps(get(f"{DES}/locations/?layer__code=STATIONS"))[:600])
    except Exception as e:
        errors.append(f"NHDES station list: {e}")
        return []

    out, own_ts, no_coords, skipped_agency = [], 0, 0, 0
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
            no_coords += 1
            continue
        time.sleep(0.2)
        try:
            tslist = des_list(f"{DES}/timeseries/sparse/?" + urllib.parse.urlencode({"location__code": code}))
        except Exception as e:
            errors.append(f"NHDES {code} list: {e}")
            continue

        best = {}
        for ts in tslist or []:
            if not isinstance(ts, dict) or not ts.get("id"):
                continue
            tcode = (ts.get("code") or "").upper()
            if ".USGS." in tcode or ".NWS." in tcode or ".USACE." in tcode:
                skipped_agency += 1
                continue  # already on the map from the original agency
            param = pinfo.get(ts.get("parameter")) or {"name": ts.get("name", ""), "code": tcode.split(".")[2] if tcode.count(".") >= 2 else ""}
            kind = des_kind(param)
            if kind and kind not in best:
                best[kind] = (ts["id"], param)
        if not best:
            continue
        own_ts += 1

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
                    s["waterType"] = "lake" if s["levelLabel"] == "Lake level" else water_type_from_name(s["name"])
                    b = cached_baseline(s["id"])
                    if b is None:
                        b = yearly_baseline(lambda st, en: des_values(ts_id, st, en))
                    s["baseline"] = b
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
        s.setdefault("waterType", water_type_from_name(s["name"], "lake"))
        if any(k in s for k in ("height", "temp", "flow")):
            out.append(s)
    print(f"NHDES: {own_ts} stations have state readings, {skipped_agency} readings skipped as USGS/NWS/Army Corps copies, "
          f"{no_coords} stations without a map location")
    return out


# ---------------- Boat ramps and cartop access (NH Fish and Game, via GRANIT) ----------------
ACCESS_URL = "https://granit24a.sr.unh.edu/nhgeodata/rest/services/CSD/RecreationResources/MapServer/0/query"
ACCESS_FIELDS = "FACILITY,WATER_BODY,TOWN,OWNERSHIP,ACCESS_TYP,WB_TYPE,FEE,PARKING_TY,RAMP,DOCK,ADDITIONAL,HCAP_ACC,LATITUDE,LONGITUDE"


def access_kind(access_type, ramp):
    t, r = (access_type or "").lower(), (ramp or "").lower()
    if "potential" in t:
        return None  # not developed or signed
    if any(w in t for w in ("cartop", "car top", "car-top", "carry", "canoe", "kayak", "paddl")):
        return "cartop"
    if any(w in t for w in ("trailer", "ramp", "boat launch", "boat access")):
        return "ramp"
    if "car" in r:
        return "cartop"
    if r and r not in ("none", "no", "n", "na", "n/a", "0"):
        return "ramp"
    return None


def collect_access():
    if isinstance(PREV_ACCESS, dict) and PREV_ACCESS.get("month") == MONTH_KEY and PREV_ACCESS.get("sites"):
        print(f"Boat access: reused {len(PREV_ACCESS['sites'])} sites from earlier this month")
        return PREV_ACCESS
    out, types, offset = [], {}, 0
    for _ in range(20):
        params = {"where": "1=1", "outFields": ACCESS_FIELDS, "returnGeometry": "true", "outSR": "4326",
                  "f": "json", "resultOffset": offset, "resultRecordCount": 1000, "orderByFields": "OBJECTID"}
        data = get(ACCESS_URL + "?" + urllib.parse.urlencode(params))
        feats = data.get("features") or []
        for f in feats:
            a, g = f.get("attributes") or {}, f.get("geometry") or {}
            lat, lon = g.get("y") or a.get("LATITUDE"), g.get("x") or a.get("LONGITUDE")
            if lat is None or lon is None:
                continue
            types[a.get("ACCESS_TYP") or "(blank)"] = types.get(a.get("ACCESS_TYP") or "(blank)", 0) + 1
            kind = access_kind(a.get("ACCESS_TYP"), a.get("RAMP"))
            if not kind:
                continue
            clean = lambda k: (str(a.get(k)).strip() if a.get(k) not in (None, "", " ") else None)
            out.append({"kind": kind, "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                        "name": clean("FACILITY") or clean("WATER_BODY") or "Public access",
                        "water": clean("WATER_BODY"), "town": clean("TOWN"), "type": clean("ACCESS_TYP"),
                        "waterType": clean("WB_TYPE"), "ramp": clean("RAMP"), "parking": clean("PARKING_TY"),
                        "fee": clean("FEE"), "dock": clean("DOCK"), "accessible": clean("HCAP_ACC"),
                        "owner": clean("OWNERSHIP"), "notes": clean("ADDITIONAL")})
        if len(feats) < 1000 and not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
        if not feats:
            break
    print("Boat access types in the state data:", ", ".join(f"{k} ({v})" for k, v in sorted(types.items(), key=lambda x: -x[1])[:15]))
    print(f"Boat access: {sum(1 for x in out if x['kind'] == 'ramp')} boat ramps, "
          f"{sum(1 for x in out if x['kind'] == 'cartop')} cartop sites")
    return {"month": MONTH_KEY, "sites": out}


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
    print(f"Army Corps: {len(corps)} stations, {sum(1 for x in corps if (x.get('baseline') or {}).get('avg') is not None)} with monthly average")
    des = safe("NHDES", collect_nhdes)
    print(f"NHDES: {len(des)} stations "
          f"({sum(1 for x in des if 'temp' in x)} with water temp, {sum(1 for x in des if (x.get('baseline') or {}).get('avg') is not None)} with monthly average)")
    all_sites = sites + corps + des
    if not all_sites:
        print("Nothing collected this time; keeping the previous data.json.")
    else:
        acc = safe("Boat access", collect_access)
        if not isinstance(acc, dict):
            acc = PREV_ACCESS  # keep last month's list if the state server is down
        result = {"updated": now.isoformat(), "errors": errors[:50], "sites": all_sites, "access": acc}
        with open("data.json", "w") as fh:
            json.dump(result, fh, separators=(",", ":"))
    for e in errors[:20]:
        print("Warning:", e)
