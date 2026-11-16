#!/usr/bin/env python3
"""Belfast Associate allocator for the Monday 'Result Tracking' board (1811483126).

Pure local logic - no network. Reads two files that the scheduled task fetches:
  --items   one or more JSON files from monday get_board_items_page (today's items,
            columns lookup_mkq5h5wt [Core] and text_mm8k3xhd [Belfast Associate])
  --roster  JSON of the NMD News Rota artifact doc state/roster
            (data.eu[N][Day] / data.us[N][Day], N = 1..6, Day = Mon..Fri)

plan   : writes <out>/batch_NN.json (<=40 monday updates each) + <out>/plan.json
verify : re-reads fresh items and compares them with <out>/plan.json

Rules (agreed with Ross):
  * scope = items created today (UK date)
  * shares: W5 25%, W6 25%, W1-W4 12.5% each (a workflow with nobody on it hands its
    share to the others in its group: 1-4 / 5-6)
  * all of a company's items today go to the SAME workflow (grouped by the Company link
    column connect_boards_mkmpc2eq; items with no company link are treated alone). A group's
    size counts against its workflow's share, so shares are approximate when groups are big
  * core companies fill workflows in the order 5, 6, 4, 3, 2, 1 up to each share
  * non-core fill the remaining capacity by smooth weighted round-robin, oldest group first
  * cell text: "N - EU name, US name" (just the names that are present)
Exit codes: 0 ok, 2 bad input, 3 weekend, 4 nobody on the rota, 5 verify mismatch.
"""
import argparse, datetime, json, os, sys
from zoneinfo import ZoneInfo

COL, CORE_COL = "text_mm8k3xhd", "lookup_mkq5h5wt"
COMPANY_COL = "connect_boards_mkmpc2eq"   # "Company" board-relation column; its linked item id = company
PRIORITY = [5, 6, 4, 3, 2, 1]
GROUP_R, GROUP_A = [5, 6], [1, 2, 3, 4]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
UK = ZoneInfo("Europe/London")
BLANKS = {"", "—", "-", "–"}


def load_items(paths):
    seen, out = set(), []
    for p in paths:
        data = json.load(open(p))
        for it in (data["items"] if isinstance(data, dict) else data):
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            cv = it.get("column_values") or {}
            rel = cv.get(COMPANY_COL)
            company = str(rel[0]["id"]) if isinstance(rel, list) and rel and isinstance(rel[0], dict) and rel[0].get("id") else None
            out.append({
                "company": company,
                "id": str(it["id"]),
                "name": it.get("name", ""),
                "created": datetime.datetime.fromisoformat(it["created_at"].replace("Z", "+00:00")),
                "core": (cv.get(CORE_COL) or "").strip() == "Core",
                "current": (cv.get(COL) or "").strip(),
            })
    out.sort(key=lambda i: (i["created"], int(i["id"])))
    return out


def load_roster(path):
    r = json.load(open(path))
    return r.get("data", r)


def names_for(roster, n, day):
    def pick(side):
        v = ((roster.get(side) or {}).get(str(n)) or {}).get(day) or ""
        v = v.strip()
        return "" if v in BLANKS else v
    return pick("eu"), pick("us")


def cell_text(roster, n, day):
    parts = [p for p in names_for(roster, n, day) if p]
    return f"{n} - " + ", ".join(parts) if parts else ""


def targets(total, active):
    w = {n: 0.0 for n in range(1, 7)}
    ra = [n for n in GROUP_R if active[n]]
    aa = [n for n in GROUP_A if active[n]]
    half_r, half_a = 0.5, 0.5
    if not ra:
        half_r, half_a = 0.0, 1.0
    if not aa:
        half_r, half_a = 1.0, 0.0
    for n in ra:
        w[n] = half_r / len(ra)
    for n in aa:
        w[n] = half_a / len(aa)
    raw = {n: w[n] * total for n in w}
    t = {n: int(raw[n]) for n in raw}
    order = sorted((n for n in raw if w[n] > 0), key=lambda n: (-(raw[n] - t[n]), PRIORITY.index(n)))
    for n in order[: total - sum(t.values())]:
        t[n] += 1
    return t


def groups_of(items):
    """Items grouped by company, in order of each group's oldest item. No company -> alone."""
    groups, index = [], {}
    for it in items:
        key = it.get("company") or ("item:" + it["id"])
        if key not in index:
            index[key] = len(groups)
            groups.append([])
        groups[index[key]].append(it)
    return groups


def allocate(items, active):
    tgt = targets(len(items), active)
    remaining, assign = dict(tgt), {}
    groups = groups_of(items)
    for g in groups:                                  # core groups first, priority order
        if any(i["core"] for i in g):
            pool = [n for n in PRIORITY if remaining[n] > 0]
            if not pool:                              # shares overshot: workflow with most room left
                pool = [m for m in PRIORITY if tgt[m] > 0]
                pool = [max(pool, key=lambda m: (remaining[m], -PRIORITY.index(m)))]
            n = pool[0]
            for i in g:
                assign[i["id"]] = n
            remaining[n] -= len(g)
    cap = {n: c for n, c in remaining.items() if c > 0}
    score = {n: 0.0 for n in cap}
    for g in groups:                                  # non-core: smooth weighted round-robin per item
        if any(i["core"] for i in g):
            continue
        pick = None
        for _ in g:
            live = [n for n in cap if remaining[n] > 0]
            if pick is None or pick not in live:
                if not live:                          # shares overshot by big groups: use most room left
                    live = [n for n in tgt if tgt[n] > 0]
                    pick = max(live, key=lambda n: (remaining[n], -PRIORITY.index(n)))
                    remaining[pick] -= 1
                    continue
                for n in live:
                    score[n] += cap[n]
                pick = max(live, key=lambda n: (score[n], -PRIORITY.index(n)))
                score[pick] -= sum(cap[n] for n in live)
            else:                                     # same workflow again: advance the round-robin as if picked again
                for n in live:
                    score[n] += cap[n]
                score[pick] -= sum(cap[n] for n in live)
            remaining[pick] -= 1
        if pick is None:
            continue
        for i in g:
            assign[i["id"]] = pick
    return assign, tgt


def existing_wf(it, active):
    """Workflow number already written in the cell ("N - ..."), if it is an active one."""
    head = it["current"].split(" - ", 1)[0].strip()
    if " - " in it["current"] and head.isdigit() and active.get(int(head)):
        return int(head)
    return None


def allocate_topup(items, active):
    """Keep cells that already name an active workflow; give only the rest a workflow,
    aiming at the same shares as a full plan (existing items count towards them).
    A new item whose company already has an item on a workflow joins that workflow."""
    tgt = targets(len(items), active)
    assign, remaining = {}, dict(tgt)
    new, company_wf = [], {}
    for it in items:
        n = existing_wf(it, active)
        if n:
            assign[it["id"]] = n
            remaining[n] -= 1
            if it.get("company"):
                company_wf.setdefault(it["company"], n)
        else:
            new.append(it)
    live_ns = [n for n in tgt if tgt[n] > 0]
    for g in groups_of(new):
        n = company_wf.get(g[0].get("company")) if g[0].get("company") else None
        if n is None:
            if any(i["core"] for i in g):
                pool = [m for m in PRIORITY if remaining[m] > 0]
                n = pool[0] if pool else None
            if n is None:
                pool = [m for m in live_ns if remaining[m] > 0] or live_ns
                n = max(pool, key=lambda m: (remaining[m] / tgt[m], -PRIORITY.index(m)))
        for i in g:
            assign[i["id"]] = n
            remaining[n] -= 1
    return assign, tgt


def uk_today(override):
    now = datetime.datetime.now(UK)
    if override:
        now = datetime.datetime.fromisoformat(override).replace(tzinfo=UK)
    return now.date()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "verify"])
    ap.add_argument("--items", nargs="+", required=True)
    ap.add_argument("--roster")
    ap.add_argument("--out", required=True)
    ap.add_argument("--blanks-only", action="store_true",
                    help="plan only: leave cells that already name an active workflow, fill the rest")
    ap.add_argument("--today", help="override UK date YYYY-MM-DD (testing)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    today = uk_today(a.today)
    day = DAYS[today.weekday()]

    items = load_items(a.items)
    in_scope = [i for i in items if i["created"].astimezone(UK).date() == today]
    skipped = len(items) - len(in_scope)

    if a.mode == "verify":
        plan = {p["id"]: p for p in json.load(open(os.path.join(a.out, "plan.json")))["items"]}
        bad = [(i["id"], plan[i["id"]]["target"], i["current"]) for i in in_scope
               if i["id"] in plan and i["current"] != plan[i["id"]]["target"]]
        missing = [i for i in plan if i not in {x["id"] for x in in_scope}]
        print(f"verify: {len(plan)} planned, {len(bad)} mismatched, {len(missing)} not returned")
        for b in bad[:20]:
            print("  MISMATCH id=%s want=%r have=%r" % b)
        sys.exit(5 if bad or missing else 0)

    if day in ("Sat", "Sun"):
        print(f"{today} is a {day}: nothing to do")
        sys.exit(3)
    if not a.roster:
        print("plan needs --roster"); sys.exit(2)
    roster = load_roster(a.roster)
    active = {n: any(names_for(roster, n, day)) for n in range(1, 7)}
    if not any(active.values()):
        print("Nobody is on the rota for", day, "- refusing to touch the board"); sys.exit(4)
    if not in_scope:
        print("No items created today - nothing to do"); sys.exit(0)

    assign, tgt = (allocate_topup if a.blanks_only else allocate)(in_scope, active)
    plan_items, updates = [], []
    for it in in_scope:
        n = assign[it["id"]]
        text = it["current"] if a.blanks_only and existing_wf(it, active) else cell_text(roster, n, day)
        plan_items.append({"id": it["id"], "name": it["name"], "core": it["core"], "wf": n,
                           "target": text, "current": it["current"]})
        if it["current"] != text:
            updates.append({"itemId": int(it["id"]),
                            "columnValues": json.dumps({COL: text if text else None}, ensure_ascii=False)})
    for f in os.listdir(a.out):
        if f.startswith("batch_"):
            os.remove(os.path.join(a.out, f))
    for k in range(0, len(updates), 40):
        json.dump(updates[k:k + 40], open(os.path.join(a.out, f"batch_{k // 40 + 1:02d}.json"), "w"), ensure_ascii=False)
    json.dump({"date": str(today), "day": day, "items": plan_items}, open(os.path.join(a.out, "plan.json"), "w"),
              ensure_ascii=False, indent=1)

    total = len(in_scope)
    print(f"{today} ({day}): {total} items in scope, {sum(i['core'] for i in in_scope)} core, "
          f"{skipped} returned items not created today (ignored)")
    print("wf  names                 target  core  non-core  total  share")
    for n in range(1, 7):
        core = sum(1 for p in plan_items if p["wf"] == n and p["core"])
        non = sum(1 for p in plan_items if p["wf"] == n and not p["core"])
        nm = ", ".join(x for x in names_for(roster, n, day) if x) or "(nobody)"
        print(f" {n}  {nm:<20} {tgt[n]:>6} {core:>5} {non:>9} {core + non:>6}  {(core + non) / total:6.1%}")
    print(f"{len(updates)} cells need changing, {total - len(updates)} already correct, "
          f"{(len(updates) + 39) // 40} batch file(s) in {a.out}")


if __name__ == "__main__":
    main()
