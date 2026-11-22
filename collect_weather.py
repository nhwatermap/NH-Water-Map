"""
Collects weather for the map's weather layers from NOAA's GFS model (public domain).

Past 5 days come from NOAA's own recent model analyses (what actually happened);
the next 5 days come from the latest forecast. Covers New Hampshire and the area
to the north and west that matters for waterfowl migration.

Each day (New Hampshire local date) is boiled down to: overnight low, daytime high,
average and strongest wind, snow depth, new snow, and how much of the day it snowed.
Saves weather.json. Runs in GitHub's twice-daily update; needs the pygrib decoder.
"""
import json, os, sys, tempfile, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

import warnings
import numpy as np
import pygrib

warnings.filterwarnings('ignore', message='All-NaN slice encountered')
warnings.filterwarnings('ignore', message='Mean of empty slice')

FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
PROD = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
REGION = {"leftlon": -78, "rightlon": -65, "toplat": 50, "bottomlat": 41}
PAST_DAYS, FUTURE_DAYS = 5, 5
LOCAL = timezone(timedelta(hours=-5))   # New Hampshire standard time; days run midnight to midnight
OUT = "weather.json"

# What to keep from each file, identified by NOAA's parameter numbers and level
WANT = {
    (0, 0, 0, "heightAboveGround", 2): "t2",      # temperature at 2 m, kelvin
    (0, 0, 4, "heightAboveGround", 2): "tmax2",   # highest temperature since the last output, kelvin
    (0, 0, 5, "heightAboveGround", 2): "tmin2",   # lowest temperature since the last output, kelvin
    (0, 2, 2, "heightAboveGround", 10): "u10",    # wind toward the east at 10 m, m/s
    (0, 2, 3, "heightAboveGround", 10): "v10",    # wind toward the north at 10 m, m/s
    (0, 1, 11, "surface", 0): "snod",             # snow depth, m
    (0, 1, 195, "surface", 0): "csnow",           # snowing right now? 1 or 0
}

now = datetime.now(timezone.utc)
requests_made = 0


def get(url, tries=3, timeout=90):
    global requests_made
    for i in range(tries):
        try:
            requests_made += 1
            req = urllib.request.Request(url, headers={"User-Agent": "nh-water-map"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(5 * (i + 1))
    return None


def cycle_dir(cycle):
    return f"/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos"


def cycle_ready(cycle, last_hour):
    """A run is usable once its last forecast hour we need has been published."""
    url = f"{PROD}{cycle_dir(cycle)}/gfs.t{cycle:%H}z.pgrb2.0p25.f{last_hour:03d}.idx"
    return get(url, tries=2, timeout=30) is not None


def latest_cycle(last_hour):
    c = now.replace(minute=0, second=0, microsecond=0)
    c -= timedelta(hours=c.hour % 6)
    for _ in range(6):
        if cycle_ready(c, last_hour):
            return c
        time.sleep(1)
        c -= timedelta(hours=6)
    raise SystemExit("No recent GFS run is available from NOAA right now; keeping the previous weather.json")


def fetch(cycle, fhour):
    """Our region and variables from one GFS file, as {name: 2-D array}, plus the grid."""
    params = {"file": f"gfs.t{cycle:%H}z.pgrb2.0p25.f{fhour:03d}", "dir": cycle_dir(cycle), "subregion": "",
              **REGION, "lev_2_m_above_ground": "on", "lev_10_m_above_ground": "on", "lev_surface": "on",
              "var_TMP": "on", "var_TMAX": "on", "var_TMIN": "on",
              "var_UGRD": "on", "var_VGRD": "on", "var_SNOD": "on", "var_CSNOW": "on"}
    raw = get(FILTER + "?" + urllib.parse.urlencode(params))
    time.sleep(1.2)  # polite pacing for NOAA's server
    if not raw:
        return None
    return decode(raw)


def decode(raw):
    with tempfile.NamedTemporaryFile(suffix=".grb2", delete=False) as fh:
        fh.write(raw)
        path = fh.name
    fields, grid = {}, None
    try:
        for msg in pygrib.open(path):
            key = (msg["discipline"], msg["parameterCategory"], msg["parameterNumber"], msg["typeOfLevel"], int(msg["level"]))
            name = WANT.get(key)
            if not name:
                continue
            vals = np.ma.filled(msg.values.astype(float), np.nan)
            lats, lons = msg.latlons()
            if lats[0, 0] > lats[-1, 0]:      # store south-to-north
                vals, lats = vals[::-1], lats[::-1]
            lons = np.where(lons > 180, lons - 360, lons)
            fields[name] = vals
            grid = {"lat0": round(float(lats[0, 0]), 4), "lon0": round(float(lons[0, 0]), 4),
                    "dlat": round(float(lats[1, 0] - lats[0, 0]), 4), "dlon": round(float(lons[0, 1] - lons[0, 0]), 4),
                    "nlat": int(vals.shape[0]), "nlon": int(vals.shape[1])}
    finally:
        os.remove(path)
    return (fields, grid) if fields else None


# ---------------- Measured daily lows/highs from weather stations (NOAA ACIS) ----------------
ACIS = "https://data.rcc-acis.org/MultiStnData"
OBS_BOX = "-73.8,42.0,-66.9,47.6"          # New England and nearby
CHECK_SPOTS = {"Plymouth": (43.757, -71.689), "Campton": (43.849, -71.636), "Laconia": (43.528, -71.470)}


def acis_num(v):
    if isinstance(v, list):
        v = v[0]
    v = str(v).strip()
    if v in ("", "M", "S"):
        return None
    if v == "T":
        return 0.0
    try:
        return float(v.rstrip("ASAF"))   # drop accumulation/flag suffixes
    except ValueError:
        return None


def collect_observed():
    today = now.astimezone(LOCAL).date()
    sdate, edate = today - timedelta(days=PAST_DAYS + 1), today
    params = {"bbox": OBS_BOX, "sdate": sdate.isoformat(), "edate": edate.isoformat(),
              "elems": "mint,maxt,snwd,snow", "meta": "name,ll,elev,sids"}
    body = urllib.parse.urlencode({"params": json.dumps(params)}).encode()
    req = urllib.request.Request(ACIS, data=body, headers={"User-Agent": "nh-water-map"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    dates = [(sdate + timedelta(days=i)).isoformat() for i in range((edate - sdate).days + 1)]
    stations = []
    for st in data.get("data", []):
        meta, rows = st.get("meta", {}), st.get("data", [])
        ll = meta.get("ll")
        if not ll or not rows:
            continue
        cols = list(zip(*rows)) if rows else []
        series = {k: [acis_num(v) for v in cols[i]] if i < len(cols) else [] for i, k in enumerate(("mint", "maxt", "snwd", "snow"))}
        if not any(v is not None for v in series["mint"] + series["maxt"]):
            continue
        stations.append({"name": meta.get("name", "").title(), "lon": round(ll[0], 4), "lat": round(ll[1], 4),
                         "elev": meta.get("elev"), **series})
    # Report: how many stations, and the closest ones to a few check spots
    yday = (today - timedelta(days=1)).isoformat()
    yi = dates.index(yday) if yday in dates else -1
    reporting = sum(1 for s in stations if yi >= 0 and yi < len(s["mint"]) and s["mint"][yi] is not None)
    print(f"Stations: {len(stations)} with temperature readings; {reporting} reported a low for {yday}")
    for label, (la, lo) in CHECK_SPOTS.items():
        near = sorted(stations, key=lambda s: (s["lat"] - la) ** 2 + ((s["lon"] - lo) * 0.72) ** 2)[:4]
        desc = []
        for s in near:
            miles = round((((s["lat"] - la) * 69) ** 2 + ((s["lon"] - lo) * 50) ** 2) ** 0.5)
            low = s["mint"][yi] if yi >= 0 and yi < len(s["mint"]) else None
            desc.append(f"{s['name']} ({miles} mi, low {'—' if low is None else round(low)}°F)")
        print(f"  Near {label}: " + "; ".join(desc))
    return {"dates": dates, "stations": stations}


# ---------------- Correct the model's lows with measured station lows ----------------
MAX_REACH = 60.0      # miles: stations farther than this don't affect a spot
NEAR = 5.0            # miles: softens the weight of a station right on top of a grid point
PULL_TO_MODEL = 1 / 30.0 ** 2   # with only distant stations, lean back toward the model
FORECAST_SHARE = 0.6  # use 60% of the recent difference for forecast days
MAX_FIX = 15.0        # never move a low more than 15 °F


def model_at(grid_vals, g, lat, lon):
    fi, fj = (lat - g["lat0"]) / g["dlat"], (lon - g["lon0"]) / g["dlon"]
    if fi < 0 or fj < 0 or fi > g["nlat"] - 1 or fj > g["nlon"] - 1:
        return None
    i, j = min(int(fi), g["nlat"] - 2), min(int(fj), g["nlon"] - 2)
    di, dj = fi - i, fj - j
    v = (grid_vals[i, j] * (1 - dj) + grid_vals[i, j + 1] * dj) * (1 - di) + (grid_vals[i + 1, j] * (1 - dj) + grid_vals[i + 1, j + 1] * dj) * di
    return None if np.isnan(v) else float(v)


def correct_lows(out, days, g, observed):
    """Adjust past days' lows toward what thermometers measured, and nudge forecast lows by the recent difference."""
    if not observed or not observed.get("stations"):
        return 0
    shape = (g["nlat"], g["nlon"])
    lats = g["lat0"] + g["dlat"] * np.arange(g["nlat"])
    lons = g["lon0"] + g["dlon"] * np.arange(g["nlon"])
    LAT, LON = np.meshgrid(lats, lons, indexing="ij")
    tmin = [np.where(np.array(d) == -9999, np.nan, np.array(d, dtype=float)).reshape(shape) for d in out["tmin"]]
    out["tmin_model"] = [list(d) for d in out["tmin"]]
    fixes, used = {}, 0
    for k, day in enumerate(days):
        if day["kind"] == "forecast" or day["date"] not in observed["dates"]:
            continue
        di = observed["dates"].index(day["date"])
        pts = []
        for st in observed["stations"]:
            low = st["mint"][di] if di < len(st["mint"]) else None
            mod = model_at(tmin[k], g, st["lat"], st["lon"]) if low is not None else None
            if low is None or mod is None or abs(low - mod) > 25:     # skip missing or suspicious readings
                continue
            pts.append((st["lat"], st["lon"], low - mod))
        if len(pts) < 5:
            continue
        num, den = np.zeros(shape), np.full(shape, PULL_TO_MODEL)
        for la, lo, b in pts:
            d = np.hypot((LAT - la) * 69.0, (LON - lo) * 69.0 * np.cos(np.radians(la)))
            w = np.where(d <= MAX_REACH, 1.0 / (d ** 2 + NEAR ** 2), 0.0)
            num += w * b; den += w
        fix = np.clip(num / den, -MAX_FIX, MAX_FIX)
        fixes[k] = fix
        tmin[k] = tmin[k] + fix
        day["lows"] = "adjusted to measured"
        used = max(used, len(pts))
    if fixes:
        recent = [fixes[k] for k in sorted(fixes)[-3:]]          # the last few days' differences
        trend = np.mean(recent, axis=0) * FORECAST_SHARE
        for k, day in enumerate(days):
            if day["kind"] == "forecast":
                tmin[k] = tmin[k] + trend
                day["lows"] = "adjusted for recent model error"
    out["tmin"] = [np.where(np.isnan(t), -9999, np.round(t)).astype(int).ravel().tolist() for t in tmin]
    return used


def main():
    last_needed = 3 * ((24 * (FUTURE_DAYS + 1) + 12) // 3)   # enough to finish the last local day
    cycle = latest_cycle(last_needed)
    print(f"Weather: using the {cycle:%Y-%m-%d %H}Z GFS run")

    samples = []   # (valid time, fields)
    grid = None
    # Past: each 6-hour run's 3- and 6-hour steps, back over the past days. Together they
    # cover every hour, and they carry the model's true lowest/highest temperature in between.
    c = cycle - timedelta(hours=6)
    oldest = now - timedelta(days=PAST_DAYS + 1)
    while c >= oldest:
        for fh in (3, 6):
            got = fetch(c, fh)
            if got:
                samples.append((c + timedelta(hours=fh), got[0]))
                grid = grid or got[1]
        c -= timedelta(hours=6)
    past_count = len(samples)
    # Forecast: the latest run, every 3 hours
    for fh in range(0, last_needed + 1, 3):
        got = fetch(cycle, fh)
        if got:
            samples.append((cycle + timedelta(hours=fh), got[0]))
            grid = grid or got[1]
    print(f"Weather: {past_count} past snapshots and {len(samples) - past_count} forecast snapshots "
          f"({requests_made} requests to NOAA)")
    if not samples or not grid:
        raise SystemExit("No weather data came back; keeping the previous weather.json")

    # Group by New Hampshire local date
    today = now.astimezone(LOCAL).date()
    dates = [today + timedelta(days=d) for d in range(-PAST_DAYS, FUTURE_DAYS + 1)]
    by_day = {d: [] for d in dates}
    for t, f in sorted(samples, key=lambda s: s[0]):
        d = t.astimezone(LOCAL).date()
        if d in by_day:
            by_day[d].append((t, f))

    shape = (grid["nlat"], grid["nlon"])
    nan = np.full(shape, np.nan)
    out = {k: [] for k in ("tmin", "tmax", "umean", "vmean", "wmax", "snod", "newsnow", "snowpct")}
    days = []

    def stack(rows, name):
        arrs = [f[name] for _, f in rows if name in f and f[name].shape == shape]
        return np.stack(arrs) if arrs else None

    def ints(a, scale=1):
        a = np.where(np.isfinite(a), np.round(a * scale), -9999).astype(int)
        return a.ravel().tolist()

    for d in dates:
        rows = by_day[d]
        kind = "today" if d == today else ("past" if d < today else "forecast")
        days.append({"date": d.isoformat(), "kind": kind, "snapshots": len(rows)})
        t = stack(rows, "t2")
        tlo = stack(rows, "tmin2")   # the model's true lowest temperature between outputs
        thi = stack(rows, "tmax2")   # and highest
        u, v = stack(rows, "u10"), stack(rows, "v10")
        sd, cs = stack(rows, "snod"), stack(rows, "csnow")
        k2f = lambda k: (k - 273.15) * 9 / 5 + 32
        ms2mph = 2.23694
        with np.errstate(all="ignore"):
            lows = [a for a in (t, tlo) if a is not None]
            highs = [a for a in (t, thi) if a is not None]
            out["tmin"].append(ints(k2f(np.nanmin(np.concatenate(lows), 0))) if lows else ints(nan))
            out["tmax"].append(ints(k2f(np.nanmax(np.concatenate(highs), 0))) if highs else ints(nan))
            out["umean"].append(ints(np.nanmean(u, 0) * ms2mph if u is not None else nan, 10))
            out["vmean"].append(ints(np.nanmean(v, 0) * ms2mph if v is not None else nan, 10))
            out["wmax"].append(ints(np.nanmax(np.hypot(u, v), 0) * ms2mph if u is not None and v is not None else nan))
            out["snod"].append(ints(np.nanmax(sd, 0) * 39.37 if sd is not None else nan, 10))            # inches x10
            out["newsnow"].append(ints(np.clip(sd[-1] - sd[0], 0, None) * 39.37 if sd is not None and len(sd) > 1 else nan, 10))
            out["snowpct"].append(ints(np.nanmean(cs, 0) * 100 if cs is not None else nan))

    try:
        observed = collect_observed()
    except Exception as e:
        print(f"Stations: couldn't get measured readings this time ({e}); the model weather is still saved")
        observed = None
    n = correct_lows(out, days, grid, observed)
    if n:
        print(f"Lows: past days adjusted to measured station lows (up to {n} stations a day); forecast lows nudged by the recent difference")
    result = {"built": now.isoformat(), "cycle": cycle.isoformat(), "source": "NOAA GFS 0.25°", "grid": grid, "observed": observed,
              "days": days, "units": {"tmin": "°F", "tmax": "°F", "umean": "mph x10 (toward east)", "vmean": "mph x10 (toward north)",
                                      "wmax": "mph", "snod": "inches x10", "newsnow": "inches x10", "snowpct": "% of day"},
              "missing": -9999, "fields": out}
    text = json.dumps(result, separators=(",", ":"))
    with open(OUT, "w") as fh:
        fh.write(text)
    covered = sum(1 for d in days if d["snapshots"] >= 4)
    print(f"Weather: {len(days)} days ({covered} with full coverage), grid {grid['nlat']}x{grid['nlon']}, "
          f"{len(text) / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
