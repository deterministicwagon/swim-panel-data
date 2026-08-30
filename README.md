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

`notices.json` is schema version 1. It contains the longer plain-text notice details keyed by the IDs referenced from `schedule.json`, plus the matching schedule and source hashes. Informational source notices may be present even when they do not alter a swim.

Consumers should reject unsupported schema versions, verify that `validity.sourceStatus` is `valid`, compare `contentHash`, inspect `generatedAt` separately from `source.freshness`, and treat `uncertain` sessions as requiring confirmation. An expired horizon or stale source freshness must not be interpreted as current availability.

## Local verification

Python 3.10 or newer and the standard library are sufficient.

```bash
python -m unittest discover -v tests

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

The workflow is scheduled twice daily, at 02:17 and 14:17 UTC, and also supports a manual dispatch. In Ottawa this is 10:17 pm (previous day) and 10:17 am during EDT, or 9:17 pm (previous day) and 9:17 am during EST. Scheduled runs can be delayed under load, and GitHub disables scheduled workflows in public repositories after 60 days without repository activity.

To publish after review:

1. Create the initial commit and push `main` to `deterministicwagon/swim-panel-data`.
2. In the repository Pages settings, select **GitHub Actions** as the publishing source.
3. Run **Refresh Schedule** manually once and inspect its build and deploy jobs.
4. Verify the Pages URLs for `schedule.json` and `notices.json`, their hashes, validity, freshness, and attribution.

The workflow grants only `contents: read` to the build job. The separate deploy job receives `pages: write` and `id-token: write`, and uses the protected `github-pages` environment. No workflow commits generated data back to the repository.

GitHub operational references: [custom Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages), [workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax), and [scheduled-workflow disabling](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
