#!/usr/bin/env python3
"""Quickstart entry point. Run:  python run.py"""
from app import app

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    print()
    print(f"  AskMyCFO · The Ledger")
    print(f"  Running on http://localhost:{port}")
    print(f"  Ctrl-C to stop.")
    print()
    app.run(host="0.0.0.0", port=port, debug=False)
