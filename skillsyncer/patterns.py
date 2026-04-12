"""Built-in regex patterns for secret detection."""

# Order matters: more specific patterns come first so the scanner's
# overlap dedup picks the precise label ("Anthropic API key") over
# the generic catch-all ("API key").
BLOCK_PATTERNS = [
    # ----- AI provider keys (high-precision prefixes) -----
    {
        "pattern": r"sk-ant-[A-Za-z0-9_\-]{40,}",
        "label": "Anthropic API key",
    },
    {
        "pattern": r"sk-proj-[A-Za-z0-9_\-]{40,}",
        "label": "OpenAI project key",
    },
    {
        "pattern": r"AIza[0-9A-Za-z_\-]{35}",
        "label": "Google API key",
    },
    {
        "pattern": r"xai-[A-Za-z0-9]{40,}",
        "label": "xAI API key",
    },
    {
        "pattern": r"gsk_[A-Za-z0-9]{40,}",
        "label": "Groq API key",
    },
    {
        "pattern": r"hf_[A-Za-z0-9]{30,}",
        "label": "HuggingFace token",
    },
    {
        "pattern": r"r8_[A-Za-z0-9]{30,}",
        "label": "Replicate API token",
    },
    {
        "pattern": r"pplx-[A-Za-z0-9_\-]{30,}",
        "label": "Perplexity API key",
    },
    # ----- Cloud / VCS / chat tokens -----
    {
        "pattern": r"AKIA[0-9A-Z]{16}",
        "label": "AWS access key",
    },
    {
        "pattern": r"ghp_[A-Za-z0-9]{36}",
        "label": "GitHub personal access token",
    },
    {
        "pattern": r"xox[bpoas]-[A-Za-z0-9\-]{10,}",
        "label": "Slack token",
    },
    # ----- Generic catch-alls (run last so specific labels win) -----
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
        "pattern": r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----",
        "label": "Private key",
    },
]

ALLOW_PATTERNS = [
    r"\$\{\{[A-Z_][A-Z0-9_]*\}\}",  # SkillSyncer placeholders
]
