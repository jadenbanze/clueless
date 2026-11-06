## What and why

<!-- One change per PR. Link the issue if there is one. -->

## How it was tested

<!-- Commands run and what they showed. UI changes: before/after screenshots (`electron . --screenshot`). -->

## Checklist

- [ ] Title is a Conventional Commit (`feat:`, `fix:`, `docs:` …): it sets the version bump
- [ ] `pre-commit run --all-files` passes
- [ ] Offline tests pass (`server.test_security`, `test_phi`, `test_profiles`, `test_providers`, `npm test`)
- [ ] Prompt changes: `test_brain` passes for every profile
- [ ] No new network destination, and no weaker security boundary (or it's discussed in an issue)
- [ ] Docs updated if behaviour changed
