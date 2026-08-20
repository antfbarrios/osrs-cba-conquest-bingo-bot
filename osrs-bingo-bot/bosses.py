"""
Boss list used to power autocomplete suggestions on /submit.

Right now this is just a flat list -- typing still accepts free text for
both boss_name and drop_name, this only supplies suggestions as the user
types.

--- HOW TO ADD BOSS -> DROP FILTERING LATER ---
When you're ready to lock drop_name down to only the valid drops for a
given boss, replace the flat list below with a dict, e.g.:

    BOSS_DROPS = {
        "Zulrah": ["Tanzanite fang", "Magic fang", "Serpentine visage", "Zulrah's scales"],
        "Vorkath": ["Vorkath's head", "Draconic visage", "Skeletal visage"],
        ...
    }

Then in main.py:
  1. Change `BOSSES = list(BOSS_DROPS.keys())` for the boss_name autocomplete.
  2. In the drop_name autocomplete function, look up
     `BOSS_DROPS.get(interaction.namespace.boss_name, [])` instead of `[]`
     to only suggest (and you can also validate/reject) drops for the
     boss the user already typed in that same command invocation.
"""

BOSSES = [
    "Zulrah", "Vorkath", "Cerberus", "Kraken", "Thermonuclear Smoke Devil",
    "Alchemical Hydra", "Grotesque Guardians", "Abyssal Sire",
    "General Graardor", "Kril Tsutsaroth", "Commander Zilyana", "K'ril Tsutsaroth",
    "Nex", "Corporeal Beast", "King Black Dragon", "Giant Mole", "Kalphite Queen",
    "Dagannoth Kings", "Chaos Elemental", "Chaos Fanatic", "Crazy Archaeologist",
    "Scorpia", "Venenatis", "Vet'ion", "Callisto", "Skotizo",
    "Theatre of Blood", "Chambers of Xeric", "Tombs of Amascut",
    "Barrows", "Wintertodt", "Tempoross", "Zalcano", "Phantom Muspah",
    "The Nightmare", "Phosani's Nightmare", "TzKal-Zuk", "TzTok-Jad",
    "Sarachnis", "Obor", "Bryophyta", "Dagannoth Rex", "Duke Sucellus",
    "The Leviathan", "The Whisperer", "Vardorvis", "Amoxliatl",
]
