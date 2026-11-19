"""
Builds the map's own copy of New Hampshire's marshes, swamps and weedy ponds,
from the U.S. Fish and Wildlife Service National Wetlands Inventory (public domain).

Run by hand from GitHub's Actions tab ("Build wetlands"), about once a year.
Saves small chunk files in the wetlands/ folder so visitors' browsers draw the
shading themselves, without asking the Fish and Wildlife Service server.
Uses only Python's built-in tools.
"""
import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timezone

LAYER = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0"
BBOX = (-72.60, 42.69, -70.55, 45.31)       # New Hampshire plus a little margin
CELL = 0.1                                  # chunk size in degrees (about 5 x 7 miles)
SIMPLIFY = 0.00002                          # about 2 m: the server smooths shapes this much
BATCH = 500
OUT = "wetlands"

TYPES = ["Freshwater Emergent Wetland", "Estuarine and Marine Wetland", "Freshwater Forested/Shrub Wetland"]
WHERE = ("Wetlands.WETLAND_TYPE IN ('" + "','".join(TYPES) + "') "
         "OR Wetlands.ATTRIBUTE LIKE 'PAB%' OR Wetlands.ATTRIBUTE LIKE 'L2AB%'")


def call(params, tries=4):
    """POST a query (long ID lists don't fit in a web address)."""
    data = urllib.parse.urlencode({**params, "f": "json"}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(LAYER + "/query", data=data, headers={"User-Agent": "nh-water-map"})
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read().decode("utf-8"))
            if "error" in out:
                raise RuntimeError(out["error"].get("message", "server error"))
            return out
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 10 * (i + 1)
            print(f"  retrying in {wait}s after: {e}")
            time.sleep(wait)


def all_ids():
    """IDs of every matching wetland, asked for in 16 smaller areas."""
    ids = set()
    x0, y0, x1, y1 = BBOX
    n = 4
    for i in range(n):
        for j in range(n):
            box = {"xmin": x0 + (x1 - x0) * i / n, "ymin": y0 + (y1 - y0) * j / n,
                   "xmax": x0 + (x1 - x0) * (i + 1) / n, "ymax": y0 + (y1 - y0) * (j + 1) / n,
                   "spatialReference": {"wkid": 4326}}
            out = call({"where": WHERE, "geometry": json.dumps(box), "geometryType": "esriGeometryEnvelope",
                        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnIdsOnly": "true"})
            got = out.get("objectIds") or []
            ids.update(got)
            print(f"  area {i * n + j + 1}/{n * n}: {len(got)} wetlands")
            time.sleep(1)
    return sorted(ids)


def classify(attrs):
    """m = marsh / salt marsh, s = shrub or wooded swamp, w = weedy pond or shallows."""
    get = lambda k: next((v for key, v in attrs.items() if key.split(".")[-1].upper() == k), None)
    code = str(get("ATTRIBUTE") or "")
    wtype = str(get("WETLAND_TYPE") or "")
    acres = get("ACRES")
    if code.startswith("PAB") or code.startswith("L2AB"):
        cls = "w"
    elif wtype in (TYPES[0], TYPES[1]):
        cls = "m"
    elif wtype == TYPES[2]:
        cls = "s"
    else:
        return None
    try:
        acres = round(float(acres), 1)
    except (TypeError, ValueError):
        acres = None
    return cls, acres, code


def pack_ring(ring):
    """Coordinates as whole numbers of 1/100,000 degree (about 1 m), each stored as the
    step from the previous point. Much smaller files, same shape."""
    out, px, py = [], 0, 0
    for x, y in ring:
        ix, iy = round(x * 1e5), round(y * 1e5)
        if out and ix == px and iy == py:
            continue
        out += [ix - px, iy - py]
        px, py = ix, iy
    return out if len(out) >= 6 else None  # a ring needs at least 3 points


def main():
    started = time.time()
    print("Finding wetlands in and around New Hampshire…")
    ids = all_ids()
    print(f"Found {len(ids)} wetlands to download")

    chunks, counts = {}, {"m": 0, "s": 0, "w": 0}
    for b in range(0, len(ids), BATCH):
        part = ids[b:b + BATCH]
        out = call({"objectIds": ",".join(map(str, part)), "outFields": "*", "returnGeometry": "true",
                    "outSR": 4326, "maxAllowableOffset": SIMPLIFY, "geometryPrecision": 5})
        for f in out.get("features", []):
            c = classify(f.get("attributes") or {})
            rings = (f.get("geometry") or {}).get("rings") or []
            if not c or not rings:
                continue
            packed = [p for p in (pack_ring(r) for r in rings) if p]
            if not packed:
                continue
            xs = [x for r in rings for x, _ in r]
            ys = [y for r in rings for _, y in r]
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            key = f"{int((cx - BBOX[0]) // CELL)}_{int((cy - BBOX[1]) // CELL)}"
            ch = chunks.setdefault(key, {"f": [], "box": [180, 90, -180, -90]})
            ch["f"].append([c[0], c[1], c[2], packed])
            bx = ch["box"]
            bx[0], bx[1], bx[2], bx[3] = min(bx[0], min(xs)), min(bx[1], min(ys)), max(bx[2], max(xs)), max(bx[3], max(ys))
            counts[c[0]] += 1
        done = min(b + BATCH, len(ids))
        print(f"  downloaded {done}/{len(ids)}")
        time.sleep(0.5)

    # Write the chunks and an index of where each one is
    if os.path.isdir(OUT):
        for name in os.listdir(OUT):
            if name.endswith(".json"):
                os.remove(os.path.join(OUT, name))
    os.makedirs(OUT, exist_ok=True)
    index, total, biggest = {}, 0, ("", 0)
    for key, ch in chunks.items():
        text = json.dumps({"f": ch["f"]}, separators=(",", ":"))
        with open(os.path.join(OUT, f"c_{key}.json"), "w") as fh:
            fh.write(text)
        size = len(text)
        total += size
        if size > biggest[1]:
            biggest = (key, size)
        index[key] = [round(v, 4) for v in ch["box"]] + [len(ch["f"]), size]
    meta = {"built": datetime.now(timezone.utc).isoformat(), "source": "U.S. Fish and Wildlife Service National Wetlands Inventory",
            "counts": {"marsh": counts["m"], "swamp": counts["s"], "weedy": counts["w"]}, "chunks": index}
    with open(os.path.join(OUT, "index.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))

    mb = lambda n: f"{n / 1e6:.1f} MB"
    print("Done in", round((time.time() - started) / 60, 1), "minutes")
    print(f"Wetlands saved: {counts['m']} marsh, {counts['s']} swamp, {counts['w']} weedy pond/shallows")
    print(f"Chunks: {len(chunks)}; total {mb(total)}; average {mb(total / max(1, len(chunks)))}; "
          f"largest {mb(biggest[1])} (chunk {biggest[0]})")


if __name__ == "__main__":
    main()
