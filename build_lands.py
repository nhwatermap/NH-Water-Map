"""
Builds the map's own copy of New Hampshire's public and publicly accessible conservation land,
from the NH GRANIT "Conservation and Public Lands" dataset (updated by the state about twice a year).

Run by hand from GitHub's Actions tab ("Build map layers" → public lands), a couple of times a year.
Saves small chunk files in the lands/ folder. Uses only Python's built-in tools.

Shows who owns and manages each parcel. It does NOT say whether hunting is allowed:
rules vary by property and change, so the map always points people to the official rules.
"""
import json, os, re, time, urllib.request, urllib.parse
from datetime import datetime, timezone

SERVICE = "https://nhgeodata.unh.edu/nhgeodata/rest/services/EC/Conservation/MapServer"
LAYER_ID = 6                                  # "CL: Solid" — every parcel
BBOX = (-72.60, 42.69, -70.55, 45.31)
CELL = 0.1
SIMPLIFY = 0.00003                            # about 3 m
BATCH = 500
OUT = "lands"

# Restrictions on land that usually stays private and closed to the public (per the GRANIT standard)
PRIVATE_RESTRICTIONS = {"AR", "CE", "DR", "FE", "SA", "SE", "HP", "PE", "RW"}
ACCESS_TEXT = {1: "Public access allowed (minor restrictions may apply)", 2: "Access restricted to certain areas or times",
               3: "Public access not allowed", 4: "Access unknown", 5: "Access unknown"}
KNOWN_AGENCIES = {32000: "NH Fish and Game", 31000: "NH Dept. of Natural and Cultural Resources",
                  22000: "U.S. Forest Service", 21000: "U.S. Fish and Wildlife Service", 21100: "National Park Service",
                  21200: "National Park Service (Appalachian Trail)", 23000: "U.S. Army Corps of Engineers",
                  23100: "U.S. Air Force", 30000: "State of New Hampshire", 20000: "U.S. Government"}
# Parks and recreation areas, matched as whole words ("Parker Brook" is not a park)
PARK_RE = re.compile(r"\b(STATE PARK|PARK|SP|BEACH|RECREATION AREA|REC AREA|CAMPGROUND|DAY USE|PICNIC AREA|HISTORIC SITE|WAYSIDE|SKI AREA)\b")


def call(path, params, tries=4, post=False):
    q = {**params, "f": "json"}
    for i in range(tries):
        try:
            if post:
                req = urllib.request.Request(f"{SERVICE}/{path}", data=urllib.parse.urlencode(q).encode(),
                                             headers={"User-Agent": "nh-water-map"})
            else:
                req = urllib.request.Request(f"{SERVICE}/{path}?" + urllib.parse.urlencode(q), headers={"User-Agent": "nh-water-map"})
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read().decode("utf-8"))
            if "error" in out:
                raise RuntimeError(out["error"].get("message", "server error"))
            return out
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  retrying in {10 * (i + 1)}s after: {e}")
            time.sleep(10 * (i + 1))


def field(attrs, name):
    return next((v for k, v in attrs.items() if k.split(".")[-1].upper() == name), None)


def as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def agency_names():
    """Agency code → name, read from the service's own code list when it has one."""
    names = dict(KNOWN_AGENCIES)
    try:
        meta = call(str(LAYER_ID), {})
        for f in meta.get("fields", []):
            if f.get("name", "").split(".")[-1].upper() == "PPAGENCY" and (f.get("domain") or {}).get("codedValues"):
                for cv in f["domain"]["codedValues"]:
                    names.setdefault(as_int(cv.get("code")), str(cv.get("name")).strip())
    except Exception as e:
        print(f"  (couldn't read agency names from the service: {e})")
    return names


def category(attrs):
    """Which kind of land this is, or None to leave it off the map."""
    pptype = str(field(attrs, "PPTYPE") or "").upper().strip()
    agency = as_int(field(attrs, "PPAGENCY"))
    agtype = as_int(field(attrs, "PPAGENTYPE"))
    owner = as_int(field(attrs, "OWNERTYPE"))
    access = as_int(field(attrs, "ACCESS"))
    name = f" {str(field(attrs, 'NAME') or '')} {str(field(attrs, 'P_NAME') or '')} ".upper()
    if pptype in PRIVATE_RESTRICTIONS and owner not in (1, 2, 3, 4, 6):
        return None  # easement on private land (even one held by an agency): not open to the public
    if agency == 32000:
        return "wma"
    public = (agency // 1000 in (20, 21, 22, 23) or 30000 <= agency < 40000 or 1000 <= agency < 20000
              or agtype in (1, 2, 3, 4) or owner in (1, 2, 3, 4, 6))
    # Any public park or recreation area, whoever manages it (e.g. a state park on Army Corps land)
    if public and PARK_RE.search(name):
        return "park"
    if 31000 <= agency < 32000:
        return "forest"
    if agency // 1000 in (20, 21, 22, 23) or agtype == 2 or owner == 2:
        return "federal"
    if agtype == 3 or owner == 3:
        return "forest"
    if agtype in (1, 4) or owner in (1, 4, 6):
        return "town"
    if owner == 5 and access in (1, 2) and pptype in ("FO", "LE", "RV", ""):
        return "trust"
    return None


def pack_ring(ring):
    out, px, py = [], 0, 0
    for x, y in ring:
        ix, iy = round(x * 1e5), round(y * 1e5)
        if out and ix == px and iy == py:
            continue
        out += [ix - px, iy - py]
        px, py = ix, iy
    return out if len(out) >= 6 else None


def main():
    started = time.time()
    agencies = agency_names()
    print("Asking the server how many parcels there are…")
    expected = call(f"{LAYER_ID}/query", {"where": "1=1", "returnCountOnly": "true"}).get("count")
    ids = sorted(call(f"{LAYER_ID}/query", {"where": "1=1", "returnIdsOnly": "true"}).get("objectIds") or [])
    print(f"Server count: {expected}; found {len(ids)} parcel IDs")

    chunks, counts, received, skipped, not_drawable = {}, {}, set(), 0, 0

    def fetch(part):
        out = call(f"{LAYER_ID}/query", {"objectIds": ",".join(map(str, part)), "outFields": "*", "returnGeometry": "true",
                                         "outSR": 4326, "maxAllowableOffset": SIMPLIFY, "geometryPrecision": 5}, post=True)
        idf = out.get("objectIdFieldName")
        feats = out.get("features", [])
        for f in feats:
            a = f.get("attributes") or {}
            oid = a.get(idf) if idf in a else field(a, "OBJECTID")
            if oid is not None:
                received.add(oid)
        return feats

    def batches():
        for b in range(0, len(ids), BATCH):
            yield ids[b:b + BATCH]
        for attempt in (1, 2):
            missing = [i for i in ids if i not in received]
            if not missing:
                return
            print(f"  retrying {len(missing)} that didn't come back (attempt {attempt})")
            for b in range(0, len(missing), 100):
                yield missing[b:b + 100]

    for part in batches():
        for f in fetch(part):
            a = f.get("attributes") or {}
            cat = category(a)
            if not cat:
                skipped += 1
                continue
            rings = (f.get("geometry") or {}).get("rings") or []
            packed = [p for p in (pack_ring(r) for r in rings) if p]
            if not packed:
                not_drawable += 1
                continue
            name = str(field(a, "NAME") or "").strip()
            pname = str(field(a, "P_NAME") or "").strip()
            acres = field(a, "CSIZE") or field(a, "RSIZE")
            try:
                acres = round(float(acres)) if acres is not None and float(acres) > 0 else None
            except (TypeError, ValueError):
                acres = None
            agency = as_int(field(a, "PPAGENCY"))
            access = as_int(field(a, "ACCESS"))
            rec = [cat, name or pname or "Unnamed parcel", pname if pname and pname != name else "",
                   acres, agencies.get(agency, ""), access]
            xs = [x for r in rings for x, _ in r]
            ys = [y for r in rings for _, y in r]
            key = f"{int(((min(xs) + max(xs)) / 2 - BBOX[0]) // CELL)}_{int(((min(ys) + max(ys)) / 2 - BBOX[1]) // CELL)}"
            ch = chunks.setdefault(key, {"f": [], "box": [180, 90, -180, -90]})
            ch["f"].append(rec + [packed])
            bx = ch["box"]
            bx[0], bx[1], bx[2], bx[3] = min(bx[0], min(xs)), min(bx[1], min(ys)), max(bx[2], max(xs)), max(bx[3], max(ys))
            counts[cat] = counts.get(cat, 0) + 1
        print(f"  downloaded {len(received)}/{len(ids)}")
        time.sleep(0.5)

    if os.path.isdir(OUT):
        for n in os.listdir(OUT):
            if n.endswith(".json"):
                os.remove(os.path.join(OUT, n))
    os.makedirs(OUT, exist_ok=True)
    index, total, biggest = {}, 0, ("", 0)
    for key, ch in chunks.items():
        text = json.dumps({"f": ch["f"]}, separators=(",", ":"))
        with open(os.path.join(OUT, f"c_{key}.json"), "w") as fh:
            fh.write(text)
        total += len(text)
        biggest = max(biggest, (key, len(text)), key=lambda t: t[1])
        index[key] = [round(v, 4) for v in ch["box"]] + [len(ch["f"]), len(text)]
    missing = len([i for i in ids if i not in received])
    saved = sum(counts.values())
    meta = {"built": datetime.now(timezone.utc).isoformat(), "source": "NH GRANIT Conservation and Public Lands",
            "counts": counts, "access": ACCESS_TEXT,
            "check": {"serverCount": expected, "found": len(ids), "downloaded": len(received), "saved": saved,
                      "leftOff": skipped, "notDrawable": not_drawable, "missing": missing},
            "chunks": index}
    with open(os.path.join(OUT, "index.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))

    labels = {"wma": "Fish and Game (WMA)", "forest": "state forest", "park": "park/recreation area", "federal": "federal",
              "town": "town/county", "trust": "land trust (open)"}
    print("Done in", round((time.time() - started) / 60, 1), "minutes")
    print("Parcels saved: " + ", ".join(f"{counts.get(k, 0)} {v}" for k, v in labels.items()))
    print(f"Chunks: {len(chunks)}; total {total / 1e6:.1f} MB; largest {biggest[1] / 1e6:.2f} MB (chunk {biggest[0]})")
    problems = []
    if expected is not None and len(ids) < expected * 0.995:
        problems.append(f"found {expected - len(ids)} fewer than the server's count")
    if missing > len(ids) * 0.005:
        problems.append(f"{missing} never came back from the server")
    verdict = "Everything accounted for ✔" if not problems else "PROBLEM: " + "; ".join(problems)
    print(f"Check: server count {expected} · found {len(ids)} · downloaded {len(received)} · saved {saved} · "
          f"left off (private/closed) {skipped} · not drawable {not_drawable} · missing {missing} → {verdict}")
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
