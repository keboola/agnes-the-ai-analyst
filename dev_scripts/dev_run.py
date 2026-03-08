#!/usr/bin/env python3
"""
Development server with auth bypass.
Run this instead of main app for local testing without Google OAuth.
"""

import os
from webapp.app import create_app
from flask import session, redirect, url_for

# Set development environment variables
os.environ["FLASK_DEBUG"] = "true"
os.environ["WEBAPP_SECRET_KEY"] = "dev-secret-key"

# Create app
app = create_app()

# Add dev login bypass route
@app.route("/dev-login")
def dev_login():
    """Dev-only login bypass - sets session without OAuth."""
    if not app.config["DEBUG"]:
        return "Dev login only available in DEBUG mode", 403

    # Set fake user session
    session["user"] = {
        "email": "dev@example.com",
        "name": "Dev User",
        "picture": ""
    }
    return redirect(url_for("dashboard"))

@app.route("/dev-catalog")
def dev_catalog():
    """Dev-only direct access to catalog - bypasses account check."""
    if not app.config["DEBUG"]:
        return "Dev catalog only available in DEBUG mode", 403

    # Set fake user session if not already set
    if "user" not in session:
        session["user"] = {
            "email": "dev@example.com",
            "name": "Dev User",
            "picture": ""
        }

    # Redirect directly to catalog
    return redirect(url_for("catalog"))

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🔧 DEV SERVER - Auth bypass enabled")
    print("="*60)
    print("📍 Server running at: http://127.0.0.1:5000")
    print("🔓 Quick access links:")
    print("   → http://127.0.0.1:5000/dev-login    (Dashboard)")
    print("   → http://127.0.0.1:5000/dev-catalog  (Direct to Catalog - RECOMMENDED)")
    print("="*60 + "\n")

    app.run(debug=True, host="127.0.0.1", port=5000)
