---
name: private-domain-short-video
description: Create concise 7-10 second vertical Chinese private-domain conversion videos from user copy and an approved material library, including the Huangque test-server library. Use for same-city networking, community recruitment, industry opportunity, female-growth, business-circle, health, beauty, or similar lead-generation clips that need bold fixed typography, semantically matched mixed image-and-video material, music, and fast rendering. Do not use for long-form explainers, narration-led talking heads, or automatic publishing.
---

# Private-Domain Short Video

Turn Chinese conversion copy plus local footage into a finished, reviewable vertical video. Preserve the user's claim and intent while making the hierarchy readable within one glance.

## Required companion skills

Use `hyperframes:hyperframes`, `hyperframes:hyperframes-cli`, and `hyperframes:gsap` for implementation, animation, rendering, and validation. Read those skills before taking HyperFrames actions.

## Inputs

Collect or infer these inputs without blocking on nonessential choices:

- Copy: the user's exact hook, evidence/comparison, conclusion, and CTA.
- Material scope: explicit files, a supplied material-library directory, files the user has placed in scope, or the default Huangque test-server library.
- Reference style: supplied videos or this skill's default layout.
- Delivery: default to a reviewable MP4; never publish automatically.

When the user supplies no footage, query the Huangque test-server library at `/home/ubuntu/material-libraries/huangque-media/` before asking for another source. Use [test-server-material-library.md](references/test-server-material-library.md) for access and staging. Do not silently substitute web stock footage.

## Workflow

1. Resolve material scope. For the default test-server library, list candidates and stage only the selected visual files and BGM with `scripts/test_server_materials.py`; never edit the server library. BGM must come from `/home/ubuntu/material-libraries/huangque-media/` and be chosen randomly from the audio files in that library. Then inspect source media with `scripts/inspect_media.py` or `ffprobe`. Record duration, dimensions, frame rate, audio presence, and orientation.
2. Sample representative frames from reference and candidate footage. Select clips and still images by narrative function, not merely visual similarity.
3. Decompose every copy independently into four visual needs: problem, comparison, judgment, and CTA. Use [copy-decomposition.md](references/copy-decomposition.md). Preserve user-provided numbers and claims verbatim unless the user asks for fact-checking or rewriting. Extract 1-3 exact emphasis keywords or numbers from the copy and render them at 1.18-1.35 times the surrounding text size.
4. Write `DESIGN.md` before composition code. Define visual hierarchy, timing, selected files, music direction, and at least one rejected alternative.
5. Build a 1080x1920, 30 fps HyperFrames composition using [layout-spec.md](references/layout-spec.md). Start with a still image visible from frame zero; do not start with a fade from black, an empty background, a delayed media load, or an undecoded video frame.
6. Add only subtle entrance motion. Keep the main copy fixed and readable after the first 0.6 seconds; this format is not word-by-word karaoke captions.
7. Randomly select BGM only from the fixed Huangque test-server material library, following [music-and-editing.md](references/music-and-editing.md). Do not generate substitute music and do not silently use music from another local or web source. If the library has no usable authorized BGM, stop before rendering and report the shortage. Mute source speech unless the user explicitly wants it retained.
8. Render once with `hyperframes render`; do not run the pre-render full `inspect` or contact-sheet visual-QA pass by default. If rendering fails, fix the reported error and retry. If the bundled browser is unavailable, locate installed Chrome or Edge and set the documented browser-path environment variable for the command.
9. Run only a fast post-render technical check with `ffprobe`: confirm the MP4 opens, has the expected duration, resolution, H.264 video, and AAC audio. Deliver the playable local file plus a compact statement of those results and any unverified user claims.

## Batch material uniqueness

Apply these rules whenever generating more than one video in a batch:

- Build an independent four-beat material bundle for every copy. Changing only text, order, crop, start time, or playback speed does not make a reused bundle independent.
- Match visuals to the meaning of that specific copy. Use customer communication for relationship and trust, service activity for continuing value, acquisition or platform scenes for traffic and lead generation, health/lifestyle scenes for health demand, and age-appropriate people for age-group claims.
- Default each video to three distinct visual assets: two still images and one video clip. Map image A to the problem, the video to the comparison and judgment, and image B to the CTA; another three-asset mapping is allowed when it matches the copy better.
- Do not reuse a visual asset in another video in the same batch unless the user explicitly approves reuse. Shuffle the test-library BGM pool once and assign tracks without replacement while enough tracks remain; do not repeat a track in the same batch when the library can supply unique tracks.
- For a 20-video batch, target at least 60 unique visual assets. If the approved library cannot supply enough semantically relevant material, stop before rendering and report the shortfall instead of silently recycling assets.
- Write a batch material map before rendering. Record video number, copy beat, source-relative path, media type, and SHA-256. Reject duplicate source paths or duplicate content hashes across videos unless reuse was explicitly approved.

## Material selection order

For each copy beat, prefer:

1. Direct proof: meetings, workshops, conversations, product/service activity, or real group interaction.
2. Emotional reinforcement: confident people, attentive listening, cooperation, movement, or achievement.
3. Atmosphere: city, venue, decor, food, travel, or luxury details.

Avoid repetitive shots that all serve the same function. For an 8-second video, use three distinct assets by default: image, video, image. The single video may carry two adjacent copy beats through timed text changes without being duplicated as another source asset.

## Default editorial decisions

- Duration: 7.2-9.0 seconds; allow up to 10.5 seconds for unusually dense copy.
- Structure: hook/statistic at top, real-world proof in the middle, conclusion and CTA at bottom.
- Typography: bold white/yellow/red Chinese text with a strong black stroke or shadow.
- Keyword emphasis: enlarge 1-3 exact keywords, figures, or CTA terms to roughly 1.18-1.35 times the surrounding text; keep the remaining copy stable and readable.
- Pacing: hard cuts or one short crossfade; avoid ornamental transitions.
- Audio: upbeat instrumental, no vocals competing with reading, target -14 to -12 LUFS and true peak at or below -1 dBTP.
- Claims: treat metrics as user-provided creative copy, not verified facts, unless verification is explicitly requested.

## Completion gate

Do not call the video finished unless all of these pass:

- Main text is readable on a phone-sized preview without pausing.
- No text enters platform-edge danger zones.
- The primary footage is not unintentionally cropped.
- CTA and reply keyword match the user's wording exactly.
- Final output is H.264 video with AAC audio in an MP4 container.
- The file opens and has the expected duration.
- Frame zero is backed by the selected still image and a fast one-frame decode confirms a non-empty picture; this is not a full contact-sheet or whole-video visual review.
- For a batch, every video has an independent semantic material mapping; duplicate source paths and duplicate content hashes are zero unless the user explicitly approved reuse.
- The batch material map proves the required image/video mix and, for 20 videos, at least 60 unique visual assets.
- Every BGM entry records a test-server-library-relative path and SHA-256; batch BGM paths and hashes are unique whenever the library contains enough tracks.

When delivering, report the exact output path, technical verification, music source, and whether factual claims were independently checked.
Also record selected material provenance in `SOURCES.md`: use test-server-relative paths and SHA-256 values, never credentials or server absolute paths outside the fixed library root.
