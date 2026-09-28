# Swim Panel Data

This repository builds a public schedule-data feed for an RP2350 LED frame. It does not contain the frame firmware. The source is the third-party, unofficial [ottrec export](https://data.ottrec.ca/export/latest.json), which extracts schedule data from City of Ottawa facility pages; it is not an official City repository or API.

The reducer selects Brewer Pool and Arena, Minto Recreation Complex - Barrhaven, Richcraft Recreation Complex-Kanata, and Nepean Sportsplex by exact Ottawa facility URL slug and expected name. It emits explicit Saturday and Sunday dates in a rolling 14-day `America/Toronto` horizon. Activities whose normalized name contains the words `lane swim` are included, except names containing `reduced capacity`.

## Safety and interpretation

The source is validated before either output is replaced. The reducer rejects a missing or malformed top-level structure, a missing or duplicated target facility, target scraper errors, target data more than two calendar days old, invalid target dates or times, missing session times, missing referenced HTML, and missing attribution. This means unavailable, invalid, or stale target data cannot be mistaken for a valid empty schedule or hidden behind a new generation timestamp. A structurally valid and fresh source with no matching sessions produces a valid feed with `sessionCount: 0`.

Output files are written through temporary files and atomically renamed. The Pages workflow downloads raw data into runner temporary storage, runs the offline tests, and uploads only `index.html`, `schedule.json`, and `notices.json`. A failed download, test, validation, or generation prevents deployment, leaving the last successfully published Pages deployment in place.

Notice handling is intentionally conservative:

- unambiguous facility or pool closure ranges suppress all affected sessions;
- unambiguous date/activity/time-specific lane-swim cancellations suppress only the matching activity and slot;
- missing date boundaries and ambiguous schedule notices mark affected sessions `uncertain`;
- duplicate slots are merged, warning references are unioned, and a conflicting reservation requirement resolves to `true` with an uncertainty warning;
- raw source HTML is converted to plain text and is never placed in the device feed.

Temporary closure dates are parsed from source notices; none are hardcoded.

## Output schema

`schedule.json` is compact JSON, currently schema version 2.

- `generatedAt`: generation time. It is deliberately excluded from `contentHash`.
- `contentHash`: SHA-256 of the stable feed content. It includes the horizon, schedules, warnings, attribution, source freshness, and `source.revisionHash`. A same-day source correction therefore changes it.
- `validity`: source status, overall `valid` or `valid_with_uncertainty` status, Toronto horizon, and session counts. Zero sessions with `sourceStatus: valid` means genuinely no selected swims were found.
- `source`: source URL, a relevant-source revision hash, oldest/newest target `scrapedAt` values, and source attribution.
- `facilities[]`: exact facility identity, source freshness, parsed closure ranges, removed cancellations, facility warning references, and generated schedules.
- `facilities[].schedules[]`: explicit date, activity name, `HH:MM` times, reservation requirement, `scheduled` or `uncertain` status, and warning IDs.

`race.json` is a separate, tiny (under 512 bytes) weekly summary for the frame's clock. It is not derived from ottrec. Its source is a private upstream endpoint whose URL is held only as the `RACE_SUMMARY_URL` Actions secret. It contains exactly `version` (1), `week_start` (a Monday) and `week_end` (the following Sunday) as `YYYY-MM-DD`, `timezone` (`America/Toronto`), `generated_at` (local ISO time with offset), and `a` and `b`: two anonymous whole-metre totals for that week. It holds no names, session records, times, locations, or history. `reducer/race.py` accepts only that exact field set. It rejects anything else, including extra fields, and rebuilds the file from the validated values. A fresh summary must have been generated within 10 minutes of the run. Consumers must judge freshness from `generated_at`, not from when they downloaded the file.

`notices.json` is schema version 1. It contains the longer plain-text notice details keyed by the IDs referenced from `schedule.json`, plus the matching schedule and source hashes. Informational source notices may be present even when they do not alter a swim.

Consumers should reject unsupported schema versions, verify that `validity.sourceStatus` is `valid`, compare `contentHash`, inspect `generatedAt` separately from `source.freshness`, and treat `uncertain` sessions as requiring confirmation. An expired horizon or stale source freshness must not be interpreted as current availability.

## Local verification

Python 3.10 or newer and the standard library are sufficient.

```bash
python -m unittest discover -v tests

python reducer/race.py summary --input upstream-race.json --output /tmp/race.json

curl --fail --show-error --silent --location \
  https://data.ottrec.ca/export/latest.json \
  --output /tmp/swim-panel-latest.json

python reducer/reducer.py \
  --input /tmp/swim-panel-latest.json \
  --output /tmp/swim-panel-schedule.json \
  --notices-output /tmp/swim-panel-notices.json
```

Use `--ref-date YYYY-MM-DD` for deterministic samples and `--pretty` for human-readable output. Raw downloads and generated JSON are ignored by git.

## Publishing (not automatic from this checkout)

The workflow has two schedules and a manual dispatch:

- **Full refresh** at 02:17 and 14:17 UTC rebuilds `schedule.json` and `notices.json` from ottrec and refreshes `race.json`. In Ottawa this is 10:17 pm (previous day) and 10:17 am during EDT, or 9:17 pm (previous day) and 9:17 am during EST. If only the race summary fails here, the currently published `race.json` is re-validated and carried forward. With none published yet, the site deploys without it.
- **Race refresh** at minutes 07, 22, 37, and 52 of every hour refreshes only `race.json`. A Pages deployment replaces the whole site, so these runs download the currently published `schedule.json` and `notices.json`. A unique query bypasses the CDN cache. The runs check that the pair is still valid and hash-linked, then deploy it byte-for-byte alongside the new `race.json`. They never contact ottrec. If the race summary fails, the run fails and nothing deploys, leaving the last deployment in place. Until the `RACE_SUMMARY_URL` secret exists, race runs exit early without deploying.
- **Manual dispatch** defaults to a full refresh. Clear `rebuild_schedule` to run a race-only refresh.

Scheduled runs can be delayed under load, and GitHub disables scheduled workflows in public repositories after 60 days without repository activity.

To publish after review:

1. Create the initial commit and push `main` to `deterministicwagon/swim-panel-data`.
2. In the repository Pages settings, select **GitHub Actions** as the publishing source.
3. Add the repository secret `RACE_SUMMARY_URL` (Settings → Secrets and variables → Actions) once the upstream summary endpoint is deployed.
4. Run **Refresh Schedule** manually once and inspect its build and deploy jobs.
5. Verify the Pages URLs for `schedule.json`, `notices.json`, and `race.json`, their hashes, validity, freshness, and attribution.

The workflow grants only `contents: read` to the build job. The separate deploy job receives `pages: write` and `id-token: write`, and uses the protected `github-pages` environment. No workflow commits generated data back to the repository. The race summary source URL is never printed, and the raw upstream response is never uploaded.

GitHub operational references: [custom Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages), [workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax), and [scheduled-workflow disabling](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
