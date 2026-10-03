import os
import sys
from dotenv import load_dotenv
import psycopg2

load_dotenv()

def init_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Error: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to database to initialize schema...")
    try:
        conn = psycopg2.connect(database_url)
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()

        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        conn.close()
        print("Schema successfully initialized!")
    except Exception as e:
        print(f"Failed to initialize database schema: {e}", file=sys.stderr)

if __name__ == "__main__":
    init_db()
