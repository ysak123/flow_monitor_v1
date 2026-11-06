# Flow tabs, triage and debugging — design

## Intent
A PD engineer carries many blocks across flows (APR, LEC, later STA). The page exists to get
from "something is wrong" to "here is the error line" fast, while healthy runs stay quiet.

## Goals
- One tab per flow (APR, LEC); STA later by adding one flow entry plus a remote scanner.
- Categories are per tab. A path pasted into a tab must be that flow, or a plain log folder/file (tagged "Log").
- Triage: a "Needs attention" strip at the top of each tab (failed, stopped mid-flow, silent, path problems).
- Debugging: log viewer with Live tail / Errors / Warnings views over the *whole* file, unique messages
  with counts, click a message to see context around it, step through occurrences.
- Stage runtimes: duration of each finished stage, elapsed time of the running one.
- Copy buttons: log path and run directory, for the terminal.
- Appearance: Dark, Light, Follow Windows, plus a custom accent colour.

## Non-goals
STA scanning (unknown flow yet). Editing or rerunning anything on the host.

## Decisions (user)
Log folders live in the tab they're added to; categories per tab; all four debugging aids; accent-colour custom theme.

## Design
### Data
`S.flows = {apr:{cats:[]}, lec:{cats:[]}}`, `S.tab`. A `FLOWS` registry lists each tab: id, name, which
item types it accepts. Migration from v2: APR/log items go to the APR tab, LEC items to the LEC tab,
same category names.

### Remote agent
- `scan` items carry `flow`; detection only tries that flow's resolver, then "log". If the path is clearly
  another flow (e.g. LEC in the APR tab), return a hint: "This is a LEC run; add it in the LEC tab."
- `grep {path, kind: error|warning}`: whole-file scan, returns unique messages (message text with
  numbers/names normalised to group duplicates), count, first line, and up to 200 line numbers each.
- `range {path, start, count}`: lines around a given line number for the context view.

### Triage strip
Per tab, above categories, only when non-empty: one compact line per problem block
(name, category, what's wrong, since when) with one action: "Show errors" for failed, "Open log" otherwise.
States: Failed, Stopped (IDLE with stale stage: job likely died), Silent (log folder over threshold), Path problem.

### Runtimes (APR)
Stage duration = its .pass time minus the previous stage's .pass time (first stage unknown).
Running stage elapsed = server time minus previous stage's .pass time. Shown under each via and in the row.

### Log viewer (drawer)
Toolbar: segmented Live tail | Errors (n) | Warnings (n), filter, wrap, copy path.
- Live tail: as today.
- Errors/Warnings: list of unique messages with counts; selecting one shows ±40 lines of context
  with the hit highlighted and "previous / next occurrence (k of n)".
- Opening a failed stage lands on Errors; a running stage lands on Live tail.

## Visual (frontend-design)
Keep the "layout viewer" identity, calm density. Accent drives running state, focus and primary buttons.
- Dark: oxide #13232A, die #192D35, line #29434D, text #E4EEF0, muted #8FA9B0, accent copper #E0A45A,
  pass #7FCB8F, fail #E36B6B.
- Light ("wafer"): base #EEF3F4, panel #FFFFFF, line #D3DEE1, text #14262C, muted #567077,
  accent #B06A1F, pass #2D8650, fail #C23B38. Cool grey-white, not cream.
- Tabs: plain text tabs under the title with an underline in accent and a small count of problems.
- Theme control: sun/moon toggle in the top bar; full choice (Dark/Light/Windows, accent picker, reset) in Settings.

## Left to the builder
Exact normalisation regex for grouping messages, spacing, copy feedback.
