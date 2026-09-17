---
name: daily-plan
description: This skill should be used when the user runs "/daily-plan", asks to "draft tomorrow's to-do list", "carry over my tasks", "plan the next working day", or wants a dated to_do_DDMMYYYY.md built by carrying over unfinished tasks and pulling in meeting actions. Designed to run unattended on a weekday-evening schedule (~17:10), shortly after daily-summary.
version: 0.1.0
---

# Daily Plan

You are helping draft a daily to-do list. This task is designed to run automatically in the evening (e.g. ~17:10), about 10 minutes after the `daily-summary` task has written today's daily summary. The goal is to produce a `to_do_DDMMYYYY.md` file that can be reviewed and edited manually. This is an UNATTENDED run — never pause to ask anything; make the sensible choice, proceed, and report any decisions in the final notification.

> **Setup:** Replace `<WORKSPACE>` below with the absolute path to your notes folder (the same one used by the daily-summary skill). Adjust the section names in Step 4 (`## Team`, `## Client delivery`, `## Roadmap`, `## Other`) to match your own workflow. Schedule this to run on weekdays shortly after the daily-summary task.

## Step 1: Determine the target date
Because this runs in the evening, the target is the NEXT WORKING DAY after today. Get today's date via bash. If today is Mon–Thu, target = tomorrow. If today is Friday, target = next Monday (skip the weekend). Resolve the target to a date and note it as DD Month YYYY for the notification.

## Step 2: Find the source to-do file to carry over from
Look in `<WORKSPACE>/to_do_md/` for the most recent `to_do_DDMMYYYY.md` file dated *before* the target date (this will normally be today's file).
If no prior to-do file exists, do not invent one — save nothing, and send a notification saying there's no to-do file to carry forward (the first one should be created manually). Then stop.

## Step 3: Find the source daily summary
Look in `<WORKSPACE>/daily_summary_md/` for `daily_summary_DDMMYYYY.md` dated the day before the target date (normally today's summary, just generated).
If it doesn't exist, fall back to the most recent prior daily summary. If none exist at all, skip Step 6 (Meeting Notes Actions section).

## Step 4: Parse and carry over tasks
**Important:** When parsing the source to-do file, **ignore the `## Meeting Notes Actions` section entirely**. That section is review-only — anything important from it will already have been moved manually into the earlier sections (`## Team`, `## Client delivery`, `## Roadmap`, `## Other`). Carrying it forward would just create noise. Stop parsing for carry-over when you reach the `## Meeting Notes Actions` heading.

Read the source to-do file. It uses this structure:
```
# Daily actions
## Team
- task one [DONE]
- task two [IN PROGRESS]
- task three
- Bonus
    - bonus task
    - bonus task [DONE]
## Client delivery
- ...
## Roadmap
- ...
## Other
- ...
```
Apply these carry-over rules to every task line (top-level and Bonus sub-items):
- A line may contain a single task or a composite of sub-items separated by commas or in parentheses (e.g. `reviews - me [DONE], up (Person A, Person B), peer finish (Person C [DONE], Person D [DONE])`).
- **Within each line, drop any sub-item that is fully marked `[DONE]`** — including parenthesised groups where every named item inside is `[DONE]` (e.g. `peer finish (Person C [DONE], Person D [DONE])` should be removed entirely).
- **Keep, but rewrite** sub-items where `[DONE]` is followed by a caveat or follow-up that implies remaining work. Translate the residual work into a clean new task rather than carrying the old wording with the `[DONE]` marker. Examples:
  - `kick-off plan for Q1 update -> with colleague [DONE] -> now needs my review` → `Review plan for Q1 update`
  - `me [DONE - not submitted]` → `submit my review` (or similar — keep the surrounding context if it helps)
  Use judgment: the goal is for the new line to read as the *next* action, not as residue from yesterday.
- **Drop the entire line** only if every sub-item on it is fully done (i.e. nothing remains after the sub-item cleanup).
- **Carry over** every other line, preserving:
  - `[IN PROGRESS]` markers.
  - Lines with no marker — preserve as-is.
  - Bonus items where any work remains — keep them under their `Bonus` sub-bullet.
When carrying over, strip any `[ADDED]` marker (it represents "added during the day" and is no longer meaningful in the new file). Preserve every other marker (`[IN PROGRESS]`, ownership notes like `-> with X`, etc.).
Preserve the original section order and structure. If a section ends up with no carry-over items (top-level or Bonus), omit it from the new file.

## Step 5: Build the new file
Write the new file with the same `# Daily actions` heading and the carried-over sections. Match the original indentation:
- Top-level tasks under each `##` section as `- task`.
- A `- Bonus` bullet at the section bottom, with its sub-tasks indented one tab below as `    - bonus task`.
**If the target date is a Friday**, add a recurring end-of-week reminder (e.g. `- eow updates`) to the `## Team` section. Create the section if no Team carry-over items exist.

## Step 6: Add Meeting Notes Actions section
After all carried-over sections, add a new section:
```
## Meeting Notes Actions
```
Read the source daily summary. Find the consolidated **Action items** block (under the `### Overall Summary` heading). For each meeting group, write:
```
**[Meeting title]**
- task [owner: X]
- task
```
Strip the `[ ]` checkbox from the source — use plain `-` bullets to match the to-do file convention. Preserve `[owner: X]` annotations.
If the daily summary has no action items, omit this section.

## Step 7: Save to file
Save as:
`<WORKSPACE>/to_do_md/to_do_DDMMYYYY.md`
where `DDMMYYYY` is the target date (e.g. `to_do_03062026.md`).
If a file already exists at that path, DO NOT overwrite it. Instead, save nothing and report in the notification that a file already exists at that path so it can be resolved manually.

## Step 8: Notify
Send a brief completion notification confirming the saved file path (e.g. `Saved to to_do_md/to_do_03062026.md`) and noting which source files were used (the to-do filename carried over from, and the daily summary filename), so the run can be verified. If you skipped saving (no prior to-do file, or file already exists), say so clearly.
