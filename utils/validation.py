"""
Input validation utilities.
"""


def validate_limit(value, default=50, max_val=200):
    try:
        v = int(value)
        return max(1, min(v, max_val))
    except (TypeError, ValueError):
        return default


def validate_offset(value, default=0):
    try:
        v = int(value)
        return max(0, v)
    except (TypeError, ValueError):
        return default


def validate_string_length(value, max_length=5000, field_name='field'):
    if value and len(str(value)) > max_length:
        return None, f'{field_name} je prilis dlhy (max {max_length} znakov)'
    return str(value) if value else '', None
