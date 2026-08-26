# Layout specification

## Canvas and safe areas

- Canvas: 1080x1920, 30 fps.
- Horizontal safe margin: 48-64 px.
- Keep essential text above y=1740 and below y=70 to avoid common platform overlays.
- Use `box-sizing: border-box` and reset browser margins.

## Default three-zone layout

| Zone | Approximate vertical range | Purpose |
| --- | --- | --- |
| Top | 5%-29% | Context label, hook, number, comparison |
| Middle | 29%-64% | Real footage that proves the social or business context |
| Bottom | 64%-94% | Decision line, audience qualifier, CTA |

For native vertical footage, allow the picture to fill the canvas and place text over protected top and bottom gradients. For landscape footage:

- Fill the canvas with a blurred, darkened duplicate of the active shot.
- Place the uncropped source in a full-width 16:9 strip around the middle zone.
- Add soft dark gradients above and below the strip so text stays independent of footage brightness.
- Do not zoom until faces or groups are cut off merely to fill 9:16.

## Typography

- Preferred family: Microsoft YaHei or a locally bundled, legally usable bold Chinese font.
- Context label: 52-64 px.
- Normal line: 66-84 px.
- Hero statistic: 104-150 px.
- CTA: 74-104 px.
- Emphasis keywords or figures: 1.18-1.35 times the surrounding line size; limit to 1-3 exact terms per video.
- Weight: 800-900.
- Outline: 6-9 px black stroke at 1080 width, supplemented by a compact shadow.
- Palette: white for connective text, yellow for primary value, red for comparison or urgency.

Use color to express hierarchy, not to color every word. One primary highlight and one secondary alert color are enough.

## Motion

- Reveal the overall hierarchy within 0.6 seconds.
- Use 8-16 px rise, 0.96-1.04 scale, or opacity for entrances.
- Stagger major lines by 80-140 ms.
- Keep persistent micro-motion on footage only: a 1%-3% push-in or slow drift.
- Avoid looping bounce, large rotation, excessive glow, and word-by-word subtitle motion.

## Shot changes

- Frame zero must already display image A at full intended brightness; do not use a fade from black or wait for video decoding before the first visible picture.

- Typical 8-second cut points: 0.0, 1.5, 3.1, 4.7, 6.3, 8.2 seconds.
- Hard cuts are the default. A 4-8 frame opacity crossfade is acceptable when footage exposure changes sharply.
- Maintain text position through cuts so reading is not reset.
