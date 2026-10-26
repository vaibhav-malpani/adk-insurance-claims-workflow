POLICIES = {
    "POL-1001": {"holder": "Asha Rao", "active": True, "covers": ["auto_collision", "theft"], "limit": 20000, "deductible": 500},
    "POL-2002": {"holder": "Ben Carter", "active": True, "covers": ["water_damage", "fire"], "limit": 80000, "deductible": 1000},
    "POL-3003": {"holder": "Chen Wei", "active": True, "covers": ["auto_collision", "theft"], "limit": 30000, "deductible": 750},
    "POL-4004": {"holder": "Dana Fox", "active": False, "covers": ["auto_collision"], "limit": 15000, "deductible": 500},
}

REQUIRED_DOCS = {
    "auto_collision": ["police_report", "photos"],
    "theft": ["police_report"],
    "water_damage": ["photos", "plumber_invoice"],
    "fire": ["fire_dept_report", "photos"],
}

CLAIM_HISTORY = {
    "POL-1001": [],
    "POL-2002": [{"type": "water_damage", "days_ago": 400}],
    "POL-3003": [
        {"type": "theft", "days_ago": 20},
        {"type": "auto_collision", "days_ago": 45},
        {"type": "theft", "days_ago": 90},
    ],
}

# Used in the demo so a "high-risk" claim looks suspicious without external services.
SUSPICIOUS_KEYWORDS = ["cash", "no witnesses", "total loss", "brand new", "no receipt", "lost the original"]
