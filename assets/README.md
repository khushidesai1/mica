# Mica logo

`mica-logo.svg` is the canonical artwork. `mica-logo.png` is its 512×512 raster
export for Slack and other surfaces that require PNG. Preserve the supplied square
canvas, pale yellow background, purple colors, and proportions.

The repository README uses the SVG. Upload the PNG under **Basic Information →
Display Information → App icon** for the existing Mica Slack app (`A0C417ELV1N`).
The installed bot shares this app icon; verify its profile after saving.

The headless API does not serve a logo or favicon. The generic animated status
spinner in `deploy/assets/` is independent of the logo. Historical UI screenshots
and deployment source manifests are records of earlier releases.

To reproduce the PNG on macOS:

```bash
mkdir -p /tmp/mica-logo
qlmanage -t -s 512 -o /tmp/mica-logo assets/mica-logo.svg
cp /tmp/mica-logo/mica-logo.svg.png assets/mica-logo.png
```
