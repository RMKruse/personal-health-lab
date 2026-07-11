# Issue tracker: GitHub

Issues and PRDs for this repository live as GitHub issues in `RMKruse/personal-health-lab`. Use the `gh` CLI for all operations.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,comments`
- Comment: `gh issue comment <number> --body "..."`
- Label: `gh issue edit <number> --add-label "..."`
- Close: `gh issue close <number> --comment "..."`

Infer the repository from the configured Git remote when commands run inside this clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Publishing

When a skill says to publish to the issue tracker, create a GitHub issue.

## Wayfinding

Wayfinder maps are issues labelled `wayfinder:map`; child tickets use GitHub sub-issues when available and task-list links otherwise. Prefer native issue dependencies for blocking relationships and fall back to a `Blocked by:` line only when dependencies are unavailable.
