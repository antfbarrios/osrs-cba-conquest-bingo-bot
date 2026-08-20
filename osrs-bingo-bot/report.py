"""
Generates a fun stats report from bingo.db -- no external dependencies,
just run: python report.py

Produces a markdown file (drops_report_YYYY-MM-DD_HHMM.md) covering:
  - Overall totals
  - Most farmed sources
  - Most active submitters
  - Mod approval speed (fastest / slowest / average, per-mod counts)
  - Submission timing patterns (hour of day, day of week)
  - Team head-to-head totals & rejection rates

Safe to run anytime, as many times as you like -- it only reads bingo.db,
never writes to it.
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DB_PATH = Path(__file__).parent / "bingo.db"
TOP_N = 10

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def bar(count, max_count, width=30):
    if max_count == 0:
        return ""
    filled = round((count / max_count) * width)
    return "█" * filled + "░" * (width - filled)


def load_rows():
    if not DB_PATH.exists():
        raise SystemExit(f"No bingo.db found at {DB_PATH} -- run the bot at least once first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM submissions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_report(rows) -> str:
    lines = []
    now = datetime.now(timezone.utc)
    lines.append(f"# Bingo Drops Report")
    lines.append(f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')} from {len(rows)} total submissions_")
    lines.append("")

    approved = [r for r in rows if r["status"] == "approved"]
    rejected = [r for r in rows if r["status"] == "rejected"]
    pending = [r for r in rows if r["status"] == "pending"]

    # --- Overall totals ---
    lines.append("## Overall")
    lines.append("")
    lines.append(f"- **Total submissions:** {len(rows)}")
    lines.append(f"- **Approved:** {len(approved)}")
    lines.append(f"- **Rejected:** {len(rejected)}")
    lines.append(f"- **Still pending:** {len(pending)}")
    if rows:
        lines.append(f"- **Overall approval rate:** {len(approved) / len(rows) * 100:.1f}%")
    lines.append("")

    # --- Most farmed sources ---
    lines.append("## Most Farmed Sources")
    lines.append("")
    source_counts = defaultdict(int)
    for r in approved:
        source_counts[r["boss_name"]] += 1
    top_sources = sorted(source_counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    if top_sources:
        max_count = top_sources[0][1]
        lines.append("| Source | Approved drops | |")
        lines.append("|---|---|---|")
        for source, count in top_sources:
            lines.append(f"| {source} | {count} | `{bar(count, max_count)}` |")
    else:
        lines.append("_No approved submissions yet._")
    lines.append("")

    # --- Most active submitters ---
    lines.append("## Most Active Submitters")
    lines.append("_Ranked by approved submissions._")
    lines.append("")
    submitter_counts = defaultdict(int)
    for r in approved:
        submitter_counts[r["submitter_name"]] += 1
    top_submitters = sorted(submitter_counts.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    if top_submitters:
        max_count = top_submitters[0][1]
        lines.append("| Submitter | Approved drops | |")
        lines.append("|---|---|---|")
        for submitter, count in top_submitters:
            lines.append(f"| {submitter} | {count} | `{bar(count, max_count)}` |")
    else:
        lines.append("_No approved submissions yet._")
    lines.append("")

    # --- Mod approval speed ---
    lines.append("## Mod Approval Speed")
    lines.append("")
    durations = []  # (seconds, row)
    mod_review_counts = defaultdict(int)
    for r in rows:
        if r["status"] in ("approved", "rejected") and r["reviewed_by"]:
            mod_review_counts[r["reviewed_by"]] += 1
            submitted = parse_iso(r["submitted_at"])
            reviewed = parse_iso(r["reviewed_at"])
            if submitted and reviewed:
                durations.append(((reviewed - submitted).total_seconds(), r))

    if durations:
        durations.sort(key=lambda d: d[0])
        fastest_secs, fastest_row = durations[0]
        slowest_secs, slowest_row = durations[-1]
        avg_secs = sum(d[0] for d in durations) / len(durations)

        lines.append(
            f"- **Fastest review:** {human_duration(fastest_secs)} "
            f"({fastest_row['boss_name']} / {fastest_row['drop_name']}, {fastest_row['team_name']})"
        )
        lines.append(
            f"- **Slowest review:** {human_duration(slowest_secs)} "
            f"({slowest_row['boss_name']} / {slowest_row['drop_name']}, {slowest_row['team_name']})"
        )
        lines.append(f"- **Average review time:** {human_duration(avg_secs)}")
        lines.append("")

        lines.append("**Reviews per mod:**")
        lines.append("")
        top_mods = sorted(mod_review_counts.items(), key=lambda kv: kv[1], reverse=True)
        max_count = top_mods[0][1]
        lines.append("| Mod | Reviews | |")
        lines.append("|---|---|---|")
        for mod, count in top_mods:
            lines.append(f"| {mod} | {count} | `{bar(count, max_count)}` |")
    else:
        lines.append("_No reviewed submissions with timestamps yet._")
    lines.append("")

    # --- Timing patterns ---
    lines.append("## Submission Timing Patterns")
    lines.append("")
    hour_counts = defaultdict(int)
    day_counts = defaultdict(int)
    for r in rows:
        submitted = parse_iso(r["submitted_at"])
        if submitted:
            hour_counts[submitted.hour] += 1
            day_counts[submitted.weekday()] += 1

    if hour_counts:
        lines.append("**By hour of day (UTC):**")
        lines.append("")
        lines.append("```")
        max_count = max(hour_counts.values())
        for h in range(24):
            count = hour_counts.get(h, 0)
            lines.append(f"{h:02d}:00  {bar(count, max_count, width=40)}  {count}")
        lines.append("```")
        lines.append("")

        lines.append("**By day of week:**")
        lines.append("")
        lines.append("```")
        max_count = max(day_counts.values())
        for d in range(7):
            count = day_counts.get(d, 0)
            lines.append(f"{DAY_NAMES[d]:<10}{bar(count, max_count, width=40)}  {count}")
        lines.append("```")
    else:
        lines.append("_No timestamped submissions yet._")
    lines.append("")

    # --- Team head-to-head ---
    lines.append("## Team Head-to-Head")
    lines.append("")
    team_stats = defaultdict(lambda: {"approved": 0, "rejected": 0, "pending": 0})
    for r in rows:
        team_stats[r["team_name"]][r["status"]] += 1

    if team_stats:
        lines.append("| Team | Approved | Rejected | Pending | Total | Rejection rate |")
        lines.append("|---|---|---|---|---|---|")
        for team, stats in sorted(team_stats.items(), key=lambda kv: kv[1]["approved"], reverse=True):
            total = stats["approved"] + stats["rejected"] + stats["pending"]
            reviewed = stats["approved"] + stats["rejected"]
            rejection_rate = f"{stats['rejected'] / reviewed * 100:.1f}%" if reviewed else "—"
            lines.append(
                f"| {team} | {stats['approved']} | {stats['rejected']} | "
                f"{stats['pending']} | {total} | {rejection_rate} |"
            )
    else:
        lines.append("_No submissions yet._")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    rows = load_rows()
    report = build_report(rows)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = Path(__file__).parent / f"drops_report_{timestamp}.md"
    out_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"\n\nSaved to {out_path}")
