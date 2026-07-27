<!--
Thanks for contributing! Please keep the change focused and squashed into one
commit. See CONTRIBUTING.md for the full workflow.
-->

## What and why

<!-- What does this change do, and which issue / FR / NFR does it address? -->

Closes #

## How it was verified

<!-- Commands run, tests added, and — per the repo's convention — whether this
was tested against mocks or validated against a live stack. -->

## Checklist

- [ ] Linked to an issue (`Closes #NNN`) and scoped to one logical change
- [ ] Branch squashed into a single, well-described commit
- [ ] `ruff check services scripts tests` passes
- [ ] `mypy services/common/common` passes (if `services/common` changed)
- [ ] Tests added/updated next to the behavior; coverage gate (≥85%) still met
- [ ] `docs/` updated if behavior or setup changed, with honest
      implemented / tested-against-mocks / validated-live labels
- [ ] No new floating image tags (NFR-16) and no secrets committed
