from fastapi import HTTPException

ALLOWED_CHILDREN = {
    "superadmin": {"master_agent"},
    "master_agent": {"agent"},
    "agent": {"player"},
    "player": set(),
}

VALID_ROLES = set(ALLOWED_CHILDREN.keys())

def validate_role_value(role: str) -> str:
    if not role or role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    return role

def validate_child_creation(parent_role: str, child_role: str) -> None:
    parent_role = validate_role_value(parent_role)
    child_role = validate_role_value(child_role)

    allowed = ALLOWED_CHILDREN[parent_role]
    if child_role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"{parent_role} cannot create {child_role}"
        )
