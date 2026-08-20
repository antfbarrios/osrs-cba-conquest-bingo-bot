"""
Structural column layout of the live Google Sheet, per region. These are
COLUMN LETTERS only (stable even if rows get inserted/deleted for new
drops) -- the bot looks up the actual ROW at runtime by matching Source +
Drop text, so adding/removing/reordering drop rows in the sheet is safe
and does not require regenerating this file.

Only regenerate this if the sheet's overall block/column structure changes
(a region added/removed, or columns reordered).
"""

REGION_COLUMNS = {
    'Asgarnia': {'source_col': 'B', 'drop_col': 'C', 'points_per_col': 'D', 'obtained_col': 'E', 'points_gained_col': 'F', 'header_row': 4},
    'Desert': {'source_col': 'H', 'drop_col': 'I', 'points_per_col': 'J', 'obtained_col': 'K', 'points_gained_col': 'L', 'header_row': 4},
    'Fremennik': {'source_col': 'N', 'drop_col': 'O', 'points_per_col': 'P', 'obtained_col': 'Q', 'points_gained_col': 'R', 'header_row': 4},
    'Kandarin': {'source_col': 'T', 'drop_col': 'U', 'points_per_col': 'V', 'obtained_col': 'W', 'points_gained_col': 'X', 'header_row': 4},
    'Kourend': {'source_col': 'Z', 'drop_col': 'AA', 'points_per_col': 'AB', 'obtained_col': 'AC', 'points_gained_col': 'AD', 'header_row': 4},
    'Misthalin': {'source_col': 'AF', 'drop_col': 'AG', 'points_per_col': 'AH', 'obtained_col': 'AI', 'points_gained_col': 'AJ', 'header_row': 4},
    'Morytania': {'source_col': 'AL', 'drop_col': 'AM', 'points_per_col': 'AN', 'obtained_col': 'AO', 'points_gained_col': 'AP', 'header_row': 4},
    'Tirannwn': {'source_col': 'AR', 'drop_col': 'AS', 'points_per_col': 'AT', 'obtained_col': 'AU', 'points_gained_col': 'AV', 'header_row': 4},
    'Varlamore': {'source_col': 'AX', 'drop_col': 'AY', 'points_per_col': 'AZ', 'obtained_col': 'BA', 'points_gained_col': 'BB', 'header_row': 4},
    'Wilderness': {'source_col': 'BD', 'drop_col': 'BE', 'points_per_col': 'BF', 'obtained_col': 'BG', 'points_gained_col': 'BH', 'header_row': 4},
}
