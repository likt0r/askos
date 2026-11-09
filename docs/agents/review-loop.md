# Review loop

Every PR an agent opens starts as a draft and leaves draft (`gh pr ready`) only through this loop. Agents never merge. The **driver** is the session that runs it, usually the one that opened the PR.

A **round** is one review, its verification and its fixes. A round is **clean** when it confirms no finding of medium or high severity, leaving out defects `main` already has outside the PR's scope, which go under "Left" ([Fix](#a-round)); its low findings are fixed in one commit, without another round. A round whose medium or high finding stays open as a question for the owner is not clean: the pass stops on that question, after that round's fixes, and the PR stays a draft. A low open question stops nothing and blocks nothing; it goes to the owner in the summary. A **pass** is a run of rounds that ends at the first clean round, or **stops**: on a medium or high open question, after 5 rounds without a clean one, or after 2 reviewers in a row without a usable report ([Verify](#a-round)). A new pass counts its rounds from 1. It starts with any commit after a clean round other than its low fixes, and after a pass that stopped, with the owner's answer or any commit.

## Steps

1. Open the PR as a draft: `gh pr create --draft`. A PR opened as non-draft by mistake: `gh pr ready <pr> --undo` first.
2. Run [rounds](#a-round) until one is clean. A pass that stops skips to step 4.
3. Run the tests once. When a test fails, post this pass's summary (step 4) first, then commit the fix: a new pass, back to step 2.
   - **A PR that leaves `web/ui/` alone:** the full suite, `python3 -m unittest discover -s tests`.
   - **A PR that changes `web/ui/`:** first its UI tests, in a VM ([ADR 0003](../adr/0003-no-node-on-the-host.md)): `cd web/ui && npm ci && npm run prepare:nuxt && npm test && npm run generate`, where `generate` also checks `dist/` (`scripts/check-dist.mjs`). `dist/` is not committed ([web/ui/README.md](../../web/ui/README.md)); CI builds it and its Python tests serve that build. Then the full suite, which serves the local `dist/` (without one, as on the Host, those tests skip); when a UI test failed, skip it.

     The Host has no Node; in a VM, Node is installed. On the Host, run only the full suite, list the UI tests as not run yet, post the summary (step 4) and leave the PR a draft. The owner has a session in a VM run this step on the PR's head and post the result as a follow-up comment, not a new summary: when the tests pass, that session goes on with step 5; when one fails, its fix starts a new pass with its own summary.
4. Post the pass's [summary](#summary) as a new comment, `gh pr comment <pr> --body-file <file>`; earlier summaries stay as they are. Ask its open questions in the session too.
5. Go ready only when the pass ended clean, the tests of step 3 passed, and no medium or high question is open. Check that `gh pr view <pr> --json headRefOid --jq .headRefOid` prints the commit the clean round reviewed, or its low fixes on top of it, then `gh pr ready <pr>`. Another commit is unreviewed: back to step 2. A PR that stays a draft waits for the owner: tell them what is left and which decisions you need.
6. A commit lands after `gh pr ready`: `gh pr ready <pr> --undo`, then back to step 2.

## A round

1. **Review.** Start a reviewer with fresh context that can read files and run commands: a new subagent (in Claude Code, `general-purpose`), never a fork or an earlier reviewer, which carry earlier context. Reviewing the diff yourself is not a round. Its prompt is the [reviewer brief](#reviewer-brief) from this file on `main`, re-read every round (`git fetch origin main && git show origin/main:docs/agents/review-loop.md`), so a PR cannot weaken its own review; until `main` has this file, take it from the PR.
   Without subagents, start a new non-interactive session with the brief on stdin, allowed to run `gh`, `git` and the tests:
   - `claude -p --allowedTools "Bash(gh *)" "Bash(git *)" "Bash(python3 -m unittest *)" < brief.md`;
   - `codex exec`, `opencode run`: grant the equivalent. opencode's `--auto` approves every permission not explicitly denied, edits included.
2. **Verify.** A report that does not name the PR's head commit, or whose reviewer could not read the full diff, is not a round: start a new reviewer. After 2 such reviewers in a row, the pass stops. Check every finding against the code, with a test that goes red on it where one can. Mark each finding:
   - **confirmed**: the finding is true (the failure can happen, the test is missing or weak, the doc contradicts the code, the convention is broken);
   - **refuted**: it is not, with the reason; a finding against a settled decision in `<spec>` is refuted as already decided;
   - **open question**: a design question, an unclear spec, a trade-off, or a defect whose fix needs one of these. The owner decides these: note the severity, the options and your recommendation. A medium or high one stops the pass after this round's fixes; a low one only goes into the summary.
3. **Fix.** Fix every confirmed finding and keep its red test as the regression test. A defect `main` already has is fixed when it lies in the PR's scope; otherwise it goes under "Left" in the summary and does not count against a clean round. Run the tests the fix affects, commit, push; the full suite runs once, at the end of the pass ([step 3](#steps)). Commit messages describe the change, not the findings: the next reviewer reads `git log`. The next round starts with a new reviewer, unless the pass ends or [stops](#review-loop): a clean round ends it after its low fixes; a medium or high open question, round 5 without a clean one, or a 2nd unusable reviewer in a row stops it, and step 4 follows.

## Reviewer brief

Copy the quoted text below without its `>` markers, replace `<pr>`, `<dir>` and `<spec>`, and change nothing else; the only addition is a check the owner asked for on this PR. Earlier findings, fixes and opinions about the code stay out, so the reviewer is unanchored.

- `<dir>`: the checkout of the PR's branch;
- `<spec>`: the acceptance criteria of every issue the PR's description names with `Closes`, `Fixes`, `Resolves` or `Part of` ([how they close](issue-tracker.md#closing-issues-from-pull-requests)), copied word for word from `gh issue view <issue> --comments`, or the owner's request word for word when it names none; then every decision the owner made on this PR, each marked "settled".

> Review PR #<pr>, checked out in `<dir>`: its full diff against its base branch, `main` for most PRs (`gh pr diff <pr>`), its description (`gh pr view <pr>`), and this spec: <spec>. A PR that says `Part of` an issue covers only the criteria its description claims. Take every point marked "settled" as given, not as a finding. First read `AGENTS.md`, `GLOSSARY.md`, `docs/agents/` and the ADRs in `docs/adr/` that touch the changed area. Run single tests where they settle a finding; I run the full suite. Look for:
>
> - correctness bugs;
> - security holes: this project is a security boundary (the egress Allowlist, credentials, Strong Signals that only the CLI may lift, with `--force`);
> - gaps against the spec;
> - missing or weak tests;
> - docs that contradict the code;
> - violations of the repo's conventions.
>
> Name the commit you reviewed and any command you could not run, and say if you could not read the full diff. Report each finding with file:line, a concrete failure scenario, a severity (high, medium, low) and your confidence (high, medium, low), or say there are none. Report to me only: read and run what you need, but edit, commit, push and comment nothing; I do all of that.

## Summary

One comment per pass, posted when the pass ends or stops. Replace each `<...>`, and leave out a section with nothing in it. The first line says how the pass ended: `clean`, `stopped on an open question`, `stopped after 5 rounds without a clean one` or `stopped, 2 reviewers in a row gave no usable report`.

```
Review loop, pass <n>: <how it ended>
Rounds: <count>

Round 1 (findings: <count>)
- fixed: <finding> (<commit>)
- refuted: <finding>: <reason>
- open question: <finding>
Round 2, clean (low findings: <count>)
- fixed: <finding> (<commit>)

Tests
- full suite: passed | failed: <test>; its fix starts the next pass | not run, a UI test failed | not run, the pass stopped
- UI tests: passed | failed: <test>; its fix starts the next pass | not run yet, they need a VM

Open questions
- <question> (<severity>): options A, B; recommended: A, because <reason>

Left
- <finding>: fixed in <commit>, not reviewed
- <finding>: not fixed
- <finding>: already on main, outside this PR
```

"Tests" picks one result per line; the UI tests line only when the PR changes `web/ui/`. "Left" lists, when a pass stopped, each finding of its last completed round as "fixed in <commit>, not reviewed" or "not fixed", and in any pass the defects `main` already has outside the PR's scope.
