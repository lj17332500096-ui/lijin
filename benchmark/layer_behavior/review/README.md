# Layer label review materials

`cases.v1-blind-review.csv` is a blind second-review worksheet generated from `../cases.v1.jsonl`. It includes case IDs, categories, and prompts but leaves the expected label and rationale blank so the independent reviewer does not start from the current label.

The reviewer should fill `independent_expected_tool` with `true` or `false`, explain ambiguous action/clarification cases in `rationale`, and return a dated copy. Compare it with the versioned dataset only after the independent pass. Do not replace `cases.v1.jsonl` or its baseline until disagreements are adjudicated and a new dataset version is created.
