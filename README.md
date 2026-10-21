# Bounty Hunter

## All-in-one Windows desktop (v0.2.1)

**The main Windows app now combines bounty scanning, supervised coding work,
settings, earnings, and payout links.** No terminal is needed for regular use.
It does **not** automatically accept work, promise payment, or withdraw money.

### Install / launch

1. Open [Windows Desktop App builds](https://github.com/Unborne-Fetus/bounty-hunter/actions/workflows/build-windows-dashboard.yml)
   and select the latest **successful** workflow run on `main`.
2. Under **Artifacts**, download **BountyHunter-Windows-Installer**, extract the
   ZIP, and run `BountyHunter-Setup.exe`. This installs a per-user app with
   a Start Menu shortcut. Alternatively download
   **BountyHunter-Windows-Portable** to run `BountyHunter.exe` without installing.
3. On first launch, choose a workspace for local history/reports, select
   marketplace sources, optionally add Algora organizations, confirm your
   Boss payout-residency country if applicable, and choose whether to scan
   periodically while the program remains running. No API key is stored.
4. Click **Find actionable** to scan enabled marketplaces and show **only**
   bounties whose current checks have fully passed and whose decision is
   `approve_for_human_review`. This is a strict *display filter*, not a
   payment guarantee or a shortcut around preflight, platform eligibility,
   or human approval. **Show all bounties** switches back to every result
   without losing any records; **Show actionable only** restores the filter.
   **Find actionable** also refreshes the public Algora board discovery when
   its saved audit is missing or more than 24 hours old. The search checks
   GitHub bounty-label hints against actual Algora boards before importing
   listings; it never treats a GitHub label as payment proof. Discovery is
   bounded and cached, and a failed provider lookup is reported without
   causing the entire scan to fail. **Scan now** remains the regular scan.
   When no matches pass, the results pane and **Why 0 matches?** summarize
   the actual primary rejection reasons, marketplace counts, and whether
   Algora or BountyPay were missing, disabled, or failing.
   **Find Algora boards** remains available for a manual discovery refresh.
   A decision of **needs_verification** means GitHub or repository evidence
   is missing, not that the bounty was conclusively rejected. Select that
   row and click **Recheck selected on GitHub** to refresh its issue and repo
   evidence directly, or use **Recheck all on GitHub** to refresh every saved
   listing with a usable issue link within the GitHub API request limit. The
   app shows verified, missing-link, failed, and deferred counts and checks
   deferred issues first on the next pass. Existing authenticated `gh`
   logins are reused for public read requests, without storing or printing
   the token. The display's **Evidence coverage** number counts known
   scoring signals; it does not measure success odds or guarantee payment.
   Source diagnostics reports missing issue links and API limits. If a
   provider never supplies a usable public GitHub issue link, inspect the
   provider listing; Bounty Hunter will not fabricate one. **Verifying a
   repository does not override a separate payout or eligibility rejection:**
   Opire and Boss public inventory may still be rejected where payment
   requires sponsor action. Failed, stale, or rate-limited API requests
   cannot promote an opportunity to eligible.
5. Use **Coding workspace** to review and approve a queued attempt, run isolated
   preflight, generate an AI patch, run before/after tests, review the immutable
   bundle, explicitly approve the exact patch, prepare a local PR and separately
   confirm **Publish PR**. Public publication can never run without confirmation.
6. Use **Overview**, **Payouts / Withdraw** and **Record Activity** to inspect
   self-recorded earnings and open payment providers. No bank/wallet integration
   or withdrawal API is currently implemented.

If you already have a clone with historical
`data/bounty_hunter.db`, select that **repository folder** as the workspace
so the app reuses your existing reports/history. Your installed program and
data are separate: uninstalling preserves settings and earnings history.

**Requirements:** Only the installer/portable EXE is needed for scanning and
the earnings dashboard. Running approved *coding* attempts additionally
requires installed and available Docker Desktop or Podman, Git, and authenticated
Codex CLI; public PR publication also needs authenticated GitHub CLI (`gh`).
The setup wizard indicates which commands are discoverable, but cannot
automatically authenticate accounts or guarantee that an external provider
accepts your work. Some platforms require identity/age/country eligibility.

Under **Settings / updates → Check tools and logins**, run bounded checks for
Git, Codex, Node/npm, GitHub CLI login, and Docker/Podman daemon readiness.
Node/npm are optional for scanning. These checks never display or save login
output or tokens. The request timeout is editable in Setup and is preserved
when other settings change. Your selected GitHub token environment variable
also applies to fresh preflight and patch-generation checks.

Scans show the current marketplace or GitHub verification stage. A provider
outage appears in **Source diagnostics** while the remaining sources continue;
zero eligible results include guidance and each row's rejection reasons.
Algora is completely skipped when disabled. With Algora enabled but no boards,
use **Find Algora boards**, then scan again. Coding buttons follow the selected
attempt's stage and available artifacts. A stopped container runtime leaves
approved work available for retry after starting Docker/Podman.

Windows builds test the GUI from source and launch the packaged EXE in a
temporary workspace using `--smoke-test` before uploading installer artifacts.

**Updates:** Under Settings, click **Check for Windows app updates**. The app
checks only the official GitHub Releases source and, with confirmation, opens
the release or the Actions downloads page. Upgrades currently require the
user to download/run the newer installer; there is no silent self-updater.

**Local launcher alternative:** With Python 3.9+ and a source checkout, pull
`main` and double-click `START-DASHBOARD.bat`. The legacy
`START-WINDOWS.bat` still runs the command-line scanner.

[View the live, read-only telemetry dashboard](https://bounty-hunter-telemetry.vercel.app)

Bounty Hunter is a validation-first system for discovering, ranking, and
supervising AI-agent implementations of coding bounties. Public submission is
never autonomous: a human must approve both the attempt and the exact
hash-bound review bundle, then provide a second explicit publication
confirmation.

## Safety boundary

This version can:

- Read TaskBounty's public opportunity API.
- Read configured Algora organization bounty boards.
- Read Opire's public server-rendered reward inventory.
- Read Boss's public open-bounty API with country and payment-assurance gates.
- Watch BountyPay's public API while excluding testnet rewards from income.
- Enrich GitHub-linked opportunities with repository and issue evidence.
- Import manually enriched opportunities from JSON.
- Reject expired, closed, ambiguous, contested, unsupported, or low-value work.
- Reject repositories that are too new, have fewer than 10 stars, lack an
  identifiable open-source license, or expose no contribution-policy artifact.
- Estimate payout probability, expected value, and expected hourly return.
- Generate auditable JSON and Markdown reports.
- Clone public GitHub repositories inside a constrained container.
- Give Codex bounded, sanitized text context without repository filesystem
  access and apply only a validated standard Git patch.
- Run verification without network access in Docker or Podman.
- Require failing-before-fix and passing-after-fix regression evidence.
- Produce an immutable review bundle for a second human approval.
- Prepare a separate clean commit containing exactly the approved patch.
- Publish that commit through the authenticated GitHub CLI only after an exact
  bundle-bound public-submission confirmation.

This version does not:

- Access private repositories.
- Open issues or comments.
- Submit bounty claims.
- Allow Codex to execute untrusted repository code.
- Pass GitHub, marketplace, wallet, or submission credentials to an agent.

Dependency preparation is supported only in two fail-closed forms:

- npm with package-lock v2 or newer, integrity metadata, public npm-registry
  HTTPS URLs, and all lifecycle scripts disabled.
- Python requirements with exact `==` pins and SHA-256 hashes for every direct
  and transitive dependency, installed only from binary wheels with dependency
  resolution disabled.

Preparation receives registry network access but no credentials, mounts the
repository read-only, and writes into a separate dependency workspace. Tests
mount that workspace read-only and run without network access. pnpm, Yarn,
source distributions, editable installs, URL requirements, local package
builds, and post-install build steps remain ineligible.

## Run

Python 3.9 or newer is sufficient; there are no third-party runtime
dependencies.

```bash
python3 -m bounty_hunter.cli
```

Scan one or more Algora organization boards:

```bash
python3 -m bounty_hunter.cli \
  --algora-org cal \
  --algora-org algora
```

GitHub enrichment works anonymously for a small snapshot. Set `GITHUB_TOKEN` to
raise the API rate limit:

```bash
GITHUB_TOKEN=github_pat_... python3 -m bounty_hunter.cli --algora-org cal
```

The token is only sent to `api.github.com`; it is never passed to marketplace
pages or stored in reports.

Successful public GitHub responses are cached for six hours in
`data/github-cache.json`. If GitHub enrichment is temporarily unavailable, a
prior enriched snapshot may be shown for context, but it is explicitly marked
stale, receives zero ranking priority, and cannot become eligible until a fresh
request succeeds.

Algora board HTML is cached for six hours in `data/algora-cache`, and uncached
board requests are spaced apart to respect platform rate limits. A failed
refresh may display a prior board snapshot for continuity, but stale Algora
evidence is marked explicitly, assigned zero payout probability, and cannot be
queued without a deliberate rejection override.

TaskBounty summary and task-detail responses are cached for six hours in
`data/taskbounty-cache`. A failed refresh retains prior inventory for context,
marks the affected evidence stale, and forces a zero payout probability until
fresh marketplace evidence is available again.

Opire's public rewards page is cached for six hours in
`data/opire-cache.html`. Its initial top-priced page exposes issue URLs, reward
amounts, languages, repository visibility, and solver counts. Reports show both
the fetched count and Opire's reported available count, and mark the source
partial when the server-rendered page is not the full inventory. Opire's
[reward documentation](https://docs.opire.dev/rewards/introduction) says pending
rewards are combined into checkout when claimed and each reward creator pays
for their own reward. Because that is creator payment after resolution rather
than public evidence of escrow, the initial policy records these opportunities
but rejects them until payout assurance can be verified. The payment model and
documentation URL are preserved in each opportunity's evidence. Implausible
rewards above $10,000 are also marked as unverified outliers and skipped during
GitHub enrichment.

Boss's public open-bounty API is cached for six hours in
`data/boss-cache.json`. Boss documents a 5% plus $0.60 earner fee and limits
payouts to residents of 23 listed countries. Set `--boss-payout-country` to a
two-letter residency code to evaluate that restriction; omission or an
unsupported country fails closed. Boss also documents that an `auto` bounty
charges the sponsor's card only after the issue is closed, while the public
inventory does not expose bounty type or proof of escrow. Boss opportunities
are therefore retained for discovery but receive zero payout probability under
the initial policy, even when the country is supported.

BountyPay's public API is cached for six hours in
`data/bountypay-cache.json`. The adapter distinguishes real Base mainnet
funding from Base Sepolia, Mezo Testnet, and unknown chains. Testnet records are
reported for launch monitoring but always receive zero payout probability.
Any future mainnet record must remain present at its approved amount in a fresh
API response during preflight and again immediately before publication. The
current public inventory contains no live mainnet bounty, so this source does
not yet create an actionable attempt.

When several Algora sponsors fund the same GitHub issue, their rows are pooled
into one opportunity and the reward amounts are summed. The report preserves
each underlying bounty ID and amount as evidence, avoiding duplicate attempts
and calculating expected value against the combined reward.

The live source requires outbound HTTPS access. To run entirely offline with an
enriched fixture:

```bash
python3 -m bounty_hunter.cli \
  --no-taskbounty \
  --input examples/enriched-opportunities.json
```

Reports are written to:

- `reports/opportunities.md`
- `reports/opportunities.json`

Each scan is also appended to `data/bounty_hunter.db`. View experiment totals:

```bash
python3 -m bounty_hunter.workflow summary
```

### Public telemetry dashboard

The [public telemetry dashboard](https://bounty-hunter-telemetry.vercel.app) is
a read-only view of deliberately sanitized scan telemetry. It cannot claim an
issue, approve or start an attempt, push a branch, open a pull request, or
publish a submission. Those actions remain behind the existing hash-bound
human approval workflow.

Generate the allowlisted public snapshot and build the static site locally:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m bounty_hunter.public_export
npm run build
```

The exporter reads the local report, monitor heartbeat, and historical ledger,
then writes `public/telemetry.json`. It publishes aggregate operating metrics,
source health, rejection reasons, and a bounded list of near misses. It does not
publish raw monitor logs, queue identifiers, approval commands or digests,
internal evidence blobs, local paths, or credentials.

For a daily inventory snapshot, edit `config/algora_orgs.txt` and run:

```bash
sh scripts/daily_scan.sh
```

Algora boards are deliberately trusted-list driven. A weekly discovery audit
uses GitHub's exact `💎 Bounty` label only as a candidate hint, then checks each
candidate's public Algora board for direct, parseable reward rows:

```bash
sh scripts/weekly_algora_discovery.sh
```

The audit writes `reports/algora-board-audit.json`. It never edits
`config/algora_orgs.txt`: a label can be copied or abused. Fresh, parseable
board results are instead added to the next daily scan as scan-only sources for
up to eight days. They still require fresh GitHub evidence and must pass every
normal repository, competition, scope, payout, and reputation gate before they
can enter the human-review queue.

For continuous VPS monitoring, run the lock-protected supervisor:

```bash
python3 -m bounty_hunter.monitor --interval-hours 6
```

Configured Algora board evidence is cached for 24 hours. Each scan attempts at
most three live board refreshes per cache pool, so expired organizations rotate
through refreshes across scans instead of producing a synchronized request
burst. Deferred or rate-limited caches remain visible as stale and are always
ineligible until refreshed. Publication still performs a zero-cache funding
check immediately before any external submission.

Public GitHub evidence is also cached for 24 hours and limited to 45 live API
requests per scan. If that local budget is exhausted, prior evidence is shown
as explicitly stale and the affected opportunity is rejected until a later
scan rotates it through refresh. Preflight and publication use separate fresh
checks and do not inherit monitor-cache confidence.

It writes an atomic heartbeat to `data/monitor-status.json`, captures bounded
scan output, prevents overlapping monitor processes, and retries failures with
exponential backoff. By default it also runs the Algora candidate-board audit
when its report is older than seven days; an audit failure is recorded but does
not suppress the primary scan. After a successful scan it queues at most one
highest-ranked eligible record in the local ledger, deduplicates against any
unfinished attempt, and stops before approval, cloning, or execution. Use
`--no-auto-queue` to disable this handoff. If a still-unapproved queued record
loses eligibility on a later scan, the monitor withdraws it and may queue the
new top candidate; this zero-work withdrawal is excluded from outcome and cost
calibration. Human-approved or started attempts are never withdrawn
automatically. The heartbeat includes evaluated and
eligible counts, the queued attempt ID and status, plus an `attention_required`
flag, allowing an external dashboard or notifier to surface the human review
checkpoint without granting execution or submission authority. A hardened example service is provided at
`config/systemd/bounty-hunter.service.example`; replace its deployment path,
optionally provide `GITHUB_TOKEN` through `/etc/bounty-hunter.env`, then install
and enable it with the VPS service manager.

The ledger distinguishes scan observations from distinct marketplace
opportunities and records the first and most recent scan timestamps.

## Supervised attempt workflow

Queueing and approval are separate actions. A queued attempt cannot transition
to `running` until a human records explicit approval:

```bash
python3 -m bounty_hunter.workflow queue \
  --source algora \
  --source-id BOUNTY_ID

python3 -m bounty_hunter.workflow review ATTEMPT_ID

python3 -m bounty_hunter.workflow approve ATTEMPT_ID \
  --record-sha256 SHA256_FROM_REVIEW_OUTPUT \
  --note "Issue, approach, expected diff, and submission policy reviewed."
```

For a real eligible opportunity, do not transition it to `running` manually.
Run the bound preflight after approval:

```bash
python3 -m bounty_hunter.preflight ATTEMPT_ID
```

The queued attempt stores an immutable copy of the opportunity and score. The
review command emits that exact record, its canonical SHA-256 digest, and the
corresponding approval command. Approval stores the digest, and preflight
recomputes it before any runtime use, so approval cannot silently transfer to a
different or modified opportunity snapshot. The
preflight derives its public GitHub URL from that snapshot, refuses rejected,
closed, private, or stale evidence, then refreshes marketplace funding and
GitHub repository, issue, claim, and linked-PR evidence before creating any
workspace or starting a container. A failed freshness check is recorded as a
hashed `preflight_validation` artifact and abandons the attempt without runtime
cost. A passing validation is also hash-recorded in preflight provenance.
Preflight then creates a fresh non-overwriting workspace,
clones with a digest-pinned container image, records the exact commit, prepares
only allowlisted dependencies, and runs the existing verification suite with
networking disabled. Success leaves the attempt in `running`; failure retains a
hashed preflight report and abandons the attempt.

After preflight, generate a patch only after refreshing issue, claim, and linked
pull-request evidence:

```bash
python3 -m bounty_hunter.attempt patch ATTEMPT_ID
```

Codex still receives only sanitized text context in a separate read-only control
directory. Dependency manifests are immutable after preflight. A malformed or
non-applying diff receives at most one context-only repair pass and must then
pass the same path, size, protected-file, and `git apply --check` validators.

Verify the generated patch independently:

```bash
python3 -m bounty_hunter.attempt verify ATTEMPT_ID
```

Verification creates an isolated checkout of the original commit, transfers
only recognizable regression-test files from the patch, and runs the full
verification plan against both trees without network access. The original must
fail and the patched tree must pass before the immutable review bundle can move
the attempt to `verified`.

After local verification, public submission requires a second approval:

```bash
python3 -m bounty_hunter.workflow transition ATTEMPT_ID verified

python3 -m bounty_hunter.workflow approve-submission ATTEMPT_ID \
  --note "Final diff, tests, PR description, and repository policy reviewed." \
  --bundle-sha256 SHA256_FROM_REVIEW_BUNDLE

python3 -m bounty_hunter.submission prepare ATTEMPT_ID

python3 -m bounty_hunter.submission publish ATTEMPT_ID \
  --bundle-sha256 SHA256_FROM_REVIEW_BUNDLE \
  --package-sha256 SHA256_FROM_SUBMISSION_PACKAGE \
  --confirm-public-submission \
  publish:ATTEMPT_ID:SHA256_FROM_REVIEW_BUNDLE:SHA256_FROM_SUBMISSION_PACKAGE
```

Preparation clones the approved base into a separate local checkout, applies
the immutable patch, disables Git hooks, commits it, and proves that the commit
diff has the approved patch hash. It also creates a hash-bound PR body with the
issue link, verification evidence, and AI-assistance disclosure. Preparation
does not contact GitHub.

After inspecting the prepared commit and PR body, the human confirmation names
both the review-bundle digest and submission-package digest. Publication then
requires an authenticated `gh` session. Before creating a fork or pushing, it
refreshes the supported marketplace and requires the approved reward to remain
open and at least as large, then confirms that the GitHub repository is still
public and active, the issue is open and unlocked, and no linked open competing
PR appeared after approval. It creates or reuses the user's fork, pushes only
the prepared branch, opens the upstream PR, records the returned URL, and
appends the fresh funding and GitHub snapshots to the immutable publication
receipt. Stale or unavailable marketplace evidence, a wrong digest, altered
package, drifted checkout, closed issue, new competing PR, or incomplete
confirmation fails before any push. The agent never receives GitHub or
marketplace credentials.

If a PR was submitted manually outside this command, the lower-level
`workflow transition ... submitted --pr-url ...` operation remains available
for ledger reconciliation, but it cannot prove the remote PR contains the
approved patch and is therefore not the preferred path.

Rejected opportunities cannot be queued accidentally. The workflow supports an
explicit `--override-rejection` flag for deliberate manual investigation, but
approval is still required before execution.

The attempt state machine records verification, submission, revision, merge,
payout, rejection, and abandonment. Every nonzero model cost, compute cost,
human-time entry, and payout is appended to an immutable event ledger and also
added to the attempt total. Revision expenses therefore accumulate rather than
replacing the original attempt cost.

Record an expense or review-time entry independently of a state transition:

```bash
python3 -m bounty_hunter.workflow record-event ATTEMPT_ID agent_cost_usd 4.25 \
  --note "Codex patch generation and first revision"

python3 -m bounty_hunter.workflow record-event ATTEMPT_ID human_hours 0.20 \
  --note "Final diff review"

python3 -m bounty_hunter.workflow events ATTEMPT_ID
```

The experiment summary reports direct costs, valued human cost, cash net income,
economic net income, ROI, net income per measured attempt, net income per human
hour, observed win rate, and the remaining payout needed to break even.
Economic net uses the same human-hour value as the opportunity model. The pilot
remains `collecting_data` until it has at least ten measured terminal attempts;
only then can it report `positive_net_income`, and only when both cash net and
economic net are positive. Untouched queue withdrawals and freshness failures
do not count toward the sample, preventing a lucky first payout or zero-work
screening rejection from deciding viability.

Completed attempt outcomes also calibrate future payout probabilities. The
scanner compares observed merged/paid outcomes with the probabilities stored in
each attempt's immutable score snapshot and applies a bounded Bayesian
observed-to-expected factor. A ten-attempt prior prevents early failures or wins
from causing large ranking swings; the factor and sample size are included in
every opportunity report. The same prior blends observed agent/compute cost and
human review time into the defaults used by subsequent expected-value and
hourly-return calculations.

Attempt status changes are also append-only audit events. Agent runs
automatically record input, cached-input, output, and reasoning token counts
when Codex reports them:

```bash
python3 -m bounty_hunter.workflow states ATTEMPT_ID
python3 -m bounty_hunter.workflow metrics ATTEMPT_ID
```

Verification produces an immutable review bundle containing the binary Git
patch, working-tree status, base commit, bounded test logs, verification
command, container image reference, failing-before-fix regression evidence, and
SHA-256 digest. Submission approval is
rejected unless it references the latest bundle digest, preventing approval of
one diff followed by submission of a different diff.

Each verification and agent run is versioned (`review-1`, `review-2`,
`agent-1.patch`, `agent-2.patch`, and so on), so reviewer-requested changes
cannot overwrite the artifacts behind an earlier approval. After recording the
submitted PR as `revision`, save the public reviewer feedback and run:

```bash
python3 -m bounty_hunter.attempt revise ATTEMPT_ID \
  --feedback-file reviewer-feedback.txt

python3 -m bounty_hunter.attempt verify ATTEMPT_ID
```

Revision generation first verifies that the local working tree still matches
the exact submitted bundle. The revised complete diff must pass the same
dependency protections and failing-before/passing-after proof, followed by a
new `approve-submission` call for the new bundle hash.

## Isolation policy

Untrusted repository code is never executed directly on the host. The execution
layer requires Docker or Podman and fails closed when neither is installed.
Generated container commands:

- Drop all Linux capabilities.
- Enable `no-new-privileges`.
- Enforce CPU, memory, process, and wall-clock limits.
- Run as an unprivileged user.
- Use a read-only container filesystem.
- Disable network access whenever repository code is executed.
- Suppress Python bytecode generation and reject generated cache artifacts from
  review bundles.
- Mount only the isolated attempt workspace.
- Reject SSH, local, private, and non-GitHub clone URLs.

The local validation environment uses Docker through Colima. The execution
layer still fails closed if Docker or Podman is unavailable.

Validate the complete lifecycle against a controlled bug fixture:

```bash
python3 scripts/validate_isolated_lifecycle.py
```

The validation requires the regression test to fail against the original code,
pass against the patched code, and produce a verified immutable review bundle.

Validate the isolated public-repository acquisition path separately:

```bash
python3 scripts/validate_public_clone.py
```

To validate the patch-generation boundary with a real ephemeral Codex session:

```bash
python3 scripts/validate_isolated_lifecycle.py --use-codex-agent
```

The agent receives bounded text context in a separate temporary directory using
a read-only sandbox. It has no target-repository filesystem access and receives
no GitHub, marketplace, wallet, or submission credentials. Its output must be a
size- and path-validated standard Git diff that passes `git apply --check`, and
the resulting tree must still pass the independent container verifier.

Validate npm and Python dependency preparation against controlled public
packages:

```bash
python3 scripts/validate_dependency_preparation.py
```

This validation confirms that dependency outputs remain separate from the
checkout and that both verification commands pass after networking is removed.

Validate the complete post-preflight orchestration with a real ephemeral Codex
session and Docker verifier:

```bash
python3 scripts/validate_attempt_pipeline.py
```

This fixture begins with a passing existing suite, generates a fix and
regression test, proves the regression fails on the original commit, proves the
patched tree passes, and checks that the final status is `verified`.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Decision model

The initial model uses explicit, inspectable rules rather than an LLM-generated
score:

```text
payout probability =
  P(bounty valid)
  × P(solution verified)
  × P(solution wins and merges)

expected value =
  net payout × payout probability
  − execution cost
  − valued human review time
  − revision reserve
  − risk reserve

expected hourly return =
  expected value / total agent-and-human hours
```

Unknown evidence lowers confidence and is shown in the report. It is never
silently treated as a positive signal. The initial reputation-safe policy also
requires a repository to be at least 180 days old, have at least 10 stars, have
an identifiable open-source license, and expose `CONTRIBUTING`, a pull-request
template, a code of conduct, or equivalent repository guidance. Preflight
re-scores the immutable queued snapshot against the current policy so an older
approval cannot bypass tightened safeguards.

GitHub issue bodies and comments are also screened for explicit bounty
withdrawal and repository-owner AI-contribution hostility. Strong signals are
fail-closed and store only compact author/URL provenance, not copied comment
text. The same check runs during fresh preflight enrichment and immediately
before publication, preventing a stale marketplace listing or changed
maintainer policy from causing an unwanted PR.

Rejected records remain in the report for audit, but they are ordered by
nonzero payout probability, then the number of primary gate blockers, expected
value, and confidence. Primary blockers are reported separately from the three
downstream economic threshold failures. Missing required public/open GitHub
evidence forces payout probability to zero, so unenriched rows cannot masquerade
as near misses.

## Next milestone

1. Continue daily snapshots and measure eligible opportunity supply over at
   least one week.
2. Expand configured Algora organizations without weakening selection rules.
3. Add safe validators for another package-manager lock format only when its
   origin, integrity, and no-build guarantees can be enforced.
4. Run the first real supervised attempt only when an open, uncontested,
   testable bounty clears the expected-value threshold.
5. Record actual model cost, compute cost, review time, acceptance result, and
   payout before tuning the scoring model.

## Earnings accounting conventions

The all-in-one Windows desktop's **Overview** shows payouts, gross and
cost-adjusted hourly rates, direct costs, work hours and merged but unconfirmed
offers. It uses the same local `data/bounty_hunter.db` ledger as the CLI.

- **Recorded payouts** are amounts manually confirmed as received, not
  automatically reconciled bank/wallet transfers. Advertised bounty offers
  and outstanding merged PRs are **not income**.
- **Gross hourly rate** is recorded payouts divided by logged human work hours.
  **Net hourly rate** additionally subtracts recorded agent and compute costs,
  but not taxes, unrecorded expenses or the monetary value of your time.
  When no hours are logged, the rates show an em dash rather than a false zero.
- **Merged, unconfirmed** is an estimated remainder of advertised rewards on
  merged work. It is a tentative comparison figure, not a confirmed receivable.
- **Record Activity** accepts hours worked and payouts actually received,
  with an audit note and an explicit payout confirmation.
- **Payouts / Withdraw** opens the relevant provider's website and never
  initiates a transfer or asks for private keys. Follow provider-specific
  payment, identity, age and country requirements.

The unified desktop settings are stored at
`%APPDATA%\\BountyHunter\\desktop.json`. Older desktop settings in
`app-settings.json` are imported on upgrade, and a corrupt desktop settings
file is preserved separately so the setup wizard can reopen. A packaged
Windows startup failure writes a diagnostic to
`%LOCALAPPDATA%\\BountyHunter\\startup-error.log`, which contains
exception types and stack locations, not secrets.
