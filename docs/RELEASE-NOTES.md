# Writing release notes

[Documentation home](README.md) · [Contributing](../CONTRIBUTING.md)

Release notes explain what changes for someone using Print Handy. They are not a raw commit log. Preserve [the detailed internal history](history/INTERNAL_RELEASE_NOTES.md); use GitHub releases for the published user-facing account of a specific release.

## Order and content

1. **What you can do:** group features by a user's task, such as preparing a model, choosing filament or following a print. State prerequisites and printer-specific limits.
2. **Fixes:** describe the observed problem and resulting behavior. Do not claim a cause unless verified.
3. **Upgrade and compatibility:** include required configuration changes, preserved settings and relevant rollback limitations. Omit this section when unnecessary.
4. **Technical details and references:** list useful implementation details and linked PRs after the user-facing material. Preserve contributor credit without inventing coauthors.
5. **Verification and limitations:** distinguish automated offline tests, real slicer checks and live printer evidence. Name unverified capabilities and known issues.

## Reusable outline

```markdown
# <version> — <date>
Status: Draft / Unreleased / Released (choose the actual status)

## What you can do
### <user task>
- <capability, prerequisites and important limits> ([#PR](URL))

## Fixes
- <problem users experienced and changed behavior> ([#PR](URL))

## Upgrade and compatibility
- <required action, or omit if none>

## Technical details and contributors
- <implementation detail, PR links and accurate credit>

## Verification and known limitations
- <actual checks and results; name unrun checks>
```

Never assign a release date, version or shipped status to an unmerged draft merely to fill the outline. An unreleased entry must remain clearly labeled. Verify the intended release's changes before summarizing them; do not copy a PR's test results onto a different revision. Passing geometry tests does not establish slicing acceptance or physical printer readiness. Writing release notes does not publish a release or authorize deployment.

This guide does not rewrite historical releases or claim that pending rotation, Docker packaging or other draft work has shipped.
