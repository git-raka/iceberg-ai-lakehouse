import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = (
    f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@postgres:5432/superset"
)
WTF_CSRF_ENABLED = True
FEATURE_FLAGS = {"DASHBOARD_RBAC": True}

# The AI assistant panel on the dashboard is an <iframe> to the local Data/AI API (localhost:8000).
# Allow exactly that element in Markdown components; everything else stays sanitized.
HTML_SANITIZATION = True
HTML_SANITIZATION_SCHEMA_EXTENSIONS = {
    "tagNames": ["iframe"],
    "attributes": {"iframe": ["src", "width", "height", "style", "title", "loading"]},
}
# Demo on localhost only. For anything shared, keep Talisman on and add the API origin to frame-src.
TALISMAN_ENABLED = False
