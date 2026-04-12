"""Built-in regex patterns for secret detection."""

BLOCK_PATTERNS = [
    {
        "pattern": r"(?:sk-|key-|token-|api[_\-]?key)[a-zA-Z0-9_\-]{8,}",
        "label": "API key",
    },
    {
        "pattern": r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}",
        "label": "Bearer token",
    },
    {
        "pattern": r"https?://[^${}\s]+:[^${}\s]+@",
        "label": "Credentials in URL",
    },
    {
        "pattern": r"AKIA[0-9A-Z]{16}",
        "label": "AWS access key",
    },
    {
        "pattern": r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----",
        "label": "Private key",
    },
    {
        "pattern": r"ghp_[A-Za-z0-9]{36}",
        "label": "GitHub personal access token",
    },
    {
        "pattern": r"xox[bpoas]-[A-Za-z0-9\-]{10,}",
        "label": "Slack token",
    },
]

ALLOW_PATTERNS = [
    r"\$\{\{[A-Z_][A-Z0-9_]*\}\}",  # SkillSyncer placeholders
]
