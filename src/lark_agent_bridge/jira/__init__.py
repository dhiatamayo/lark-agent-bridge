"""Read-only Jira (Atlassian Cloud) connector.

Query issues via JQL for ticket triage / cleanup insight. Read-only by design:
it only searches and reads issues; it never transitions, edits, or creates.
Credentials (email + API token) live in the encrypted SecretStore, never in
plaintext config.
"""
