# Working on this project with Claude

You talk to Claude in a browser. Claude writes code and pushes it to GitHub. The
rig pulls it down over its own cellular connection. You never have to use git on
the command line.

This page assumes you have never used GitHub before. Follow it literally.

## The loop, in one picture

```
  You, in a browser                GitHub                    The rig
  ─────────────────                ──────                    ───────
  "make the END button
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

Three things you do: ask, merge, apply.

## Step 1: ask for a change

Open a Claude session pointed at this repository and describe what you want in
plain language. Useful things to include:

* What you were doing when you noticed the problem.
* What you expected versus what happened.
* Anything from the Recent events panel on the dashboard, or from
  `sudo journalctl -u viabot-survey -n 50`, copied and pasted.

Good: "During the run this morning the banner stayed on NO DATA the whole time
even though the router was online. The events panel said
`ping: SO_BINDTODEVICE`."

Less good: "it's broken".

## Step 2: merge the pull request

When Claude finishes it gives you a link like:

```
https://github.com/erikbahl-afk/claude-code-viabot/pull/7
```

Open it. You will see a Conversation tab describing what changed and why, a
Files changed tab with the actual edits (old lines red, new lines green), and a
green Merge pull request button.

Read the description. If it sounds right, press Merge pull request, then Confirm
merge. That puts the change on the `main` branch, which is the branch the rig
follows.

Nothing reaches the rig until you press Merge. An open pull request is a
proposal, not a change.

If it looks wrong, do not merge. Say so in the Claude session instead.

To undo a merge, press Revert on the pull request page. That opens another pull
request which undoes it. Merge that one too, then apply the update as normal.

## Step 3: apply it on the rig

1. Join the rig's Wi-Fi with your phone.
2. Open the dashboard.
3. Make sure no run is in progress. Updating is blocked during a run.
4. Open Software update.
5. Press Check for updates. It fetches over the cellular link, so give it a few
   seconds. You will see the list of new commits.
6. Press Apply update.

The rig pulls the code, reinstalls anything new, restarts itself, and the page
reloads on its own after ten to twenty seconds.

If the phone cannot reach the rig afterwards, wait thirty seconds and reload. If
it still does not come back, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-rig-did-not-come-back-after-an-update).

The same thing over SSH, if you would rather:

```bash
ssh viabot@garage-surveyor-01.local
cd ~/claude-code-viabot
./scripts/update.sh
```

## Step 4: only when the change touches the server

Apply update updates the rig and nothing else. The survey server is a different
machine. `server/viabot_receiver.py` runs from `/opt/viabot-receiver` on the
Dallas box and the rig's update never touches it. A change under `server/` needs
a second step or it will not take, and the symptom is that the thing you just
merged behaves exactly as before.

```bash
ssh root@viabotsurveys.com
cd ~/claude-code-viabot && git pull && sudo ./server/setup.sh --domain viabotsurveys.com
```

Safe to re-run. It leaves the secrets alone unless you pass `--rotate-secrets`,
which you almost never want, because rotating means updating every rig too.

A pull request that changes anything under `server/` should say so.

Reports already published do not change. A finished upload is final, which is
deliberate so that re-analysing a run cannot overwrite an upload in flight. An
improvement to the report page reaches the next walk's report, not the ones
already on the server.

## Things worth knowing

**`main` is what the rig runs.** Claude develops on a separate branch so
half-finished work cannot reach the rig by accident. Merging is the moment a
change becomes real.

**Your settings survive updates.** `config/config.yaml` and everything under
`data/` are ignored by git and never overwritten. An update is a hard reset of
the tracked files only.

**Hand edits do not survive.** If you SSH in and edit a Python file directly,
the next update discards it. The dashboard warns you when this would happen. Ask
Claude to make the change properly instead.

**Updates need the cellular link.** The rig fetches from GitHub through the
router's modem. Update indoors with a decent signal, not in the basement of the
garage you are about to survey.

**Update before you go, not during.** Applying an update restarts the service.
Get the rig onto the version you want, confirm it works, then drive to site.

## Vocabulary

| Term | What it means here |
|---|---|
| repository (repo) | This project, all its files and their history |
| commit | One saved change, with a message explaining it |
| branch | A parallel line of work. `main` is the one the rig follows |
| pull request (PR) | A proposal to merge a branch into `main`. What you review and merge |
| merge | Accepting a pull request, putting its commits on `main` |
| fetch / pull | Downloading new commits from GitHub. What Check for updates does |
| revert | A new commit that undoes an earlier one. How you back out a bad change |

## If you get stuck

Copy the error into a Claude session. Useful things to include:

```bash
sudo systemctl status viabot-survey               # is it running?
sudo journalctl -u viabot-survey -n 100           # what did it say before it stopped?
cd ~/claude-code-viabot && git log --oneline -5   # what version is on the rig?
curl -s http://192.168.50.1/api/events?limit=200  # the event log as JSON
```
