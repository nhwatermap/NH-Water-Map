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


def water_f(raw, label_says_celsius, where="", site=None):
    """Water temperature in °F, with a sanity check: NH water is always roughly 30-90 °F.
    If the station's label gives an impossible number, try the two common mistakes: a Celsius
    reading labeled Fahrenheit (or the reverse), and a reading converted to Fahrenheit twice.
    If one of those is believable, use it and say so; otherwise drop the reading."""
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return None
    ok = lambda f: 30 <= f <= 90
    as_label = raw * 9 / 5 + 32 if label_says_celsius else raw
    if ok(as_label):
        return round(as_label, 1)
    fixes = [raw * 9 / 5 + 32, raw, (raw - 32) * 5 / 9, (as_label - 32) * 5 / 9]
    for f in fixes:
        if ok(f):
            print(f"Note: {where} temperature posted as {round(as_label, 1)} °F looks like a unit error; using {round(f, 1)} °F")
            if site is not None:
                site["tempNote"] = (f"The agency posted {round(as_label, 1)} °F, which isn't possible here. It looks like a "
                                    f"unit mix-up in their system, so the corrected value is shown.")
            return round(f, 1)
    print(f"Note: {where} temperature {raw} doesn't make sense in any unit; left off the map")
    return None


# ---------------- Summer (growing season) level: June-August average ----------------
def summer_window():
    """This summer so far (in Jun-Aug), this summer (Sep-Dec), or last summer (Jan-May)."""
    y = LOCAL.year
    if LOCAL.month in (6, 7, 8):
        start = datetime(y, 6, 1, tzinfo=timezone.utc)
        end = datetime(LOCAL.year, LOCAL.month, LOCAL.day, tzinfo=timezone.utc)  # through yesterday
        return start, end, y, True
    if LOCAL.month < 6:
        y -= 1
    return datetime(y, 6, 1, tzinfo=timezone.utc), datetime(y, 9, 1, tzinfo=timezone.utc), y, False


def summer_key(year, partial):
    return f"{year}-JJA" + (f"-to-{LOCAL:%Y%m%d}" if partial else "")


def summer_from_points(pts, start, end, year, partial):
    """Average of daily averages, so frequent and infrequent stations count the same."""
    days = {}
    for t, v in pts:
        if start <= t < end:
            days.setdefault(t.date(), []).append(v)
    if len(days) < 7:
        return None
    daily = [sum(v) / len(v) for v in days.values()]
    last = max(days)
    return {"key": summer_key(year, partial), "avg": round(sum(daily) / len(daily), 2), "year": year,
            "days": len(days), "partial": partial, "through": last.isoformat() if partial else None}


def summer_level(site_id, fetch):
    """Summer average, reused while still current: all season once summer is over, or for the
    day while summer is under way. If this summer has under 7 days so far, use last summer."""
    start, end, year, partial = summer_window()
    key = summer_key(year, partial)
    old = (PREV.get(site_id) or {}).get("summer")
    if isinstance(old, dict) and old.get("key") == key:
        return old
    try:
        got = summer_from_points(fetch(start, end), start, end, year, partial)
        if got is None and partial:
            ls, le = datetime(year - 1, 6, 1, tzinfo=timezone.utc), datetime(year - 1, 9, 1, tzinfo=timezone.utc)
            got = summer_from_points(fetch(ls, le), ls, le, year - 1, False)
        got = got or {"avg": None}
        got["key"] = key
        return got
    except Exception as e:
        errors.append(f"Summer level {site_id}: {e}")
        return None


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
            if key == "temp":
                val = water_f(val, "c" in (p.get("unit_of_measure") or "degC").lower(), f"USGS {sid}", s)
                if val is None:
                    continue
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

    # Summer level: one request per gauge, then reused (for the season, or for the day during summer)
    SUMMER_BUDGET, summer_used = 70, 0
    for s in sites.values():
        if s.get("height") is None:
            continue
        start, end, year, partial = summer_window()
        old = (PREV.get(s["id"]) or {}).get("summer")
        fresh_enough = isinstance(old, dict) and old.get("key") == summer_key(year, partial)
        if not fresh_enough and summer_used >= SUMMER_BUDGET:
            s["summer"] = {"avg": None, "pending": True}
            continue
        if not fresh_enough:
            summer_used += 1
            time.sleep(0.3)
        s["summer"] = summer_level(s["id"], lambda st, en, sid=s["id"]: raw_month(sid, st, en))
    print(f"Summer levels: {sum(1 for x in sites.values() if (x.get('summer') or {}).get('avg') is not None)} USGS gauges have one; "
          f"{summer_used} worked out this run")
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
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "page-size": 12000}
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
                    s["summer"] = summer_level(s["id"], lambda st, en: corps_series(level, days=max(1, (en - st).days), end=en))
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
                    v = water_f(pts[-1][1], False, f"Army Corps {loc}", s)  # asked for English units
                    if v is None:
                        raise ValueError("implausible water temperature")
                    s["temp"] = v
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
                    s["summer"] = summer_level(s["id"], lambda st, en: des_values(ts_id, st, en))
            except Exception as e:
                errors.append(f"NHDES {code} level: {e}")
        if "temp" in best:
            ts_id, param = best["temp"]
            try:
                pts = des_values(ts_id, now - timedelta(days=2), now)
                if pts:
                    u = param.get("unit")
                    unit = str((u.get("code") or u.get("name") or "") if isinstance(u, dict) else (u or "")).upper()
                    says_c = unit in ("C", "DEGC", "DEG C", "CEL", "CELSIUS") or ("C" in unit and "F" not in unit)
                    v = water_f(pts[-1][1], says_c, f"NHDES {pr.get('name') or code}", s)
                    if v is not None:
                        s["temp"] = v
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


# ---------------- Seacoast tides (NOAA CO-OPS) ----------------
NOAA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
NOAA_MD = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
COAST_BOX = (42.84, -71.05, 43.30, -70.50)  # Hampton/Seabrook to Dover and Salmon Falls, out to the Isles of Shoals


def in_coast(lat, lon):
    return COAST_BOX[0] <= lat <= COAST_BOX[2] and COAST_BOX[1] <= lon <= COAST_BOX[3]


def noaa(params):
    q = {"application": "nh_water_map", "format": "json", "time_zone": "gmt", "units": "english", **params}
    data = get(NOAA + "?" + urllib.parse.urlencode(q))
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(data["error"].get("message", "NOAA error"))
    return data


def noaa_stations(kind):
    data = get(NOAA_MD + "?" + urllib.parse.urlencode({"type": kind}))
    out = {}
    for st in data.get("stations", []):
        try:
            lat, lon = float(st["lat"]), float(st["lng"])
        except Exception:
            continue
        if in_coast(lat, lon):
            out[str(st["id"])] = {"id": str(st["id"]), "name": st.get("name", str(st["id"])), "lat": lat, "lon": lon,
                                  "state": st.get("state")}
    return out


def noaa_time(t):
    return datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def collect_tides():
    pred_st = noaa_stations("tidepredictions")
    try:
        live_st = noaa_stations("waterlevels")
    except Exception as e:
        errors.append(f"NOAA live station list: {e}")
        live_st = {}
    print(f"Tides: {len(pred_st)} prediction points and {len(live_st)} live NOAA stations on the seacoast")
    begin = (now - timedelta(hours=12)).strftime("%Y%m%d %H:%M")
    stations, temps = [], []
    for sid, st in pred_st.items():
        time.sleep(0.2)
        try:
            data = noaa({"product": "predictions", "station": sid, "begin_date": begin, "range": 7 * 24 + 12,
                         "datum": "MLLW", "interval": "hilo"})
            hilo = [[noaa_time(p["t"]).isoformat(), round(float(p["v"]), 2), p.get("type", "")]
                    for p in data.get("predictions", [])]
        except Exception as e:
            errors.append(f"NOAA tides {sid}: {e}")
            continue
        if hilo:
            stations.append({**st, "hilo": hilo})

    # Live stations: water level vs. predicted, water temperature, wind
    by_id = {s["id"]: s for s in stations}
    for sid, st in live_st.items():
        s = by_id.get(sid)
        if s is None:
            s = {**st, "hilo": []}
            stations.append(s)
            by_id[sid] = s
        s["live"] = True
        try:
            wl = noaa({"product": "water_level", "station": sid, "date": "latest", "datum": "MLLW"}).get("data", [])
            if wl:
                t_obs, v_obs = noaa_time(wl[-1]["t"]), float(wl[-1]["v"])
                pr = noaa({"product": "predictions", "station": sid, "datum": "MLLW", "interval": "6",
                           "begin_date": (t_obs - timedelta(minutes=30)).strftime("%Y%m%d %H:%M"), "range": 1}).get("predictions", [])
                s["level"] = round(v_obs, 2)
                s["levelTime"] = t_obs.isoformat()
                if pr:
                    best = min(pr, key=lambda p: abs((noaa_time(p["t"]) - t_obs).total_seconds()))
                    s["vsPredicted"] = round(v_obs - float(best["v"]), 2)
        except Exception as e:
            errors.append(f"NOAA water level {sid}: {e}")
        for product, key in (("water_temperature", "temp"), ("air_temperature", "airTemp")):
            try:
                d = noaa({"product": product, "station": sid, "date": "latest"}).get("data", [])
                if d and d[-1].get("v") not in (None, ""):
                    s[key] = round(float(d[-1]["v"]), 1)
                    s[key + "Time"] = noaa_time(d[-1]["t"]).isoformat()
            except Exception:
                pass
        try:
            d = noaa({"product": "wind", "station": sid, "date": "latest"}).get("data", [])
            if d and d[-1].get("s") not in (None, ""):
                w = d[-1]
                s["wind"] = {"speed": round(float(w["s"])), "gust": round(float(w.get("g") or 0)),
                             "dir": w.get("dr"), "deg": float(w.get("d") or 0), "time": noaa_time(w["t"]).isoformat()}
        except Exception:
            pass
        if "temp" in s:
            # Also feed the regular "Water temp" view
            temps.append({"id": f"NOAA-{sid}", "source": "NOAA", "lat": s["lat"], "lon": s["lon"], "name": s["name"],
                          "waterType": "tidal", "temp": s["temp"], "tempTime": s["tempTime"]})
    print(f"Tides: forecasts for {sum(1 for x in stations if x['hilo'])} points; "
          f"{sum(1 for x in stations if 'level' in x)} live water levels; {len(temps)} water temperatures")
    return {"updated": now.isoformat(), "stations": stations, "temps": temps}


# ---------------- Seacoast water temperature: Great Bay Reserve (NERACOOS) and buoys (NDBC) ----------------
ERDDAPS = ["https://www.neracoos.org/erddap", "https://data.neracoos.org/erddap"]
BUOY_BOX = (42.60, -71.10, 43.40, -70.00)  # NH coast out to Jeffreys Ledge


def c_to_f(v, units):
    u = (units or "").lower()
    return v * 9 / 5 + 32 if ("c" in u and "f" not in u) or "celsius" in u or u in ("degree_c", "degc") else v


def erddap_rows(base, dataset, columns, since):
    url = f"{base}/tabledap/{dataset}.json?" + ",".join(columns) + "&time>=" + since
    data = get(url.replace(" ", "%20"))
    t = data.get("table", {})
    names, units = t.get("columnNames", []), t.get("columnUnits", [])
    return [dict(zip(names, r)) for r in t.get("rows", [])], dict(zip(names, units))


def collect_great_bay(existing):
    out = []
    since = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for base in ERDDAPS:
        try:
            found = get(f"{base}/search/index.json?" + urllib.parse.urlencode(
                {"searchFor": "Great Bay NERRS water quality", "page": 1, "itemsPerPage": 100}))
        except Exception as e:
            errors.append(f"Great Bay search at {base}: {e}")
            continue
        t = found.get("table", {})
        cols = t.get("columnNames", [])
        rows = [dict(zip(cols, r)) for r in t.get("rows", [])]
        ids = [r.get("Dataset ID") for r in rows if r.get("Dataset ID") and "GRB" in r["Dataset ID"].upper()
               and "WQ" in r["Dataset ID"].upper()]
        print(f"Great Bay Reserve: {len(ids)} water monitors listed at {base}")
        for ds in ids:
            title = next((r.get("Title") for r in rows if r.get("Dataset ID") == ds), ds)
            time.sleep(0.3)
            try:
                rws, units = erddap_rows(base, ds, ["time", "latitude", "longitude", "sea_water_temperature", "sea_water_salinity"], since)
            except Exception:
                try:
                    rws, units = erddap_rows(base, ds, ["time", "latitude", "longitude", "sea_water_temperature"], since)
                except Exception as e:
                    errors.append(f"Great Bay {ds}: {e}")
                    continue
            rws = [r for r in rws if r.get("sea_water_temperature") is not None]
            if not rws:
                continue  # monitor out for the season or not reporting
            last = max(rws, key=lambda r: r["time"])
            name = str(title).replace("Great Bay,NH.", "").replace("Great Bay, NH.", "").replace("WQ station", "").strip() or ds
            s = {"id": f"GRB-{ds}", "source": "Great Bay Reserve", "lat": float(last["latitude"]), "lon": float(last["longitude"]),
                 "name": f"Great Bay Reserve – {name}", "waterType": "tidal",
                 "temp": round(c_to_f(float(last["sea_water_temperature"]), units.get("sea_water_temperature")), 1),
                 "tempTime": last["time"]}
            if last.get("sea_water_salinity") is not None:
                s["salinity"] = round(float(last["sea_water_salinity"]), 1)
            out.append(s)
        if ids:
            break  # found the catalog; no need to try the other address
    return out


def collect_buoys(existing):
    out = []
    try:
        req = urllib.request.Request("https://www.ndbc.noaa.gov/activestations.xml", headers={"User-Agent": "nh-water-map"})
        with urllib.request.urlopen(req, timeout=60) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        errors.append(f"NDBC station list: {e}")
        return out
    import re as _re
    stations = []
    for m in _re.finditer(r"<station\s([^>]*)/?>", xml):
        a = dict(_re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except Exception:
            continue
        if BUOY_BOX[0] <= lat <= BUOY_BOX[2] and BUOY_BOX[1] <= lon <= BUOY_BOX[3] and a.get("met", "n") == "y":
            stations.append((a["id"].upper(), a.get("name", a["id"]), lat, lon))
    print(f"Buoys: {len(stations)} NDBC stations off the NH coast")
    for sid, name, lat, lon in stations:
        # Skip stations that duplicate one we already have (NDBC also republishes NOAA tide stations)
        if any(abs(e["lat"] - lat) < 0.01 and abs(e["lon"] - lon) < 0.01 for e in existing):
            continue
        time.sleep(0.3)
        try:
            req = urllib.request.Request(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt", headers={"User-Agent": "nh-water-map"})
            with urllib.request.urlopen(req, timeout=60) as r:
                lines = r.read().decode("utf-8", "replace").splitlines()
        except Exception:
            continue
        if len(lines) < 3:
            continue
        head = lines[0].lstrip("#").split()
        rec = None
        for line in lines[2:30]:  # newest first; take the first row with a water temperature
            vals = dict(zip(head, line.split()))
            if vals.get("WTMP", "MM") != "MM":
                rec = vals
                break
        if not rec:
            continue
        try:
            t = datetime(int(rec["YY"]), int(rec["MM"]), int(rec["DD"]), int(rec["hh"]), int(rec["mm"]), tzinfo=timezone.utc)
        except Exception:
            continue
        s = {"id": f"NDBC-{sid}", "source": "NOAA buoy", "lat": lat, "lon": lon, "name": name.split(" - ")[-1].strip() or sid,
             "waterType": "tidal", "temp": round(float(rec["WTMP"]) * 9 / 5 + 32, 1), "tempTime": t.isoformat(), "ndbcId": sid}
        if rec.get("WVHT", "MM") != "MM":
            s["waves"] = round(float(rec["WVHT"]) * 3.28084, 1)  # metres to feet
        if rec.get("WSPD", "MM") != "MM":
            mph = lambda ms: round(float(ms) * 2.23694)
            s["wind"] = {"speed": mph(rec["WSPD"]), "gust": mph(rec["GST"]) if rec.get("GST", "MM") != "MM" else 0,
                         "deg": float(rec["WDIR"]) if rec.get("WDIR", "MM") != "MM" else None}
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
    print(f"Army Corps: {len(corps)} stations, {sum(1 for x in corps if (x.get('baseline') or {}).get('avg') is not None)} with monthly average")
    des = safe("NHDES", collect_nhdes)
    print(f"NHDES: {len(des)} stations "
          f"({sum(1 for x in des if 'temp' in x)} with water temp, {sum(1 for x in des if (x.get('baseline') or {}).get('avg') is not None)} with monthly average)")
    tides = safe("Tides", collect_tides)
    if not isinstance(tides, dict):
        tides = None
    coast_temps = (tides or {}).get("temps") or []
    grb = safe("Great Bay Reserve", lambda: collect_great_bay(coast_temps))
    buoys = safe("Buoys", lambda: collect_buoys(coast_temps + grb))
    print(f"Seacoast water temps: {len(coast_temps)} NOAA tide stations, {len(grb)} Great Bay monitors, {len(buoys)} buoys")
    all_sites = sites + corps + des + coast_temps + grb + buoys
    if not all_sites:
        print("Nothing collected this time; keeping the previous data.json.")
    else:
        acc = safe("Boat access", collect_access)
        if not isinstance(acc, dict):
            acc = PREV_ACCESS  # keep last month's list if the state server is down
        if tides is None:
            try:
                with open("data.json") as fh:
                    tides = json.load(fh).get("tides")  # keep the last good tide forecast
            except Exception:
                tides = None
        result = {"updated": now.isoformat(), "errors": errors[:50], "sites": all_sites, "access": acc, "tides": tides}
        with open("data.json", "w") as fh:
            json.dump(result, fh, separators=(",", ":"))
    for e in errors[:20]:
        print("Warning:", e)
