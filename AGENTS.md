# Repository File Placement Rules

Follow these rules when creating or moving project files.

## Reports and documentation

- Put new architecture/design reports and ADRs under `docs/architecture/`.
- Put security/runtime audit reports under `docs/audits/`.
- Put production verification and release acceptance reports under `docs/acceptance/`.
- Put operator guides and configuration references under `docs/operations/`.
- Put superseded reports under `docs/archive/YYYY-MM/`; add an entry to the archive index and update repository links when moving a file.
- Keep current documentation in its matching category. Do not create new report files at the repository root.

## Experiments and generated output

- Put reproducible experiment code, cases, and reviewed baseline data under `benchmark/<topic>/`, with a topic README.
- Write raw benchmark runs to `var/benchmark-runs/<topic>/<run-id>/`; promote only reviewed, reproducible baselines into `benchmark/<topic>/`.
- Put runtime logs in `var/logs/`, traces in `var/traces/`, test reports in `var/test-reports/`, and retained temporary files in `var/tmp/`.
- Use the operating system temporary directory for disposable scratch files. Never write generated or temporary output to the repository root.

## Paths and imports

- Derive the repository root from `runtime_paths.PROJECT_ROOT`; do not derive it from a module's package directory after moving code.
- Read configurable locations from environment settings. Do not add machine-specific absolute paths to source or example configuration.
- When moving a module, update every import and path reference, then run the module's focused acceptance tests before moving another module.
