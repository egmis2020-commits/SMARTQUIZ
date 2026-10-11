"""Non-destructive Turso connectivity test for EGMIS V24.4 Phase 1.

Reads credentials from environment variables and executes SELECT 1 only.
It does not create tables or change database contents.
"""
import os
import sys


def main() -> int:
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        print("MISSING CONFIG: Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in this terminal session.")
        return 2

    try:
        import turso_serverless
    except Exception as exc:
        print(f"DRIVER ERROR: Could not import turso_serverless ({type(exc).__name__}).")
        print("Install it with: py -3.14 -m pip install turso_serverless")
        return 3

    conn = None
    try:
        conn = turso_serverless.connect(url, auth_token=token)
        cursor = conn.execute("SELECT 1")
        row = cursor.fetchone()
        if not row or row[0] != 1:
            print("UNEXPECTED RESPONSE: Connected, but SELECT 1 did not return 1.")
            return 4
        print("TURSO CONNECTION OK")
        print("Remote URL configured: yes (credential value hidden)")
        print("Read-only smoke test: SELECT 1 passed")
        print("Note: this does not test the full quiz app or migrate data.")
        return 0
    except Exception as exc:
        print(f"TURSO CONNECTION FAILED: {type(exc).__name__}: {str(exc)[:240]}")
        print("Check the database URL, token permissions, network access, and installed driver version.")
        return 5
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
