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

import numpy as np
import pygrib

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

    result = {"built": now.isoformat(), "cycle": cycle.isoformat(), "source": "NOAA GFS 0.25°", "grid": grid,
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
