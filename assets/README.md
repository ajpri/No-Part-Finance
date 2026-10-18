# PWA icon artwork

Dark green (`#123D2C`) with white bold sans-serif lettering (DejaVu Sans).
Small icons use `NPF`. Large icons use `No Part` above a larger `Finance`.
All backgrounds are opaque and edge-to-edge, without baked-in rounded corners.

| Export | Dimensions | Source |
| --- | --- | --- |
| `icon-192.png` | 192 × 192 | `icon-npf.svg` |
| `icon-512.png` | 512 × 512 | `icon-full.svg` |
| `icon-maskable-512.png` | 512 × 512 | `icon-maskable.svg` |
| `apple-touch-icon.png` | 180 × 180 | `icon-npf.svg` |

The editable SVG sources use a 1024 × 1024 canvas. The maskable variant
uses smaller lettering to keep essential artwork inside the centered
80%-diameter safe circle. Only the four PNG exports are served publicly
and included as icon artwork in portable packages.

Export with ImageMagick and DejaVu Sans installed:

```sh
convert -background none assets/icon-npf.svg -resize 192x192 -alpha off PNG24:assets/icon-192.png
convert -background none assets/icon-full.svg -resize 512x512 -alpha off PNG24:assets/icon-512.png
convert -background none assets/icon-maskable.svg -resize 512x512 -alpha off PNG24:assets/icon-maskable-512.png
convert -background none assets/icon-npf.svg -resize 180x180 -alpha off PNG24:assets/apple-touch-icon.png
```

Artwork is rendered directly from vector typography; no image-generation model
or prompt is used. The user confirmed successful iOS and browser installation on October 18, 2026;
browser and operating-system versions were not recorded.
