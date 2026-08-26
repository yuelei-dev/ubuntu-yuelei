# Music and editing

## Music choice

Choose music from the emotional job of the footage:

| Video tone | BPM | Character |
| --- | ---: | --- |
| Warm growth/community | 72-90 | Soft pulse, warm chords, light percussion |
| Business/meeting/opportunity | 88-105 | Clear beat, confident bass, clean plucks |
| Premium/luxury/social circle | 84-100 | Restrained groove, polished synth or piano |

Avoid vocals and dense lead melodies because the viewer is already reading. For this workflow, randomly select only authorized BGM stored under the Huangque test-server library `/home/ubuntu/material-libraries/huangque-media/`. Do not generate replacement music or substitute another local or web source. For batches, shuffle once and assign without replacement while enough library tracks remain.

## Mix

- Mute source speech by default for this text-led format.
- Keep useful room ambience only if it adds realism and does not mask the track.
- Aim for -14 to -12 LUFS integrated.
- Keep true peak at or below -1 dBTP.
- Fade in over 100-180 ms and out over 250-400 ms.
- Align the first strong beat with the first complete text reveal or first shot change.

## Source normalization

Mixed phone and screen-recorded footage may seek poorly in browser video elements. If preview or render shows frozen, blank, or incorrect initial frames, create an editing proxy:

```powershell
ffmpeg -i input.mp4 -vf "fps=30,format=yuv420p" -an -c:v libx264 -preset medium -crf 18 -g 30 -keyint_min 30 -sc_threshold 0 proxy.mp4
```

Keep the original source unchanged. Record which proxy maps to which original.

## Editing checks

- Every chosen shot must support a specific line of copy.
- Avoid two adjacent shots with nearly identical framing.
- Do not let a cut land in the middle of a short entrance animation.
- Inspect the last 300 ms for black frames or an audio tail beyond picture.
