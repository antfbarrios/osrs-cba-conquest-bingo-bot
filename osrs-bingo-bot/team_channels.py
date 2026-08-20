"""
Maps each team's channel to that team's name.

/submit is now run FROM a team's own channel -- the bot figures out which
team is submitting based on where the command was used, so players no
longer type a team name at all.

--- HOW TO ADD A TEAM ---
1. Right-click the team's channel -> Copy Channel ID (Developer Mode must
   be on: User Settings -> Advanced -> Developer Mode).
2. Add a line below: channel_id_as_a_number: "Team Name"

Only channels listed here will accept /submit -- if someone runs it
somewhere else, the bot tells them to use it in their team's channel.
"""

TEAM_CHANNELS = {
    # 123456789012345678: "Team 1",
}
