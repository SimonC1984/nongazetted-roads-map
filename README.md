# Non-gazetted Roads Map

An interactive Google Maps overlay of Australia's unofficial roads: station tracks, fire trails and 4WD-only routes. Each state is built as its own layer from OpenStreetMap data.

**Live map:** https://simonc1984.github.io/nongazetted-roads-map/

## What's shown

| Colour | Category | Meaning (from OpenStreetMap tags) |
|---|---|---|
| Red | 4WD-only / rough track | `4wd_only=yes`, or a track graded 4 or 5 |
| Orange | Unformed track | `highway=track` |
| Blue | Unnumbered minor road | `highway=unclassified` with no route number (optional) |

Tracks tagged private, permit-only or no access are flagged, and the map can filter to show only those. Click any line for its surface, grade, access and a link to the OpenStreetMap source.

**Caveat:** "Gazetted" is a legal status held in state road registers. OpenStreetMap doesn't record it, so this map uses road tags as a proxy. Treat it as a guide, and check with the state road authority or local council before relying on a specific road. Many tracks cross pastoral leases or Aboriginal land, where a permit is needed.

## Updating the map

The map rebuilds automatically on the 2nd of each month. To rebuild it yourself:

1. Open the **Actions** tab and choose **Build and publish map**.
2. Click **Run workflow**, choose the areas and detail level, then **Run workflow** again.
3. When it finishes (usually 20–60 minutes for all states), the summary shows each area's size and the live map updates.

Areas: `states` (every state), or any of `wa nt sa qld nsw vic tas kimberley pilbara top-end cape-york high-country flinders-ranges`. Each run publishes exactly the areas it built.

## Running it on your own computer

```
brew install osmium-tool tippecanoe
export GOOGLE_MAPS_API_KEY=your_key
python3 nongazetted_roads.py build --region kimberley --kml
python3 nongazetted_roads.py serve
```

Run `python3 nongazetted_roads.py build --help` for all options.

## One-time setup

- **Settings → Pages → Source:** GitHub Actions.
- **Settings → Secrets and variables → Actions:** add a secret named `GOOGLE_MAPS_API_KEY`.
- In Google Cloud, restrict the key to the website `https://simonc1984.github.io/*` (plus `http://localhost:8000/*` for local use).

Road data © OpenStreetMap contributors, available under the [Open Database Licence](https://www.openstreetmap.org/copyright).
