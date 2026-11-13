#!/usr/bin/env python3
"""Test the new mac-compute auth logic WITHOUT touching the real auth.json.

Run ON the Mac, in ~/mac-compute/:
    .venv/bin/python test_auth.py        (or python3 test_auth.py)

- Monkey-patches auth_store.AUTH_FILE to /tmp/test-auth.json and
  /tmp/test-auth2.json, runs create/verify checks, deletes them.
- Asserts the real ~/mac-compute/auth.json still does not exist
  afterwards (this script never creates it).
- Prints PASS/FAIL per check; exit 0 only if all pass.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth_store

REAL_FILE = os.path.expanduser("~/mac-compute/auth.json")
TMP1 = "/tmp/test-auth.json"
TMP2 = "/tmp/test-auth2.json"

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name}")


def cleanup():
    for p in (TMP1, TMP2):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def main():
    cleanup()
    real_existed_before = os.path.exists(REAL_FILE)
    print(f"real auth.json exists before test: {real_existed_before}")

    # --- temp file 1: normal create/verify flow ---
    auth_store.AUTH_FILE = TMP1
    check("no credentials before setup", not auth_store.credentials_exist())
    check("verify fails with no file",
          not auth_store.verify_credentials("u", "somepassword123"))
    check("verify_password fails with no file",
          not auth_store.verify_password("somepassword123"))
    auth_store.create_credentials("tester", "correct-horse-123")
    check("credentials exist after setup", auth_store.credentials_exist())
    check("verify correct username+password",
          auth_store.verify_credentials("tester", "correct-horse-123"))
    check("reject wrong password",
          not auth_store.verify_credentials("tester", "wrong-password"))
    check("reject wrong username",
          not auth_store.verify_credentials("other", "correct-horse-123"))
    check("verify_password accepts the password",
          auth_store.verify_password("correct-horse-123"))
    check("verify_password rejects wrong password",
          not auth_store.verify_password("nope"))
    check("verify_password rejects empty string",
          not auth_store.verify_password(""))
    try:
        auth_store.create_credentials("tester2", "another-pass-123")
        check("second create raises", False)
    except RuntimeError:
        check("second create raises", True)
    st = os.stat(TMP1)
    check("temp auth file mode is 600", (st.st_mode & 0o777) == 0o600)

    # --- temp file 2: short password rejected, no file left behind ---
    auth_store.AUTH_FILE = TMP2
    try:
        auth_store.create_credentials("tester", "short")
        check("short password rejected", False)
    except ValueError:
        check("short password rejected", True)
    check("no file created by rejected setup", not os.path.exists(TMP2))
    try:
        auth_store.create_credentials("", "long-enough-password")
        check("empty username rejected", False)
    except ValueError:
        check("empty username rejected", True)

    cleanup()
    check("temp files cleaned up",
          not os.path.exists(TMP1) and not os.path.exists(TMP2))
    check("real auth.json still absent after test",
          os.path.exists(REAL_FILE) == real_existed_before
          and not real_existed_before)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
