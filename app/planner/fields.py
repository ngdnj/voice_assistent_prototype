"""Field rules shared by tasks, meetings and reminders."""

MAX_DESCRIPTION_LENGTH = 2000
MAX_NAME_LENGTH = 200


def check_name_length(name: str, field: str) -> None:
    """Applied when a record is created; restoring stored records does not re-check it."""
    if isinstance(name, str) and len(name.strip()) > MAX_NAME_LENGTH:
        raise ValueError(f"{field} must contain at most {MAX_NAME_LENGTH} characters")


def normalize_description(value: str) -> str:
    """Strip outer whitespace; an empty description is allowed."""
    if not isinstance(value, str):
        raise TypeError("description must be a string")
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"description must contain at most {MAX_DESCRIPTION_LENGTH} characters")
    return description


def description_fingerprint_data(description: str) -> dict[str, str]:
    """Omit an empty description so operations stored before the field existed still replay."""
    description = normalize_description(description)
    return {"description": description} if description else {}
