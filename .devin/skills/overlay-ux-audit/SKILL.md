---
name: overlay-ux-audit
description: Render headless screenshots of Clueless overlay, onboarding wizard, and settings states to audit UI/UX, contrast, and layout
allowed-tools:
  - read
  - grep
  - glob
  - exec
---

Visually audit Clueless windows without needing Screen Recording permission by using Electron's offscreen `capturePage()` harness:

1. From `overlay/`, render representative states to `/tmp/clueless-*.png` via LaunchServices or headless Electron:
   - Card states per profile:
     `npx electron . --screenshot /tmp/clueless-dev.png --profile developer`
     `npx electron . --screenshot /tmp/clueless-student.png --profile student`
     `npx electron . --screenshot /tmp/clueless-clinical.png --profile medical`
     `npx electron . --screenshot /tmp/clueless-general.png --profile general`
   - Pill, streaming, and ask-bar states:
     `npx electron . --screenshot /tmp/clueless-pill.png --state pill`
     `npx electron . --screenshot /tmp/clueless-stream.png --streaming`
   - Onboarding steps:
     `npx electron . --screenshot /tmp/clueless-step1.png --onboarding-step 1`
2. Inspect each rendered PNG with `read` and verify:
   - Readability & WCAG AA contrast over translucent dark surfaces (`tokens.css`)
   - No clipped pills, overlapping status/toast labels, or truncated primary actions
   - Focus safety: overlay controls use `acceptFirstMouse` and never steal focus unless the ask bar is explicitly opened
   - Strict DOM safety: zero `innerHTML` assignments (`npm run lint` in `overlay/`)
