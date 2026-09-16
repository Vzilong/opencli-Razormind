# QA

## Automated evidence

- Backend local-auth, identity, and model tests: pass.
- Ruff on changed Python authentication files: pass.
- TypeScript type-check: pass.
- Targeted ESLint on changed authentication UI files: pass.
- Login theme and authentication-path regression script: pass.
- Next.js production build: pass.
- Alembic graph: one head, including the local administrator migration.
- Design and motion foundation checks: ready.

## Covered behavior

- Fresh installation reports that no local administrator exists.
- A valid Bootstrap credential creates the only local administrator.
- Invalid Bootstrap credentials and passwords are rejected.
- Repeated failed attempts are rate-limited.
- A signed local session crosses Fleet auth and resolves to a platform administrator identity.
- A second setup attempt is rejected.
- Daily login uses the local administrator password.
- Recovery remains hidden behind a secondary disclosure.
- OIDC remains optional, including its advanced Fleet-token setting.

## Design Pipeline v0.9.0 runtime acceptance

Date: 2026-09-04

Result: **FAIL for the login FX layer.** Password authentication and responsive containment pass, but the shipped visual effects do not conform to the active design and motion foundations.

### Control-plane evidence

- Release: `design-pipeline v0.9.0`; archive SHA-256 `1c6ed651590dc255b96ae8a0a2bdfd87bf3d9649d72f47c06a76041cdb68e957`.
- `designer-pipeline doctor`: `ready`.
- Primary Prism route: `ui-craft`, confidence `high`.
- Toolchain resolution: `ready`; React, project-owned UI library, no new dependency.
- Design foundation: `ready`, SHA-256 `73f4c58bcb1aa1c33baf0db99ba1d75df90d39092fd9ff13da09dd26033c0409`.
- Motion foundation: `ready`, posture `minimal`, SHA-256 `31c6d70594b5c0c7d396544ff84a2e31e30795c657a24592869cf5f26094331e`.
- The foundation `ready` results validate schema shape only; they do not prove semantic coverage for the login scene.
- Toolchain artifacts: `qa/toolchain-request.json`, `qa/toolchain-plan.json`.
- `designer-pipeline status`: unavailable because this legacy change has no `state.json`; this is a process gap, and no `complete` or `archive` claim is made.

### ORCA browser evidence

- Runtime: ORCA 1.4.197 Electron WebView, `http://127.0.0.1:8030/login`.
- Desktop viewport exercised: 1312x1280.
- Narrow viewport exercised: 390x844; no page-level overflow, form remained single-column, and the product-introduction panel was removed.
- Actual ORCA preference was `prefers-reduced-motion: reduce`. The page selected the static GPU fallback with zero canvases and zero active animations.
- The normal-motion path was instrumented by overriding the media preference to `no-preference`.
- Failed login returned HTTP 401 and announced `用户名或密码错误` through an `aria-live="polite"` region.
- Recovery login returned HTTP 200 and navigated to `/dashboard`.
- The visible `1 Issue` chip in development captures is the Next.js development overlay, not product UI; it is excluded from the product verdict.

### Findings

| Priority | Status | Finding | Evidence |
| --- | --- | --- | --- |
| P1 | Pre-existing | The change-level motion contract assigns `reveal.trim-line` to successful login, but the foundation reserves that primitive exclusively for newly created or actively traced graph edges. Login needs its own bounded acknowledgement scene and static reduced-motion state. | `MOTION.md:31,57,65`; `openspec/changes/local-admin-onboarding/motion.md:5-8` |
| P1 | Pre-existing | The implementation contradicts the minimal motion contract. `MOTION.md` prohibits decorative idle motion, autonomous loops, particle backgrounds, and ordinary WebGL effects; the login page runs one continuous WebGL2 canvas for each of three decorative themes and rotates headline content every 2.8 seconds. | `frontend/app/login/page.tsx`; runtime GPU and headline instrumentation |
| P1 | Pre-existing | Failed authentication loses recovery focus. After HTTP 401, `document.activeElement` was `BODY`; neither field received an inline error state or focus. The distant toast is announced correctly but is the only recovery signal. | ORCA error-state instrumentation and `login-error-reduced.png` |
| P2 | Pre-existing | The FX layer outranks the authentication task. Across fluid, terminal, and pixel captures, the large animated promotional region is more salient than the fixed-width login card. Terminal and pixel treatments add dense moving texture around a two-field task. | Three desktop captures |
| P2 | Pre-existing | Idle animation consumes measurable work without communicating state. Over three seconds: fluid used 234.6ms task / 167.9ms script; terminal 191.6ms / 107.4ms; pixel 255.1ms / 185.6ms. | ORCA Performance metrics |
| P2 | Pre-existing | Interactive targets miss the pipeline's 44x44 default: desktop theme buttons 64x32, inputs 384x32, submit 384x40; mobile theme buttons 34x32, inputs 326x32, submit 326x40. | ORCA geometry instrumentation |
| P2 | Pre-existing | The 11px `text-white/35` footer computes to approximately 3.08:1 against `#070604`, below WCAG AA for normal text. | Computed token composition |
| P3 | Pre-existing | The three background modes are an exposed visual preference unrelated to authentication, reset to `liquid` on reload, and add a second decision before login. | Local React state and runtime behavior |

### Passing behavior

- Reduced motion removes the GPU canvas, autonomous headline rotation, and active animations while preserving all login information and controls.
- The 390x844 layout has no horizontal or vertical overflow and keeps the form order stable.
- Theme buttons expose accessible names and `aria-pressed`.
- The pending state expands the submit button from 40px to 64px and provides `正在登录 / 建立本地会话`.
- The error toast is present in a polite live region.

### Scorecard

| Axis | Score | Note |
| --- | ---: | --- |
| Visual taste | 2/5 | Distinctive industrial identity, but three competing FX dialects and excessive decorative salience. |
| UX clarity | 3/5 | Form order and primary action are clear; hero motion and the theme chooser dilute task focus. |
| Accessibility | 2/5 | Reduced-motion and live-region behavior pass; target sizes, footer contrast, and failed-login focus recovery do not. |
| Responsiveness | 4/5 | Narrow layout contains correctly; mobile targets remain undersized. |
| Motion quality | 1/5 | Continuous decorative WebGL and rotating copy violate the authored minimal posture and timing vocabulary. |
| Engineering fit | 2/5 | Project primitives are reused, but three GPU implementations are loaded for an authentication surface outside the approved runtime policy. |
| Performance risk | 2/5 | No failure observed, but idle login animation spends 6.4-8.5% of the measured three-second window in task work. |

### Evidence artifacts

- `qa/screenshots/login-fluid-motion.png` — SHA-256 `da3730ebe03825916f32355b15f1dfb603f1b4f1ee07248e72eddc8b671f70cf`
- `qa/screenshots/login-terminal-motion.png` — SHA-256 `2726261bf22eca2c644f5754501ad49b75834678534b3d75cf1f67ac46170279`
- `qa/screenshots/login-pixel-motion.png` — SHA-256 `025c83c5b2d531e813f25dce30b9b7ef0b0433627f2bc16912b75f7f9ca7162f`
- `qa/screenshots/login-mobile-motion.png` — SHA-256 `4007cc7404983ac6651bcb9b925946179244b4aa77f453c224352f4d4b410647`
- `qa/screenshots/login-error-reduced.png` — SHA-256 `f9effb901af8e8d35aaac4013c5df44d79fbbdcd446e66328246531af7fc0ba3`

### Limitations

- No external visual reference or pixel-fidelity target was supplied; reconstruction fidelity is not applicable.
- Tablet, 1920x1080, 200% zoom, font-failure, and production-build captures were not exercised in this acceptance run.
