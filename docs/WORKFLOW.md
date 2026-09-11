# Working on this project with Claude

You talk to Claude in a browser. Claude writes code and pushes it to GitHub. The
rig pulls it down over its own cellular connection. You never have to use git on
the command line.

This page assumes you have never used GitHub before. Follow it literally.

---

## The loop, in one picture

```
  You, in a browser                GitHub                    The rig
  ─────────────────                ──────                    ───────
  "make the MARK button
   bigger"            ──────►  Claude pushes a
                               branch and opens
                               a pull request
                                      │
  You press "Merge"  ◄──────────────  │
         │
         └──────────────────────►  main branch
                                   updated
                                        │
                                        └──────►  You press
                                                  "Check for updates"
                                                  then "Apply update"
                                                  on the dashboard
```

Three things you do: **ask**, **merge**, **apply**. Nothing else.

---

## Step 1 — Ask for a change

Open a Claude session pointed at this repository and describe what you want in
plain language. Useful things to include:

- What you were doing when you noticed the problem.
- What you expected versus what happened.
- Anything from the **Recent events** panel on the dashboard, or from
  `sudo journalctl -u viabot-survey -n 50`, copied and pasted.

Good: *"During the run this morning the banner stayed on NO DATA the whole
time even though the router was online. The events panel said `ping: SO_BINDTODEVICE`."*

Less good: *"it's broken"*.

## Step 2 — Merge the pull request

When Claude finishes it will give you a link that looks like:

```
https://github.com/erikbahl-afk/claude-code-viabot/pull/7
```

Open it. You will see:

- **Conversation** — a description of what changed and why.
- **Files changed** — the actual edits, old lines in red, new lines in green.
- A green **Merge pull request** button.

Read the description. If it sounds right, press **Merge pull request**, then
**Confirm merge**. That is what puts the change on the `main` branch, which is
the branch the rig follows.

> Nothing reaches the rig until you press Merge. An open pull request is a
> proposal, not a change.

If it looks wrong, don't merge — say so in the Claude session instead.

**Undoing a merge:** on the pull request page, press **Revert**. That opens
another pull request which undoes it; merge that one too. Then apply the update
on the rig as normal.

## Step 3 — Apply it on the rig

1. Join the rig's Wi-Fi with your phone.
2. Open the dashboard.
3. Make sure no run is in progress — updating is blocked during a run.
4. Open **Software update**.
5. Press **Check for updates**. It fetches over the cellular link, so give it a
   few seconds. You will see the list of new commits.
6. Press **Apply update**.

The rig pulls the code, reinstalls anything new, restarts itself, and the page
reloads on its own after ten to twenty seconds.

If the phone can't reach the rig afterwards, wait thirty seconds and reload. If
it still doesn't come back, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-rig-didnt-come-back-after-an-update).

### The manual equivalent

Same thing, over SSH, if you would rather:

```bash
ssh viabot@garage-surveyor-01.local
cd ~/claude-code-viabot
./scripts/update.sh
```

---

## Things worth knowing

**`main` is what the rig runs.** Claude develops on a separate branch so that
half-finished work can never reach the rig by accident. Merging is the moment a
change becomes real.

**Your settings survive updates.** `config/config.yaml` and everything under
`data/` are ignored by git and are never overwritten. An update is a hard reset
of the *tracked* files only.

**But hand edits do not survive.** If you SSH in and edit a Python file
directly, the next update discards it. The dashboard warns you ("local edits
present") when this would happen. Ask Claude to make the change properly
instead.

**Updates need the cellular link.** The rig fetches from GitHub through the
router's modem. Update indoors with a decent signal, not in the basement of the
garage you are about to survey.

**Update before you go, not during.** Applying an update restarts the service.
Get the rig onto the version you want, confirm it works, then drive to the site.

---

## Vocabulary

| Term | What it means here |
|---|---|
| **repository** (repo) | This project — all its files and their history. |
| **commit** | One saved change, with a message explaining it. |
| **branch** | A parallel line of work. `main` is the one the rig follows. |
| **pull request** (PR) | A proposal to merge a branch into `main`. What you review and merge. |
| **merge** | Accepting a pull request, putting its commits on `main`. |
| **fetch / pull** | Downloading new commits from GitHub. What "Check for updates" does. |
| **revert** | A new commit that undoes an earlier one. How you back out a bad change. |

## If you get stuck

Copy the error and paste it into a Claude session. Genuinely useful things to
include:

```bash
sudo systemctl status viabot-survey          # is it running?
sudo journalctl -u viabot-survey -n 100      # what did it say before it stopped?
cd ~/claude-code-viabot && git log --oneline -5   # what version is on the rig?
```
