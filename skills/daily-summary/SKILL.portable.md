---
name: daily-summary
description: This skill should be used when the user runs "/daily-summary", asks to "summarise today's meetings", "write the daily summary", "pull my Granola notes for today", or wants a dated daily summary of meeting notes and action items saved to a markdown file. Designed to run on a weekday-afternoon schedule (~17:00).
version: 0.1.0
---

# Daily Summary

Produce a daily summary of your Granola meeting notes for TODAY (the date this task runs). Uses the Granola MCP connector (tools: `list_meetings`, `get_meetings`, `get_meeting_transcript`).

> **Setup:** Replace `<WORKSPACE>` below with the absolute path to the folder where you want summaries saved (e.g. the root of your notes repo). Schedule this to run on weekdays in the late afternoon (~17:00).

## Step 1: Determine the target date
Use today's date (the date this task runs). Resolve it to an ISO date (YYYY-MM-DD). You can get the current date via bash if needed.

## Step 2: Pull meetings for that day
1. Use `list_meetings` to get all meetings. Filter to those where the meeting date matches today.
2. For each matching meeting, use `get_meetings` to retrieve the full note content including the AI-enhanced summary.

If no meetings are found for today, save a short markdown file noting "No meetings found for [date]." at the path in Step 6, send a brief notification saying so, and stop.

## Step 3: Extract action items per meeting
For each meeting, scan the enhanced notes and summary for:
- Explicit action items or next steps (anything phrased as a task, commitment, or follow-up)
- Decisions that imply an action (e.g. "we agreed to X" → action to do X)
- Items assigned to you specifically (prioritise these)
- Items assigned to others that you need to track

Ignore general discussion points, background context, and observations with no action implied.
If an action item has a clear owner other than you, note it in brackets: `- [ ] Send proposal to client [owner: Sarah]`

## Step 4: Write a per-meeting block
For each meeting, write a tight summary (3–6 bullets): main topic(s), key decisions, notable outcomes or blockers, who was involved if relevant. Then add an *Action items* sub-section as a `- [ ]` checklist. Omit the sub-section if a meeting had no clear actions. Keep it tight — a quick reminder, not a reproduction of the notes.

Format:
```
---
**[Meeting title] — [time if available]**
- Key point one
- Key point two
*Action items*
- [ ] Action item one
- [ ] Action item two [owner: Name]
---
```

## Step 5: Write an overall day summary
After all per-meeting blocks, write a single-paragraph overall summary (4–8 sentences): dominant themes, most significant decisions/outcomes, cross-cutting threads, overall tone and focus. Then add a consolidated *Action items* section listing every action grouped by meeting, using `- [ ]` format. End with a count: `[N] action items across [M] meetings.`

Format:
```
---
### Overall Summary
[paragraph]
### Action items
**[Meeting title]**
- [ ] Action item
**[Next meeting title]**
- [ ] Action item
[N] action items across [M] meetings.
---
```

## Step 6: Save to file
Save the full output as a markdown file at:
`<WORKSPACE>/daily_summary_md/daily_summary_DDMMYYYY.md`
where DDMMYYYY is today's date (e.g. `daily_summary_01062026.md`). Create the `daily_summary_md/` directory if it does not exist. The file must begin with:
`# Daily Summary — [DD Month YYYY]`

## Step 7: Notify
Send a brief completion notification summarising the day in 1–2 sentences plus the total action item count, and confirm the saved file path (e.g. "Saved to daily_summary_md/daily_summary_01062026.md").
