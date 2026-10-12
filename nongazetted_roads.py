#!/usr/bin/env python3
"""
nongazetted-roads-map: non-gazetted roads of Australia on Google Maps
=====================================================================

What it does
  1. Downloads the OpenStreetMap extract for Australia (Geofabrik, ~1 GB, cached).
  2. Clips it to each area you ask for (a state, a preset region, or a custom box).
  3. Pulls out the ways most likely to be non-gazetted (unofficial / unformed):
       - highway=track            (station tracks, fire trails, access tracks)
       - 4wd_only=yes             (anything marked 4WD-only)
       - highway=unclassified with no route number (optional, --include-unclassified)
     Anything carrying a route number (ref=...) is dropped, as numbered roads are
     almost always gazetted.
  4. Classifies each one (4wd / track / unclassified) and flags access
     restrictions (private / permit / no).
  5. Builds one overlay per area, so each state is its own tile set:
       output/tiles/<area>/...      vector tiles (states, big areas)
       output/data/<area>.geojson   plain GeoJSON (small regions)
       output/kml/<area>/*.kml      optional, for Google My Maps
     output/areas.json lists everything built; output/index.html shows them all
     on Google Maps with a checkbox per area. Build areas one at a time or
     together; each new build is added to the list.
  6. `serve` starts a local web server and opens the map. The output folder can
     also be published as-is to GitHub Pages or any static host.

Important caveat
  "Gazetted" is a legal status recorded by each state/territory, not something
  OpenStreetMap stores. This script uses OSM tags as a proxy. For legal
  certainty about a specific road, check the relevant state road register or
  local council. Areas are clipped by bounding box, so neighbouring states
  overlap slightly at their borders.

Requirements
  Python 3.9+
  osmium-tool  (macOS: brew install osmium-tool | Ubuntu: sudo apt install osmium-tool)
  tippecanoe   (for tile sets; macOS: brew install tippecanoe)
  A Google Maps JavaScript API key (env GOOGLE_MAPS_API_KEY or --api-key)

Usage
  python3 nongazetted_roads.py build --region states            # every state, separately
  python3 nongazetted_roads.py build --region wa nt             # just these two
  python3 nongazetted_roads.py build --region kimberley --kml   # a preset region
  python3 nongazetted_roads.py build --bbox 141,-29,154,-10 --name se-qld
  python3 nongazetted_roads.py serve                            # open the map

Road data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import argparse
import functools
import html
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

PBF_URL = "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"

STATES = ["wa", "nt", "sa", "qld", "nsw", "vic", "tas"]

# Approximate bounding boxes (min_lon, min_lat, max_lon, max_lat), with labels.
REGIONS = {
    "australia": ((112.5, -44.0, 154.0, -9.0), "Australia"),
    "wa": ((112.9, -35.2, 129.0, -13.6), "Western Australia"),
    "nt": ((129.0, -26.0, 138.0, -10.9), "Northern Territory"),
    "sa": ((129.0, -38.1, 141.0, -26.0), "South Australia"),
    "qld": ((137.9, -29.2, 153.6, -10.0), "Queensland"),
    "nsw": ((140.9, -37.6, 153.7, -28.1), "New South Wales & ACT"),
    "vic": ((140.9, -39.2, 150.0, -33.9), "Victoria"),
    "tas": ((143.8, -43.7, 148.5, -39.5), "Tasmania"),
    "kimberley": ((121.5, -19.5, 129.0, -13.6), "Kimberley"),
    "pilbara": ((115.0, -24.0, 121.5, -19.5), "Pilbara"),
    "top-end": ((129.0, -16.5, 137.0, -10.9), "Top End"),
    "cape-york": ((141.0, -16.5, 145.5, -10.0), "Cape York"),
    "high-country": ((145.5, -38.0, 149.0, -36.0), "Victorian High Country"),
    "flinders-ranges": ((137.5, -32.5, 140.0, -29.5), "Flinders Ranges"),
}

CATEGORY_LABELS = {
    "4wd": "4WD-only / rough track",
    "track": "Unformed track",
    "unclassified": "Unnumbered minor road",
}
CATEGORY_COLORS = {  # RGB
    "4wd": (214, 40, 40),
    "track": (247, 127, 0),
    "unclassified": (0, 150, 199),
}
RESTRICTED_ACCESS = {"private", "no", "permit"}
KEEP_TAGS = ("name", "highway", "tracktype", "surface", "access", "4wd_only",
             "seasonal", "smoothness", "motor_vehicle")

# Above this many features an area is built as tiles instead of plain GeoJSON.
GEOJSON_FEATURE_LIMIT = 50_000
KML_FEATURES_PER_FILE = 2_000  # Google My Maps caps each imported layer at about this


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def run(cmd: list) -> None:
    cmd = [str(c) for c in cmd]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def need(tool: str, hint: str) -> None:
    if shutil.which(tool) is None:
        sys.exit(f"Missing required tool '{tool}'. Install it with: {hint}")


def download(url: str, dest: Path, refresh: bool = False) -> None:
    if dest.exists() and not refresh:
        print(f"Using cached {dest} ({dest.stat().st_size / 1e6:,.0f} MB)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB "
                      f"({100 * done / total:.0f}%)", end="", flush=True)
    print()
    tmp.replace(dest)


def parse_bbox(text: str) -> tuple:
    parts = [float(x) for x in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be min_lon,min_lat,max_lon,max_lat")
    min_lon, min_lat, max_lon, max_lat = parts
    if not (min_lon < max_lon and min_lat < max_lat):
        raise argparse.ArgumentTypeError("bbox min values must be less than max values")
    return tuple(parts)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not s:
        raise argparse.ArgumentTypeError("name must contain letters or numbers")
    return s


def dir_size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e6


# ----------------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------------

def classify(tags: dict) -> str | None:
    """Return a category for a way, or None if it is probably gazetted."""
    hw = tags.get("highway", "")
    if tags.get("ref") and hw != "track":
        return None  # numbered routes are almost always gazetted
    four_wd = tags.get("4wd_only") == "yes"
    rough = tags.get("tracktype") in ("grade4", "grade5")
    if hw == "track":
        return "4wd" if (four_wd or rough) else "track"
    if four_wd:
        return "4wd"
    if hw == "unclassified":
        return "unclassified"
    return None


def postprocess(raw_seq: Path, out_seq: Path, exclude_restricted: bool,
                include_unclassified: bool) -> dict:
    """Classify osmium's GeoJSONSeq output into out_seq; return counts."""
    counts = {k: 0 for k in CATEGORY_LABELS}
    counts["restricted"] = 0
    with open(raw_seq, encoding="utf-8") as fin, open(out_seq, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            geom = feat.get("geometry") or {}
            if geom.get("type") not in ("LineString", "MultiLineString"):
                continue
            tags = feat.get("properties") or {}
            cat = classify(tags)
            if cat is None or (cat == "unclassified" and not include_unclassified):
                continue
            access = tags.get("access") or tags.get("motor_vehicle") or ""
            restricted = access in RESTRICTED_ACCESS
            if restricted and exclude_restricted:
                continue
            props = {k: tags[k] for k in KEEP_TAGS if k in tags}
            props["category"] = cat
            props["restricted"] = restricted
            osm_id = tags.get("@id") or feat.get("id")
            if osm_id is not None:
                props["osm_id"] = str(osm_id).lstrip("w")
            fout.write(json.dumps({"type": "Feature", "geometry": geom,
                                   "properties": props}, separators=(",", ":")) + "\n")
            counts[cat] += 1
            counts["restricted"] += restricted
    counts["total"] = sum(counts[k] for k in CATEGORY_LABELS)
    return counts


def write_feature_collection(seq: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(seq, encoding="utf-8") as fin, open(dest, "w", encoding="utf-8") as fout:
        fout.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for line in fin:
            line = line.strip()
            if not line:
                continue
            fout.write(("" if first else ",\n") + line)
            first = False
        fout.write("\n]}\n")


# ----------------------------------------------------------------------------
# KML (for Google My Maps / Google Earth)
# ----------------------------------------------------------------------------

def _kml_color(rgb: tuple, alpha: int = 230) -> str:
    r, g, b = rgb
    return f"{alpha:02x}{b:02x}{g:02x}{r:02x}"  # KML is aabbggrr


def _kml_lines(geom: dict) -> list:
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    return list(geom["coordinates"])


def write_kml(seq: Path, kml_dir: Path, area_id: str) -> list:
    if kml_dir.exists():
        shutil.rmtree(kml_dir)
    kml_dir.mkdir(parents=True)
    styles = "".join(
        f'<Style id="{c}"><LineStyle><color>{_kml_color(rgb)}</color>'
        f"<width>3</width></LineStyle></Style>"
        for c, rgb in CATEGORY_COLORS.items())
    files, buf, part = [], [], 0

    def flush():
        nonlocal buf, part
        if not buf:
            return
        part += 1
        path = kml_dir / f"{area_id}_{part:03d}.kml"
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>Non-gazetted roads {area_id} part {part}</name>{styles}"
            + "".join(buf) + "</Document></kml>\n", encoding="utf-8")
        files.append(path)
        buf = []

    with open(seq, encoding="utf-8") as fin:
        for line in fin:
            if not line.strip():
                continue
            f = json.loads(line)
            p = f["properties"]
            name = html.escape(p.get("name") or CATEGORY_LABELS[p["category"]])
            desc = html.escape("; ".join(f"{k}={v}" for k, v in p.items() if k != "name"))
            lines = _kml_lines(f["geometry"])
            geoms = "".join(
                "<LineString><tessellate>1</tessellate><coordinates>"
                + " ".join(f"{x:.6f},{y:.6f}" for x, y, *_ in coords)
                + "</coordinates></LineString>"
                for coords in lines)
            if len(lines) > 1:
                geoms = f"<MultiGeometry>{geoms}</MultiGeometry>"
            buf.append(f"<Placemark><name>{name}</name><description>{desc}</description>"
                       f"<styleUrl>#{p['category']}</styleUrl>{geoms}</Placemark>")
            if len(buf) >= KML_FEATURES_PER_FILE:
                flush()
    flush()
    return files


# ----------------------------------------------------------------------------
# HTML map (static; reads areas.json at runtime, works under any sub-path)
# ----------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Australian Tracks Map</title>
<style>
  html, body, #map { height: 100%; margin: 0; }
  body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; }
  #panel { position: absolute; top: 12px; left: 12px; z-index: 5; background: #fff;
           padding: 12px 14px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,.25);
           font-size: 13px; max-width: 270px; max-height: calc(100% - 48px); overflow: auto; }
  #panel h1 { font-size: 15px; margin: 0 0 8px; }
  #panel h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
              color: #666; margin: 10px 0 4px; }
  #panel label { display: flex; align-items: center; gap: 8px; margin: 3px 0; cursor: pointer; }
  .sw { width: 22px; height: 4px; border-radius: 2px; display: inline-block; flex: none; }
  .n { color: #777; margin-left: auto; font-variant-numeric: tabular-nums; }
  #panel small { display: block; color: #555; margin-top: 10px; line-height: 1.35; }
  #toggle { display: none; }
  @media (max-width: 480px) {
    #panel { left: 8px; right: 8px; max-width: none; max-height: 45%; }
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <h1>Non-gazetted roads</h1>
  <h2>Areas</h2><div id="areas">Loading…</div>
  <h2>Road types</h2><div id="legend"></div>
  <label><input type="checkbox" id="restrictedOnly"> Only private / permit tracks</label>
  <small>Derived from OpenStreetMap tags; check state road registers for legal
  status. Road data &copy; OpenStreetMap contributors.</small>
</div>
<script src="https://unpkg.com/deck.gl@9.1.14/dist.min.js"></script>
<script>
const BASE = location.href.replace(/[#?].*$/, "").replace(/[^/]*$/, "");
const LABELS = __LABELS__;
const COLORS = __COLORS__;
const catOn = Object.fromEntries(Object.keys(LABELS).map(k => [k, true]));
const areaOn = {};
const geojsonLoaded = {};
let AREAS = [], restrictedOnly = false, map, info, overlay;

function shown(p, area) {
  return areaOn[area] && catOn[p.category] &&
    (!restrictedOnly || p.restricted === true || p.restricted === "true");
}
function esc(v) {
  return String(v).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}
function infoHtml(p) {
  const rows = Object.entries(p).filter(([k]) => !["category", "area"].includes(k))
    .map(([k, v]) => `<tr><td style="color:#666;padding-right:8px">${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  const osm = p.osm_id ? `<a href="https://www.openstreetmap.org/way/${esc(p.osm_id)}" target="_blank" rel="noopener">View on OSM</a>` : "";
  return `<b>${esc(p.name || LABELS[p.category])}</b><br><i>${LABELS[p.category]}</i>
          <table style="font-size:12px;margin-top:4px">${rows}</table>${osm}`;
}
function openInfo(p, latLng) { info.setContent(infoHtml(p)); info.setPosition(latLng); info.open(map); }

function deckLayers() {
  return AREAS.filter(a => a.mode === "tiles" && areaOn[a.id]).map(a => new deck.MVTLayer({
    id: "roads-" + a.id,
    data: BASE + "tiles/" + a.id + "/{z}/{x}/{y}.pbf",
    extent: a.bbox, minZoom: 0, maxZoom: a.maxzoom,
    binary: false, pickable: true,
    getLineColor: f => shown(f.properties, a.id) ? [...COLORS[f.properties.category], 230] : [0, 0, 0, 0],
    getLineWidth: 2, lineWidthUnits: "pixels", lineWidthMinPixels: 1,
    updateTriggers: { getLineColor: [JSON.stringify(catOn), restrictedOnly] },
    onClick: ({ object, coordinate }) => {
      if (object && shown(object.properties, a.id))
        openInfo(object.properties, { lat: coordinate[1], lng: coordinate[0] });
    },
  }));
}
function dataStyle(f) {
  const p = { category: f.getProperty("category"), restricted: f.getProperty("restricted") };
  const [r, g, b] = COLORS[p.category] || [0, 0, 0];
  return { visible: shown(p, f.getProperty("area")), strokeColor: `rgb(${r},${g},${b})`,
           strokeWeight: 2.5, strokeOpacity: 0.9 };
}
function redraw() {
  for (const a of AREAS) {
    if (a.mode === "geojson" && areaOn[a.id] && !geojsonLoaded[a.id]) {
      geojsonLoaded[a.id] = true;
      map.data.loadGeoJson(BASE + "data/" + a.id + ".geojson", null,
        feats => { feats.forEach(f => f.setProperty("area", a.id)); map.data.setStyle(dataStyle); });
    }
  }
  map.data.setStyle(dataStyle);
  overlay.setProps({ layers: deckLayers() });
  updateCounts();
}
function updateCounts() {
  for (const k of Object.keys(LABELS)) {
    const n = AREAS.filter(a => areaOn[a.id]).reduce((s, a) => s + (a.counts[k] || 0), 0);
    const el = document.getElementById("n-" + k);
    if (el) el.textContent = n.toLocaleString();
  }
}
function buildPanel() {
  const areasEl = document.getElementById("areas");
  areasEl.innerHTML = AREAS.length ? "" : "Nothing built yet.";
  for (const a of AREAS) {
    const row = document.createElement("label");
    row.innerHTML = `<input type="checkbox" checked> ${esc(a.label)}
      <span class="n">${a.counts.total.toLocaleString()}</span>`;
    row.querySelector("input").onchange = e => { areaOn[a.id] = e.target.checked; redraw(); };
    areasEl.appendChild(row);
  }
  const legend = document.getElementById("legend");
  for (const [k, label] of Object.entries(LABELS)) {
    if (!AREAS.some(a => a.counts[k])) continue;
    const [r, g, b] = COLORS[k];
    const row = document.createElement("label");
    row.innerHTML = `<input type="checkbox" checked>
      <span class="sw" style="background:rgb(${r},${g},${b})"></span> ${label}
      <span class="n" id="n-${k}"></span>`;
    row.querySelector("input").onchange = e => { catOn[k] = e.target.checked; redraw(); };
    legend.appendChild(row);
  }
  document.getElementById("restrictedOnly").onchange = e => { restrictedOnly = e.target.checked; redraw(); };
}

async function initMap() {
  map = new google.maps.Map(document.getElementById("map"), {
    center: { lat: -25.5, lng: 134 }, zoom: 4, mapTypeId: "hybrid",
    mapTypeControlOptions: { position: google.maps.ControlPosition.TOP_RIGHT },
  });
  info = new google.maps.InfoWindow();
  overlay = new deck.GoogleMapsOverlay({ layers: [] });
  overlay.setMap(map);
  map.data.addListener("click", e => {
    const p = {}; e.feature.forEachProperty((v, k) => (p[k] = v)); openInfo(p, e.latLng);
  });
  try {
    AREAS = await (await fetch(BASE + "areas.json", { cache: "no-cache" })).json();
  } catch (err) { AREAS = []; }
  AREAS.forEach(a => (areaOn[a.id] = true));
  buildPanel();
  if (AREAS.length) {
    const b = new google.maps.LatLngBounds();
    AREAS.forEach(a => { b.extend({ lat: a.bbox[1], lng: a.bbox[0] }); b.extend({ lat: a.bbox[3], lng: a.bbox[2] }); });
    map.fitBounds(b);
  }
  redraw();
}
window.initMap = initMap;
</script>
<script async src="https://maps.googleapis.com/maps/api/js?key=__API_KEY__&callback=initMap"></script>
</body>
</html>
"""


def write_html(out_dir: Path, api_key: str) -> Path:
    page = (HTML_TEMPLATE
            .replace("__LABELS__", json.dumps(CATEGORY_LABELS))
            .replace("__COLORS__", json.dumps({k: list(v) for k, v in CATEGORY_COLORS.items()}))
            .replace("__API_KEY__", api_key or "YOUR_GOOGLE_MAPS_API_KEY"))
    dest = out_dir / "index.html"
    dest.write_text(page, encoding="utf-8")
    return dest


def update_manifest(out_dir: Path, entry: dict) -> list:
    path = out_dir / "areas.json"
    areas = []
    if path.exists():
        try:
            areas = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            areas = []
    areas = [a for a in areas if a.get("id") != entry["id"]] + [entry]
    order = {k: i for i, k in enumerate(REGIONS)}
    areas.sort(key=lambda a: (order.get(a["id"], 999), a["id"]))
    path.write_text(json.dumps(areas, indent=2), encoding="utf-8")
    return areas


# ----------------------------------------------------------------------------
# build one area
# ----------------------------------------------------------------------------

def build_area(a: argparse.Namespace, area_id: str, label: str, bbox: tuple,
               pbf: Path, out_dir: Path) -> dict:
    print(f"\n=== {label} ({area_id}) {bbox} ===")
    work = out_dir / "work" / area_id
    work.mkdir(parents=True, exist_ok=True)

    src = pbf
    if area_id != "australia":
        clipped = work / "clipped.osm.pbf"
        run(["osmium", "extract", "-b", ",".join(map(str, bbox)), src,
             "-o", clipped, "--overwrite"])
        src = clipped

    filtered = work / "filtered.osm.pbf"
    exprs = ["w/highway=track", "w/4wd_only=yes"]
    if a.include_unclassified:
        exprs.append("w/highway=unclassified")
    run(["osmium", "tags-filter", src, *exprs, "-o", filtered, "--overwrite"])

    raw_seq = work / "raw.geojsonseq"
    run(["osmium", "export", filtered, "-f", "geojsonseq", "-o", raw_seq, "--overwrite",
         "--geometry-types=linestring", "--attributes=id",
         "-x", "print_record_separator=false"])

    seq = work / "roads.geojsonseq"
    counts = postprocess(raw_seq, seq, a.exclude_restricted, a.include_unclassified)
    print("Segments: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))

    tiles_dir = out_dir / "tiles" / area_id
    geojson_path = out_dir / "data" / f"{area_id}.geojson"
    use_tiles = (a.tiles or area_id in STATES or area_id == "australia"
                 or counts["total"] > GEOJSON_FEATURE_LIMIT)
    if use_tiles:
        need("tippecanoe", "brew install tippecanoe   (or build from github.com/felt/tippecanoe)")
        if tiles_dir.exists():
            shutil.rmtree(tiles_dir)
        run(["tippecanoe", "-e", tiles_dir, "-l", "roads", "-Z", "0", "-z", a.maxzoom,
             "--no-tile-compression", "--drop-densest-as-needed",
             "--extend-zooms-if-still-dropping", "--force", seq])
        geojson_path.unlink(missing_ok=True)
        mode, size = "tiles", dir_size_mb(tiles_dir)
    else:
        write_feature_collection(seq, geojson_path)
        if tiles_dir.exists():
            shutil.rmtree(tiles_dir)
        mode, size = "geojson", dir_size_mb(geojson_path)
    print(f"{label}: {mode}, {size:,.1f} MB")

    if a.kml:
        if counts["total"] > 50 * KML_FEATURES_PER_FILE:
            print("Skipping KML: too many features for Google My Maps; use a smaller region.")
        else:
            files = write_kml(seq, out_dir / "kml" / area_id, area_id)
            print(f"Wrote {len(files)} KML file(s) to {out_dir / 'kml' / area_id}")

    if not a.keep_work:
        shutil.rmtree(work, ignore_errors=True)

    return {"id": area_id, "label": label, "bbox": list(bbox), "mode": mode,
            "maxzoom": a.maxzoom, "size_mb": round(size, 1), "counts": counts}


# ----------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------

def cmd_build(a: argparse.Namespace) -> None:
    need("osmium", "brew install osmium-tool   (or: sudo apt install osmium-tool)")
    out_dir = Path(a.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if a.bbox:
        name = a.name or "custom"
        targets = [(name, name.replace("-", " ").title(), a.bbox)]
    else:
        ids = []
        for r in a.region:
            for i in (STATES if r == "states" else [r]):
                if i not in ids:
                    ids.append(i)
        targets = [(i, REGIONS[i][1], REGIONS[i][0]) for i in ids]

    pbf = Path(a.pbf) if a.pbf else out_dir / "cache" / "australia-latest.osm.pbf"
    if not a.pbf:
        download(PBF_URL, pbf, refresh=a.refresh)

    built = []
    for area_id, label, bbox in targets:
        entry = build_area(a, area_id, label, bbox, pbf, out_dir)
        areas = update_manifest(out_dir, entry)
        built.append(entry)

    api_key = a.api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
    index = out_dir / "index.html"
    if api_key or not index.exists():
        write_html(out_dir, api_key)

    print("\nBuilt:")
    for e in built:
        print(f"  {e['label']:<24} {e['counts']['total']:>9,} segments  "
              f"{e['size_mb']:>8,.1f} MB  ({e['mode']})")
    print(f"Map now lists {len(areas)} area(s): {', '.join(x['id'] for x in areas)}")
    if not api_key and "YOUR_GOOGLE_MAPS_API_KEY" in index.read_text(encoding="utf-8"):
        print("No Google Maps API key set yet; re-run with --api-key or set GOOGLE_MAPS_API_KEY.")
    print(f"Run:  python3 {Path(__file__).name} serve --out {a.out}")


def cmd_remove(a: argparse.Namespace) -> None:
    out_dir = Path(a.out).resolve()
    path = out_dir / "areas.json"
    areas = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    for area_id in a.area:
        shutil.rmtree(out_dir / "tiles" / area_id, ignore_errors=True)
        shutil.rmtree(out_dir / "kml" / area_id, ignore_errors=True)
        (out_dir / "data" / f"{area_id}.geojson").unlink(missing_ok=True)
        areas = [x for x in areas if x.get("id") != area_id]
        print(f"Removed {area_id}")
    path.write_text(json.dumps(areas, indent=2), encoding="utf-8")


def cmd_serve(a: argparse.Namespace) -> None:
    out_dir = Path(a.out).resolve()
    if not (out_dir / "index.html").exists():
        sys.exit(f"No map in {out_dir}; run 'build' first.")

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".pbf": "application/x-protobuf",
                          ".geojson": "application/geo+json"}

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=str(out_dir))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), handler) as httpd:
        url = f"http://localhost:{a.port}/"
        print(f"Serving {out_dir} at {url}  (Ctrl+C to stop)")
        if not a.no_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> None:
    p = argparse.ArgumentParser(description="Map likely non-gazetted roads in Australia on Google Maps.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="download data and build one or more areas")
    area = b.add_mutually_exclusive_group()
    area.add_argument("--region", nargs="+", choices=["states", *REGIONS], default=["states"],
                      metavar="AREA",
                      help="'states' (every state separately, the default), or any of: "
                           + ", ".join(REGIONS))
    area.add_argument("--bbox", type=parse_bbox, help="custom area: min_lon,min_lat,max_lon,max_lat")
    b.add_argument("--name", type=slug, help="name for a --bbox area (e.g. se-qld)")
    b.add_argument("--out", default="output", help="output folder (default: output)")
    b.add_argument("--pbf", help="use an existing .osm.pbf instead of downloading")
    b.add_argument("--refresh", action="store_true", help="re-download the OSM extract")
    b.add_argument("--include-unclassified", action="store_true",
                   help="also include unnumbered highway=unclassified roads (noisier)")
    b.add_argument("--exclude-restricted", action="store_true",
                   help="drop tracks tagged access=private/no/permit")
    b.add_argument("--tiles", action="store_true", help="force tile mode for small regions")
    b.add_argument("--maxzoom", type=int, default=13,
                   help="max tile zoom (default 13; 12 roughly quarters the size, 14 adds detail)")
    b.add_argument("--kml", action="store_true", help="also write KML for Google My Maps")
    b.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    b.add_argument("--api-key", help="Google Maps JS API key (or env GOOGLE_MAPS_API_KEY)")
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("remove", help="remove built areas from the map")
    r.add_argument("area", nargs="+")
    r.add_argument("--out", default="output")
    r.set_defaults(func=cmd_remove)

    s = sub.add_parser("serve", help="serve the map locally and open it")
    s.add_argument("--out", default="output")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args()
    if args.cmd == "build" and args.name and not args.bbox:
        p.error("--name only applies with --bbox")
    args.func(args)


if __name__ == "__main__":
    main()
