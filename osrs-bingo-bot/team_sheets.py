"""
Maps each team name to the ID of THEIR OWN Google Sheet (each team has a
separate copy of the bingo sheet, not one shared sheet).

--- HOW TO SET THIS UP ---
1. Open a team's sheet in the browser and look at the URL:
   docs.google.com/spreadsheets/d/THIS_PART/edit
2. Copy the ID (the long string between /d/ and /edit).
3. Add an entry below: "Exact Team Name": "the_sheet_id"
4. Make sure that sheet is shared with your service account's email as an
   Editor (same email you used for the original sheet setup) -- each
   team's sheet needs to be shared individually, sharing one doesn't grant
   access to the others.

The keys here must exactly match the team names in team_channels.py.
The worksheet TAB NAME (e.g. "Sheet4") is assumed to be the same across
every team's sheet -- set that once via GOOGLE_WORKSHEET_NAME in .env.
If a team's sheet uses a different tab name, let me know and we can make
this per-team too.
"""

TEAM_SHEETS = {
    # "Team 1": "your_sheet_id_here",
}
